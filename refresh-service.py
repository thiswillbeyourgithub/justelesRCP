# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "lxml", "brotli", "loguru", "click", "pymupdf>=1.24"]
# ///
"""Tiny companion service: on-demand, single-CIS RCP refresh.

The public site stays 100% static: Caddy serves precomputed files read-only and
nothing dynamic runs to render a page. This service is the ONE runtime piece,
deliberately minimal, behind three things:

  * the "Rafraichir maintenant" button on an RCP page (an on-demand, high-priority
    refresh), and
  * the automatic background refresh a page older than a year triggers on load, and
  * a perpetual background CRAWLER that walks every page in frequency (sold-units)
    order, refreshing any whose captured copy is older than the crawl TTL
    (``REFRESH_CRAWL_TTL_DAYS``, default 365d), then idles until the oldest fresh
    page ages past it again.

The on-demand button/auto requests and the crawler run on TWO SEPARATE worker
threads, each with its OWN rate limit, so a click is not stuck behind the crawler's
slow global gap: it is scraped on the fast on-demand lane (``REFRESH_DEMAND_RATE_SECONDS``,
default 5s) while the crawler keeps trickling on its own slow gap (``REFRESH_RATE_SECONDS``,
default 120s). Both lanes are still SERIAL (one worker each) and share the per-CIS
dedup + min-interval floor, so decoupling them adds speed without letting clicks
hammer ANSM.

It exposes a handful of JSON endpoints (see ``_Handler``); the important one is
``POST /api/refresh/<cis>``. That re-scrapes ONE drug, writes/refreshes its overlay,
re-renders just that page into ``dist/`` (so Caddy serves the fresh bytes,
precompressed siblings and all), and updates a manifest. Everything else is untouched.

TWO kinds of page can be refreshed, routed automatically by CIS (``_is_eu``):

  * an ordinary ANSM RCP page (``/rcp/``): re-scrape from the live ANSM site into
    ``data/rcp/<cis>.html[.gz]`` and re-render via ``build.render_record``; or
  * an EU-authorization page (``/eu/``): a centrally-authorized drug whose SmPC/
    notice lives in an EMA PDF, not the ANSM. Its refresh goes through the EMA lane
    (``_process_ema``), which reuses ``scrape-ema.process_one`` to fetch + convert
    the PDF into ``data/eu/<cis>.html[.gz]`` and ``build.render_eu_page`` to rebuild
    the page. A separate manifest (``data/.scrape-ema-manifest.json``) keeps the EMA
    TTL/last_fetch from colliding with the ANSM one. An ANSM re-scrape of such a CIS
    would come back empty, hence the routing.

Politeness is enforced HERE, never trusted to the caller (a user-triggered live
scraper is exactly what a government site would read as abuse):

  * each lane's rate limit serialises its outbound ANSM fetches (one worker thread
    per lane, a minimum gap + jitter between that lane's requests), so no amount of
    clicking or traffic can exceed a steady, gentle trickle; the on-demand lane's
    gap is small so a click feels instant, the crawler's is large; and
  * a per-CIS MIN-INTERVAL floor collapses repeat clicks and many users hitting
    the same stale page into a single fetch (the request just reports "fresh").

All scrape/clean/render logic is REUSED from scrape-rcp.py and build.py by import
(no duplication): this file only adds the queue, the rate limit and the HTTP shell.

Run locally:  uv run refresh-service.py --port 8460
Then a reverse proxy (Caddy, see docker/) maps /api/* to it, same origin as the
site, so the strict `connect-src 'self'` CSP keeps holding.
"""

from __future__ import annotations

import importlib.util
import json
import queue
import re
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click
from loguru import logger

ROOT = Path(__file__).parent


def _load_module(filename: str, name: str):
    """Import a sibling PEP 723 script by path (handles the hyphenated name).

    Both scripts guard their CLI/build behind ``if __name__ == '__main__'`` so
    importing them only defines functions and constants; nothing runs. This keeps
    the scrape and render logic single-sourced instead of copied in here.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scrape = _load_module("scrape-rcp.py", "scrape_rcp")
build = _load_module("build.py", "build_mod")
# EMA lane: fetch + convert an EMA product-information PDF into a /eu/ overlay.
# scrape-ema.py transitively imports scrape-rcp.py and ema_pdf.py (which needs
# pymupdf, hence the dep above); all import-safe (__main__-guarded).
ema_scrape = _load_module("scrape-ema.py", "scrape_ema")

CIS_RE = re.compile(r"\d{8}")
# Trigger sources tracked for the crawl stats: a manual button click ("user"),
# the >1-year auto-refresh a page fires on load ("auto"), and the perpetual
# background crawler ("crawl", set internally). Any other value a caller passes
# on /api/refresh is treated as "auto"; "crawl" is never accepted from a caller.
_SOURCES = ("user", "auto", "crawl")


def _asof_today() -> str:
    """Today's date (UTC) as ``YYYY-MM-DD`` for stamping a freshly scraped page."""
    return datetime.now(timezone.utc).date().isoformat()


def _fmt_dhm(seconds: float) -> str:
    """Format a duration as ``DdHhMm`` (days/hours/minutes), e.g. ``7d5h36m``.

    For the crawl sweep ETA, which spans days at the slow crawl rate: scrape's
    H:MM:SS (fine for the seconds-to-minutes on-demand queue) would read as an
    unwieldy hour count there (e.g. 347:13:20 instead of 14d11h13m).
    """
    total = int(max(0.0, seconds))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d{h}h{m}m"


class _CrawlLane:
    """Per-lane state for a perpetual crawler. There are two: the ANSM ``/rcp/``
    pages and the EMA ``/eu/`` pages. Each rotates its OWN frequency-ordered set
    through its OWN slow rate, manifest and TTL, so the crawler methods
    (_build_crawl_order / _claim_next_crawl / _idle_wait_seconds / _crawl_run) are
    written once and driven by whichever lane, instead of being duplicated. The two
    lanes still share the Refresher's ``_lock`` and ``_pending`` (so the same CIS is
    never fetched twice at once) and its ``_handle`` (which routes each CIS to the
    right fetch path via ``_is_eu``).

    ``order_fn`` is a callable returning the frequency-ordered CIS list (lanes order
    different sets and may raise SystemExit if the BDPM inputs are missing). ``order``
    is built once in the worker thread; ``idx`` is the rotating cursor; ``idle`` marks
    a rotation that found nothing due; ``last_fetch`` is this lane's throttle mark,
    touched only by its own worker thread (so it needs no lock).

    ``force`` / ``force_pending`` / ``wake`` drive a full re-crawl on demand
    (deploy.sh --rebuild -> SIGHUP, see Refresher.request_recrawl): ``force`` is a
    one-shot flag the signal handler sets; ``_claim_next_crawl`` consumes it by
    seeding ``force_pending`` with the whole ``order`` and then hands those pages out
    (in frequency order, ignoring the TTL) one full pass before returning to normal
    rotation. Existing overlays are kept and keep serving until each is refreshed.
    ``wake`` is an Event the handler sets so an idle worker stops sleeping at once
    instead of after its (up to hour-long) idle wait.
    """

    def __init__(self, name, enabled, ttl_days, rate, manifest, order_fn):
        self.name = name
        self.enabled = enabled
        self.ttl_days = ttl_days
        self.rate = rate
        self.manifest = manifest      # the lane's manifest dict (shared ref)
        self.order_fn = order_fn      # () -> list[str], may raise SystemExit
        self.order: list[str] = []
        self.idx = 0
        self.idle = False
        self.idle_logged = False
        self.last_fetch = 0.0
        self.force = False                    # armed by SIGHUP; consumed in _claim_next_crawl
        self.force_pending: set[str] = set()  # CIS still to re-crawl in the forced pass
        self.wake = threading.Event()         # set by SIGHUP to break an idle sleep


class Refresher:
    """Serialises refreshes behind rate-limited worker threads, one per lane.

    Splitting the lanes onto separate workers is what makes a click feel instant:
    it no longer waits behind the crawler's slow global gap. Each worker sleeps its
    own ``rate`` (+ jitter) between fetches, so each lane's request rate to ANSM is
    bounded on its own no matter what enqueues work. Two sources, two workers:

    * an ON-DEMAND lane (``_demand``, worker ``_demand_run``) for the button and the
      >1-year auto-refresh, throttled by the small ``demand_rate`` so a lone click
      after an idle period fetches almost at once. A CIS already queued or in flight
      is not enqueued again (dedup), and a CIS refreshed within ``min_interval``
      seconds is reported "fresh" without any fetch.
    * TWO perpetual CRAWLERS (worker ``_crawl_run`` per lane, see ``_CrawlLane``),
      one for the ANSM ``/rcp/`` pages (throttled by ``rate``) and one for the EMA
      ``/eu/`` pages (throttled by the slower ``eu_rate``, since the EMA is strict
      about automated access). Each rotates its own frequency-ordered set, picking
      the next page whose captured copy is older than its lane TTL; when a full
      rotation finds nothing due it idles until the oldest fresh page ages past the
      TTL, then resumes. ``crawl=False`` / ``eu_crawl=False`` disables either.

    All workers share ``_pending`` (under ``_lock``) so the same CIS is never fetched
    by two at once, and ``_persist_lock`` so their manifest writes never race on the
    temp file. Each worker owns its own throttle mark (written only by that thread),
    so the throttles need no lock.
    """

    def __init__(self, *, rate: float, demand_rate: float, min_interval: float,
                 timeout: float, gzip_overlay: bool, user_agent: str, queue_max: int,
                 crawl: bool = True, crawl_ttl_days: int = 365,
                 eu_crawl: bool = True, eu_rate: float = 300.0,
                 eu_crawl_ttl_days: int = 180, embed_notify_url: str = "") -> None:
        self.rate = rate
        self.demand_rate = demand_rate
        self.min_interval = min_interval
        self.timeout = timeout
        self.gzip_overlay = gzip_overlay
        self.user_agent = user_agent
        # Best-effort URL of the sibling embed service. After a fresh overlay lands
        # we ping it so it re-embeds this page for semantic search (content-hash
        # gated on its side, so an unchanged re-crawl re-embeds nothing). Empty
        # disables the notify; the embedder is optional and its own reconcile sweep
        # is the backstop, so a failed/absent ping never disturbs the refresh.
        self._embed_notify_url = (embed_notify_url or "").rstrip("/")
        # On-demand (button/auto) lane. It runs on its own fast worker and is the
        # only lane a caller can fill, so its bound is what sheds load as "busy".
        self._demand: queue.Queue[str] = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        # Serialises the two workers' manifest writes (save_manifest uses one fixed
        # temp path, so concurrent writers would clobber each other's .tmp).
        self._persist_lock = threading.Lock()
        # cis -> trigger source ("user" | "auto" | "crawl") while queued/in flight
        self._pending: dict[str, str] = {}
        self._manifest = scrape.load_manifest()
        # On-demand lane throttle mark (each crawler lane owns its own, see _CrawlLane).
        self._last_demand_fetch = 0.0
        # Crawl statistics, surfaced in the logs and at GET /api/stats. Counts of
        # *completed* refreshes by outcome (ok/empty/error) and by trigger source
        # (user = button, auto = >1yr page-load refresh, crawl = background crawler),
        # plus request-level short-circuits (fresh = min-interval hit, busy = queue
        # full). ok+empty+error == user+auto+crawl by construction.
        self._stats = {"ok": 0, "empty": 0, "error": 0,
                       "user": 0, "auto": 0, "crawl": 0,
                       "fresh": 0, "busy": 0}
        # Prime build.py's render globals (names + page template + cross-drug
        # backlink index) once, so render_record() can run outside its normal pool
        # worker AND a refreshed page carries the same "Médicaments liés" links a
        # full build would produce (build_xref_index needs the BDPM composition +
        # frequency files, mounted read-only into this container; absent them it
        # returns {} and the rebuilt page simply has no backlinks).
        names = build.load_names()
        tpl = (build.SRC / "rcp.html").read_text(encoding="utf-8")
        # Restrict link targets to CIS that already have a built page. This
        # container has no CIS_RCP.csv (only dist/rcp is mounted), so derive the
        # page set from the rendered files rather than the source. Prevents a
        # backlink to a pageless CIS (which would 404, e.g. HELICOBACTER). The
        # crawler is also restricted to this set (it never fetches a pageless CIS).
        page_cis = build.page_cis_from_dist()
        self._page_cis = page_cis
        xref = build.build_xref_index(names, page_cis)
        # Active-substance search strings for the external-reference pill row a
        # refreshed page carries (build._ref_links_html), same as a full build. Reads
        # CIS_COMPO_bdpm.txt, mounted read-only here for the backlink index; absent it
        # the row falls back to each drug's brand root.
        substances = build.load_substances()
        logger.info(
            "primed render: {} names, {} pages, {} backlink terms, {} substance links",
            len(names), len(page_cis), len(xref), len(substances),
        )
        build._init_worker(names, tpl, xref, substances)
        self._tpl = tpl  # reused by render_eu_page for on-demand /eu/ refreshes
        # EMA (/eu/) lane state. A centrally-authorized drug has no /rcp/ page; its
        # SmPC/notice lives in an EMA PDF that scrape-ema.py fetches + converts into
        # a data/eu overlay, which build.render_eu_page turns into the /eu/ page.
        # An on-demand refresh of such a page must go through THIS lane, not the
        # ANSM scrape (which would come back empty). A CIS is a /eu/ one when it is
        # centrally authorized (in the cap-meta set) but has no /rcp/ page.
        self._cap = build.load_cap_meta()
        self._eu_cis = frozenset(c for c in self._cap if c not in page_cis)
        # Presentations of one product share a single EMA PDF/overlay, so a /eu/
        # page with no overlay of its own borrows a sibling's (build.resolve_eu).
        # Group once; drives _eu_url + the EMA crawl order.
        self._auth_groups = build.auth_groups(self._cap)
        # Separate manifest so the EMA TTL/last_fetch never collides with the ANSM
        # one; ema_links maps CIS -> the exact EMA PDF URL harvested into the ANSM
        # manifest (fallback: the URL baked on an existing overlay, see _eu_url).
        self._ema_manifest = scrape.load_manifest(ema_scrape.EMA_MANIFEST_PATH)
        self._ema_links = ema_scrape.ema_links(self._manifest)
        logger.info("primed EMA lane: {} centrally-authorized /eu/ CIS, {} PDF links",
                    len(self._eu_cis), len(self._ema_links))
        # Two perpetual crawler lanes, each rotating its own frequency-ordered set on
        # its own rate + manifest + TTL (the shared crawler methods are driven by
        # whichever lane): the ANSM /rcp/ pages, and the slower EMA /eu/ pages.
        self._ansm_lane = _CrawlLane(
            "rcp", crawl, crawl_ttl_days, rate, self._manifest,
            lambda: scrape.build_queue(crawl_ttl_days, force=True, restrict=page_cis))
        self._eu_lane = _CrawlLane(
            "eu", eu_crawl, eu_crawl_ttl_days, eu_rate, self._ema_manifest,
            self._build_eu_order)
        self._crawl_lanes = (self._ansm_lane, self._eu_lane)
        # Workers: the on-demand lane always runs; each crawl lane only when enabled.
        self._demand_worker = threading.Thread(target=self._demand_run,
                                               name="demand", daemon=True)
        self._crawl_workers = [
            (lane, threading.Thread(target=self._crawl_run, args=(lane,),
                                    name=f"crawler-{lane.name}", daemon=True))
            for lane in self._crawl_lanes
        ]

    def start(self) -> None:
        self._demand_worker.start()
        for lane, worker in self._crawl_workers:
            if lane.enabled:
                worker.start()

    def _build_eu_order(self) -> list[str]:
        """Frequency-ordered /eu/ CIS the EMA crawler rotates through: every
        centrally-authorized CIS whose authorization GROUP has at least one known
        EMA PDF link or already-built overlay. A sibling presentation counts (they
        share one PDF, so _eu_url resolves a URL to refetch), so ALL strengths of a
        seeded product are crawled; each borrowing page grows its OWN overlay on the
        first fetch (self-healing). A group nobody has seeded yet is excluded (no URL
        to fetch); its pages stay stubs until the button harvests a link live.
        Reuses scrape.build_queue's ordering; raises SystemExit (caught by
        _build_crawl_order) if the BDPM inputs are gone."""
        have = {p.name.split(".")[0] for p in build.EU_OVERLAY_DIR.glob("*.html*")}
        seeded = (set(self._ema_links) | have) & self._eu_cis
        if not seeded:
            return []
        keys = {build._auth_key(*self._cap[c][:2]) for c in seeded}
        targets = {c for c in self._eu_cis
                   if build._auth_key(*self._cap[c][:2]) in keys}
        return scrape.build_queue(0, force=True, restrict=targets)

    # -- state helpers -------------------------------------------------------

    def _is_eu(self, cis: str) -> bool:
        """True for a centrally-authorized /eu/ CIS (its refresh goes through the
        EMA lane, not the ANSM scrape). Immutable after __init__, so no lock."""
        return cis in self._eu_cis

    def _eu_url(self, cis: str) -> str:
        """The EMA PDF URL to (re)fetch for a /eu/ CIS, SHARED across its
        authorization group (build.resolve_eu): the link harvested for this CIS,
        else a sibling presentation's link, else the URL baked on this CIS's (or a
        sibling's) overlay. "" if nothing in the group knows one yet; _process_ema
        then tries a live ANSM harvest before giving up."""
        return build.resolve_eu(cis, self._cap, self._auth_groups, self._ema_links)[1]

    def _entry(self, cis: str) -> dict | None:
        # Route to the EMA manifest for /eu/ CIS so asof_of / _recently_fetched
        # read the right lane's last_fetch (for /eu/, that date IS data-rcp-asof).
        with self._lock:
            man = self._ema_manifest if cis in self._eu_cis else self._manifest
            entry = man.get(cis)
            return dict(entry) if entry else None

    def asof_of(self, cis: str) -> str:
        """Best-known 'as of' date for a CIS: its last scrape date if we have one.

        Empty string when never scraped (the page then still carries whatever the
        build baked, e.g. the 2022 baseline date); the caller only uses this to
        detect that a refresh has landed.
        """
        entry = self._entry(cis)
        last = (entry or {}).get("last_fetch")
        if not last:
            return ""
        try:
            return datetime.fromisoformat(last).date().isoformat()
        except ValueError:
            return ""

    def _recently_fetched(self, cis: str) -> bool:
        """True if this CIS was fetched within min_interval (anti-hammer floor)."""
        entry = self._entry(cis)
        last = (entry or {}).get("last_fetch")
        if not last:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            return False
        return age < self.min_interval

    def is_pending(self, cis: str) -> bool:
        with self._lock:
            return cis in self._pending

    def _eta_seconds(self, n: int) -> float:
        """Rough seconds to drain ``n`` queued fetches at the on-demand rate limit.

        The queued items live on the on-demand lane, which drains at ``demand_rate``:
        each fetch waits the base rate plus, on average, half the 0..min(rate,10)s
        jitter, plus ~1s for the request itself. Good enough for an ETA hint.
        """
        return n * (self.demand_rate + min(self.demand_rate, 10.0) / 2 + 1.0)

    def _due_count_locked(self, lane: _CrawlLane) -> int:
        """Count a lane's crawl pages still due per its TTL. CALLER MUST HOLD ``_lock``.

        A live sweep-size hint: how many pages the lane still has to fetch before it
        goes idle. O(len(order)) but cheap (a dict lookup + one ISO parse each), and
        only ever run under the lock the callers already hold.
        """
        if not lane.enabled:
            return 0
        return sum(1 for cis in lane.order
                   if scrape.is_due(lane.manifest.get(cis), lane.ttl_days))

    @staticmethod
    def _crawl_eta_seconds(due: int, rate: float) -> float:
        """Rough seconds to finish a crawl sweep of ``due`` pages on a lane's ``rate``.

        Each lane crawler is serial on its slow rate: each due page costs the base
        rate plus, on average, half the 0..min(rate,10)s jitter. Mirrors
        ``_eta_seconds`` for the on-demand lane. Zero when nothing is due (idle).
        """
        return due * (rate + min(rate, 10.0) / 2)

    def _gauge_locked(self, lane: _CrawlLane) -> dict:
        """One lane's crawl gauge for GET /api/stats. CALLER MUST HOLD ``_lock``.

        During a forced re-crawl (deploy.sh --rebuild), pages are refetched
        regardless of the TTL, so the remaining-to-fetch count is the larger of the
        TTL-due count and the forced backlog; ``forced`` exposes that backlog so an
        operator can watch a --rebuild sweep drain.
        """
        due = max(self._due_count_locked(lane), len(lane.force_pending))
        return {"enabled": lane.enabled, "total": len(lane.order), "idx": lane.idx,
                "ttl_days": lane.ttl_days, "idle": lane.idle, "due": due,
                "forced": len(lane.force_pending),
                "eta_seconds": round(self._crawl_eta_seconds(due, lane.rate), 1)}

    def stats(self) -> dict:
        """Snapshot of the crawl counters (served at GET /api/stats)."""
        with self._lock:
            snap = dict(self._stats)
            queued = self._demand.qsize()
            pending = len(self._pending)
            crawl = self._gauge_locked(self._ansm_lane)
            crawl_eu = self._gauge_locked(self._eu_lane)
        snap["done"] = snap["ok"] + snap["empty"] + snap["error"]
        snap["queued"] = queued  # on-demand (button/auto) requests waiting
        snap["pending"] = pending
        snap["eta_seconds"] = round(self._eta_seconds(queued), 1)
        snap["crawl"] = crawl        # ANSM /rcp/ lane (unchanged shape)
        snap["crawl_eu"] = crawl_eu  # EMA /eu/ lane, same shape
        return snap

    # -- perpetual crawler (one instance per lane) ---------------------------

    def _build_crawl_order(self, lane: _CrawlLane) -> None:
        """Build the frequency-ordered page list a lane rotates through.

        Reuses the lane's ``order_fn`` (scrape.build_queue's frequency ordering, the
        SAME the batch scrapers use), restricted to CIS that actually render a page.
        Runs once in the worker thread so the HTTP server can start serving
        immediately; per-page due-ness is checked live in _claim_next_crawl, not
        frozen here. Degrades to no crawler (empty order) if the BDPM inputs are
        missing rather than crashing the service.
        """
        if not lane.enabled:
            return
        try:
            order = lane.order_fn()
        except SystemExit as exc:  # e.g. missing CIS_bdpm; degrade, don't crash
            logger.warning("{} crawler disabled: {}", lane.name, exc)
            lane.enabled = False
            return
        lane.order = order
        logger.info("{} crawler armed: {} pages in frequency order, ttl {}d, rate {}s",
                    lane.name, len(order), lane.ttl_days, lane.rate)

    def _claim_next_crawl(self, lane: _CrawlLane) -> str | None:
        """Claim the lane's next page to crawl, or None if there is nothing to do.

        Normally rotates a cursor through the frequency-ordered page list, returning
        the first CIS that is due per the lane TTL (see scrape.is_due) and not already
        queued/in flight (in the SHARED ``_pending``, so the two lanes + on-demand
        never double-fetch a CIS). The claimed CIS is marked pending as "crawl"; a
        full rotation with nothing due flips the lane to idle and returns None.

        When a full re-crawl was armed (deploy.sh --rebuild -> SIGHUP set
        ``lane.force``), the flag is consumed here by seeding ``force_pending`` with
        the whole order, and those pages are then handed out in frequency order
        IGNORING the TTL, one full pass, before normal rotation resumes. Seeding
        happens here (not in the handler) so it always reads the fully-built order,
        even if the signal raced ahead of _build_crawl_order.
        """
        with self._lock:
            n = len(lane.order)
            if not n:
                lane.idle = True
                return None
            if lane.force:
                # Consume the one-shot force flag: (re)start a full forced pass.
                lane.force_pending = set(lane.order)
                lane.force = False
            if lane.force_pending:
                # Forced pass: next not-in-flight page, in frequency order, regardless
                # of TTL. Existing overlays keep serving until each is re-fetched.
                for cis in lane.order:
                    if cis in lane.force_pending and cis not in self._pending:
                        lane.force_pending.discard(cis)
                        self._pending[cis] = "crawl"
                        lane.idle = False
                        lane.idle_logged = False
                        return cis
                # All still-forced pages are momentarily in flight elsewhere; fall
                # through to the TTL rotation (they get re-checked next call).
            for _ in range(n):
                cis = lane.order[lane.idx]
                lane.idx = (lane.idx + 1) % n
                if cis in self._pending:
                    continue  # already queued/in flight (on-demand or other lane)
                if scrape.is_due(lane.manifest.get(cis), lane.ttl_days):
                    self._pending[cis] = "crawl"
                    lane.idle = False
                    lane.idle_logged = False
                    return cis
            lane.idle = True
            return None

    def _idle_wait_seconds(self, lane: _CrawlLane) -> float:
        """Seconds until the lane's oldest fresh page next crosses its TTL (capped).

        Called only after a full rotation found nothing due, so every page has a
        recent last_fetch: wake at the soonest one's expiry so the crawler resumes
        exactly when a page ages past the TTL, but re-poll at least hourly so a
        manifest or clock change is noticed. A page with no/invalid timestamp is
        due now (return ~immediately).
        """
        cap = 3600.0
        now = datetime.now(timezone.utc)
        ttl = timedelta(days=lane.ttl_days)
        soonest: float | None = None
        with self._lock:
            for cis in lane.order:
                last = (lane.manifest.get(cis) or {}).get("last_fetch")
                if not last:
                    return 1.0
                try:
                    secs = (datetime.fromisoformat(last) + ttl - now).total_seconds()
                except ValueError:
                    return 1.0
                if soonest is None or secs < soonest:
                    soonest = secs
        if soonest is None:
            return cap
        return max(1.0, min(soonest, cap))

    def request_recrawl(self) -> int:
        """Arm a full forced re-crawl of every enabled lane (deploy.sh --rebuild,
        delivered as SIGHUP; see the handler in main()).

        Signal-safe: it only flips each lane's one-shot ``force`` flag and sets its
        ``wake`` Event, both plain non-blocking writes, so it never takes ``_lock``
        (the main thread runs this from the signal handler and must not risk blocking
        on a worker's critical section). The real work (seeding ``force_pending`` from
        the lane order and handing pages out ignoring the TTL) happens in the worker
        via ``_claim_next_crawl``; ``wake`` bumps an idle worker out of its sleep so
        the sweep starts at once. Existing overlays are kept the whole time and keep
        serving until each page is re-fetched. Returns a best-effort count of pages
        armed (a lane whose order is still building reports 0 but is still armed).
        """
        armed = 0
        for lane in self._crawl_lanes:
            if not lane.enabled:
                continue
            lane.force = True
            lane.wake.set()
            armed += len(lane.order)
        return armed

    # -- public API ----------------------------------------------------------

    def request(self, cis: str, source: str = "auto") -> dict:
        """Enqueue an on-demand refresh for one CIS; return a small status dict.

        ``source`` is the trigger, tallied for the crawl stats: "user" (button) or
        "auto" (the >1-year page-load refresh); any other value (including "crawl",
        which only the background crawler sets internally) is treated as "auto". A
        manual click on an already-queued item upgrades the recorded source to
        "user" so the stats credit the human action, AND, because on-demand items
        have priority over the crawler, guarantees the human wait is honoured next.

        ``fresh``  - refreshed within min_interval, nothing to do.
        ``queued`` - accepted (either just now or already in flight).
        ``busy``   - the on-demand lane is full; the caller should retry later.
        """
        # "crawl" is an internal source the caller may not set; anything else
        # unrecognised collapses to "auto". _SOURCES stays the canonical set.
        source = source if source in _SOURCES and source != "crawl" else "auto"
        if self._recently_fetched(cis):
            with self._lock:
                self._stats["fresh"] += 1
            return {"status": "fresh", "asof": self.asof_of(cis)}
        # NB: asof_of() reads the manifest under self._lock, so it must NOT be
        # called while we hold the lock here (self._lock is a plain, non-reentrant
        # Lock; re-acquiring it on the same thread deadlocks). Decide under the
        # lock, then read asof after releasing it.
        with self._lock:
            already = cis in self._pending
            if already:
                # Upgrade a queued auto/crawl item to "user" so a human click both
                # credits the stats and jumps ahead of the crawler on the next pick.
                if source == "user":
                    self._pending[cis] = "user"
            else:
                try:
                    self._demand.put_nowait(cis)
                except queue.Full:
                    self._stats["busy"] += 1
                    return {"status": "busy"}
                self._pending[cis] = source
                pending_n = self._demand.qsize()
        if not already:
            logger.info("queued {} [{}] (on-demand pending={})", cis, source, pending_n)
        return {"status": "queued", "asof": self.asof_of(cis)}

    # -- worker --------------------------------------------------------------

    @staticmethod
    def _wait_rate(since: float, rate: float) -> float:
        """Sleep until ``rate`` (+ jitter) seconds have elapsed since ``since``.

        Returns the new monotonic mark for the caller to store. Kept lane-agnostic
        so the on-demand and crawler workers reuse it with their own rate + mark.
        """
        gap = rate + scrape.random.uniform(0.0, min(rate, 10.0))
        wait = since + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return time.monotonic()

    def _client(self) -> "scrape.httpx.Client":
        """Fresh HTTP client for one worker (each worker owns its own client)."""
        return scrape.httpx.Client(follow_redirects=True, timeout=self.timeout,
                                   headers={"User-Agent": self.user_agent})

    def _handle(self, client, cis: str, source: str) -> None:
        """Run one refresh with full error handling; always clears the pending mark.

        Shared by both workers so the fetch/render/record + failure bookkeeping
        lives in one place. On any error it records an error entry and still frees
        the CIS from ``_pending`` so the crawler can retry it later.
        """
        try:
            self._process(client, cis, source)
        except Exception as exc:  # never let a worker thread die
            logger.error("refresh {} [{}] failed: {}", cis, source, exc)
            # Record the error in the lane's own manifest (the EMA path normally
            # handles its errors internally; this covers an unexpected raise).
            eu = self._is_eu(cis)
            entry = {"last_fetch": scrape._now_iso(), "status": "error", "error": str(exc)[:200]}
            with self._lock:
                (self._ema_manifest if eu else self._manifest)[cis] = entry
            if eu:
                self._persist(self._ema_manifest, ema_scrape.EMA_MANIFEST_PATH)
            else:
                self._persist_manifest()
            self._record(cis, source, "error", f"ERROR {str(exc)[:120]}")
        finally:
            with self._lock:
                self._pending.pop(cis, None)

    def _demand_run(self) -> None:
        """On-demand (button/auto) worker: serial, on its OWN fast rate limit.

        Decoupled from the crawler so a click is not stuck behind the slow global
        gap: it blocks on the demand queue, and a lone click after an idle period
        fetches almost at once (``demand_rate`` only spaces out bursts of distinct
        pages). ``_process`` picks the live source from ``_pending`` so a mid-flight
        upgrade to "user" is not needed here.
        """
        with self._client() as client:
            while True:
                cis = self._demand.get()  # blocks until a click/auto-refresh arrives
                with self._lock:
                    source = self._pending.get(cis, "auto")
                self._last_demand_fetch = self._wait_rate(self._last_demand_fetch,
                                                          self.demand_rate)
                self._handle(client, cis, source)

    def _crawl_run(self, lane: _CrawlLane) -> None:
        """Perpetual crawler worker for ONE lane: serial, on that lane's rate limit.

        Builds the lane's frequency-ordered page list once (here, not in __init__, so
        the HTTP server starts serving immediately instead of blocking on the BDPM
        I/O), then rotates the cursor, refreshing each due page and idling when none
        is. ``_handle`` routes each CIS to the ANSM or EMA fetch path via ``_is_eu``.
        """
        self._build_crawl_order(lane)
        if not lane.enabled or not lane.order:
            logger.info("{} crawler: nothing to crawl (disabled or empty order)", lane.name)
            return
        with self._client() as client:
            while True:
                cis = self._claim_next_crawl(lane)
                if cis is None:  # nothing due: idle until the oldest page ages out
                    wait = self._idle_wait_seconds(lane)
                    if not lane.idle_logged:
                        logger.info("{} crawler idle: all {} pages within {}d; next due in {}",
                                    lane.name, len(lane.order), lane.ttl_days,
                                    scrape._fmt_dur(wait))
                        lane.idle_logged = True
                    # Sleep on the lane's wake Event, not a bare time.sleep, so a
                    # SIGHUP-armed re-crawl (request_recrawl sets force + wake) breaks
                    # the idle wait at once instead of after up to an hour.
                    if lane.wake.wait(timeout=wait):
                        lane.wake.clear()
                        lane.idle_logged = False
                    continue
                lane.last_fetch = self._wait_rate(lane.last_fetch, lane.rate)
                self._handle(client, cis, "crawl")

    def _persist(self, manifest: dict, path=None) -> None:
        """Persist a manifest, best-effort and NEVER fatal.

        A manifest is only a TTL cache (build.py re-derives each page's capture
        date from the overlay itself), so a failure to write it must not sink a
        refresh. It is snapshotted under the lock and written outside it, and is
        always called AFTER the page has been re-rendered, so the user-visible
        page update lands even when /app/data cannot be written. save_manifest
        already falls back to an in-place write when its atomic temp+rename cannot
        work (an EROFS .tmp on the read-only refresh rootfs); this additionally
        swallows even that fallback failing, logging instead of raising. ``path``
        selects the lane's manifest file (None = the ANSM one; the EMA lane passes
        its own); save_manifest derives a per-path temp so the two never collide.
        """
        with self._lock:
            snapshot = dict(manifest)
        # Serialise the actual write across both workers: save_manifest uses one
        # temp path per manifest, so two concurrent writers of the SAME manifest
        # would clobber each other's .tmp.
        with self._persist_lock:
            try:
                scrape.save_manifest(snapshot, path)
            except OSError as exc:
                logger.warning("could not persist {} manifest ({}); refresh already applied",
                               "EMA" if path else "scrape", exc)

    def _persist_manifest(self) -> None:
        """Persist the ANSM scrape manifest (thin wrapper over _persist)."""
        self._persist(self._manifest)

    def _record(self, cis: str, source: str, outcome: str, result: str) -> None:
        """Tally one completed refresh and emit its progress line.

        ``outcome`` is 'ok' | 'empty' | 'error'. A crawler item logs which lane
        (rcp/eu) plus its position in that lane's rotation and its sweep ETA
        (still-due pages x the lane's rate); an on-demand item logs the live
        on-demand queue depth + ETA. A compact aggregate line (both lanes' due +
        ETA) follows at the first completion and every 10th, so the overall run is
        visible at INFO without the per-request DEBUG chatter.
        """
        with self._lock:
            self._stats[outcome] += 1
            self._stats[source] += 1
            snap = dict(self._stats)
            to_go = self._demand.qsize()
            lane = None
            if source == "crawl":
                lane = self._eu_lane if self._is_eu(cis) else self._ansm_lane
                lane_name, lane_idx, lane_total = lane.name, lane.idx, len(lane.order)
                lane_due = self._due_count_locked(lane)
                lane_rate = lane.rate
            agg = None
            done = snap["ok"] + snap["empty"] + snap["error"]
            if done == 1 or done % 10 == 0:  # both lanes' due, only for the aggregate
                agg = (self._due_count_locked(self._ansm_lane),
                       self._due_count_locked(self._eu_lane))
        if source == "crawl":
            logger.info("crawl[{}] {}/{} {} -> {} | due~{} sweep-eta {} | on-demand to-go={}",
                        lane_name, lane_idx, lane_total, cis, result, lane_due,
                        _fmt_dhm(self._crawl_eta_seconds(lane_due, lane_rate)), to_go)
        else:
            logger.info("refreshed {} [{}] -> {} | on-demand to-go={} eta {}", cis, source,
                        result, to_go, scrape._fmt_dur(self._eta_seconds(to_go)))
        if agg is not None:
            a_due, e_due = agg
            logger.info(
                "stats | done={} (crawl={} auto={} user={}) ok={} empty={} err={} "
                "| rcp-crawl due~{} eta {} | eu-crawl due~{} eta {} | on-demand to-go={} eta {}",
                done, snap["crawl"], snap["auto"], snap["user"],
                snap["ok"], snap["empty"], snap["error"],
                a_due, _fmt_dhm(self._crawl_eta_seconds(a_due, self._ansm_lane.rate)),
                e_due, _fmt_dhm(self._crawl_eta_seconds(e_due, self._eu_lane.rate)),
                to_go, scrape._fmt_dur(self._eta_seconds(to_go)),
            )

    def _process(self, client, cis: str, source: str) -> None:
        """Dispatch one refresh to the right lane: the EMA PDF path for a /eu/ CIS,
        the ANSM scrape otherwise. Both re-render the ONE page and persist their
        own manifest. The caller has already waited on its lane's rate limit."""
        if self._is_eu(cis):
            self._process_ema(client, cis, source)
        else:
            self._process_ansm(client, cis, source)

    def _process_ansm(self, client, cis: str, source: str) -> None:
        logger.debug("fetching {} [{}] from {}", cis, source, scrape.PAGE_URL.format(cis=cis))
        page, status = scrape.fetch_one(client, cis)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        rcp = scrape.extract_rcp(page)
        scrape.write_overlay(cis, rcp, self.gzip_overlay)
        asof = _asof_today()
        digest = scrape.hashlib.sha256(rcp.encode("utf-8")).hexdigest()
        with self._lock:
            self._manifest[cis] = {"last_fetch": scrape._now_iso(), "hash": digest,
                                   "status": "ok", "http": status}
        # Re-render this ONE page (writes dist/rcp/<slug>.html + .gz/.br) BEFORE
        # persisting the manifest. The rebuilt page, carrying today's "vérifiée
        # par justelesRCP le" capture date, IS the point of the refresh; the
        # manifest is merely a TTL cache. So persistence is a best-effort LAST
        # step that can never abort the refresh: a manifest-write failure (e.g.
        # EROFS on the read-only /app/data) no longer leaves the page stuck on
        # its old capture date, which is exactly the bug this ordering fixes.
        if rcp == "":
            self._record(cis, source, "empty", "no RCP (empty overlay)")
        else:
            row = build.render_record((cis, rcp, asof))
            if row is None:
                logger.warning("render produced nothing for {}", cis)
                self._record(cis, source, "empty", "render produced nothing")
            else:
                self._record(cis, source, "ok", f"{row['slug']} ({len(rcp)} bytes)")
                self._notify_embed(cis)  # re-embed this page for semantic search
        self._persist_manifest()

    def _harvest_ema_url(self, client, cis: str) -> str:
        """Last-resort on-demand harvest of a /eu/ CIS's EMA PDF link when neither it
        nor any sibling has one (its authorization group was never ANSM-scraped).

        Fetches the live ANSM /medicament/<cis>/extrait page and reads the
        product-information href off it, exactly as scrape-rcp.py's batch harvest
        does, then caches it into the ANSM manifest so later builds/refreshes (and
        the CIS's siblings) resolve it. Returns the URL, or "" if the page links
        none. Only the on-demand button reaches this path (the crawl order already
        excludes never-seeded groups), so a user never has to wait for the batch
        scraper to catch up before their refresh does anything."""
        try:
            page, status = scrape.fetch_one(client, cis)
        except Exception as exc:
            logger.warning("ANSM harvest for {} failed: {}", cis, str(exc)[:120])
            return ""
        if status != 200:
            return ""
        url = scrape.extract_ema_pdf(page)
        if not url:
            return ""
        with self._lock:
            entry = dict(self._manifest.get(cis) or {})
            entry["ema_pdf"] = url
            self._manifest[cis] = entry
            self._ema_links[cis] = url  # so this + sibling lookups resolve it now
        self._persist_manifest()
        logger.info("harvested EMA PDF link for {} live from ANSM", cis)
        return url

    def _process_ema(self, client, cis: str, source: str) -> None:
        """Refresh one /eu/ page: fetch its EMA PDF, convert it, and re-render the
        page. Mirrors _process_ansm but on the EMA lane (its own manifest). Reuses
        scrape-ema.process_one (fetch->convert->overlay) and build.render_eu_page,
        so no scrape/convert/render logic is duplicated here."""
        url = self._eu_url(cis)
        if not url:
            # Nothing in this CIS's authorization group has a known EMA PDF link: a
            # user clicked refresh on a never-scraped stub and must NOT be told to
            # wait for the batch scraper. Harvest the link live off the ANSM page,
            # then proceed (writes this CIS's OWN overlay, so it becomes full).
            url = self._harvest_ema_url(client, cis)
        if not url:  # still nothing we can fetch
            logger.warning("no EMA PDF url for {} [{}]; cannot refresh", cis, source)
            self._record(cis, source, "empty", "no EMA PDF url")
            return
        # process_one catches its own network/parse errors and returns
        # status='error' (it does not raise), so a dead EMA link degrades cleanly.
        entry = ema_scrape.process_one(client, cis, url, self.gzip_overlay)
        with self._lock:
            self._ema_manifest[cis] = entry
        status = entry.get("status")
        if status == "ok":
            # Re-read the overlay process_one just wrote, then render the /eu/ page
            # (writes dist/eu/<slug>.html + .gz/.br) BEFORE persisting the manifest,
            # same best-effort ordering as the ANSM lane.
            overlay = build._read_overlay(build._overlay_path(cis, build.EU_OVERLAY_DIR))
            row = build.render_eu_page(cis, overlay, self._cap.get(cis), self._tpl, url)
            if row is None:
                logger.warning("EU render produced nothing for {}", cis)
                self._record(cis, source, "empty", "render produced nothing")
            else:
                self._record(cis, source, "ok", f"{row['slug']} (eu, {entry.get('bytes', 0)} bytes)")
                self._notify_embed(cis)  # re-embed this /eu/ page for semantic search
        elif status == "empty":
            self._record(cis, source, "empty", "EMA PDF yielded no HTML")
        else:
            self._record(cis, source, "error", f"ERROR {str(entry.get('error', ''))[:120]}")
        self._persist(self._ema_manifest, ema_scrape.EMA_MANIFEST_PATH)

    def _notify_embed(self, cis: str) -> None:
        """Tell the embed service a fresh overlay landed for this CIS, so it re-embeds
        the page for semantic search (POST /api/sem/page/<cis>?src=crawl).

        Fire-and-forget in a daemon thread so a slow or absent embedder never delays
        the refresh worker, and every error is swallowed: the embedder is optional,
        content-hash gated (an unchanged re-crawl re-embeds nothing on its side), and
        its own periodic reconcile sweep is the backstop for any missed ping."""
        base = self._embed_notify_url
        if not base:
            return
        url = f"{base}/api/sem/page/{cis}?src=crawl"

        def _fire() -> None:
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                urllib.request.urlopen(req, timeout=5).close()
            except Exception as exc:  # embedder optional; never disturb the refresh
                logger.debug("embed notify for {} failed: {}", cis, str(exc)[:120])

        threading.Thread(target=_fire, name=f"embed-notify-{cis}", daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    """Minimal JSON API. Routes:

    ``POST /api/refresh/<cis>[?src=user|auto]`` - enqueue a refresh; -> {status, asof?}.
    ``GET  /api/status/<cis>``  - {asof, pending} so the button can poll.
    ``GET  /api/stats``         - crawl counters by source + queue depth + ETA.
    ``GET  /api/health``        - {ok: true} for container healthchecks (never logged).
    """

    server_version = "justelesRCP-refresh"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # route through loguru
        # The container healthcheck hits /api/health every 30s forever; logging
        # it would bury the meaningful lines, so drop it entirely (even at DEBUG).
        # Everything else is DEBUG, so it stays quiet at the default INFO level
        # but is available when REFRESH_LOG_LEVEL=DEBUG for troubleshooting.
        if self.path == "/api/health":
            return
        logger.debug("http {} - {}", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send(200, {"ok": True})
            return
        if self.path == "/api/stats":
            self._send(200, REFRESHER.stats())
            return
        m = re.fullmatch(r"/api/status/(\d{8})", self.path)
        if m:
            cis = m.group(1)
            self._send(200, {"asof": REFRESHER.asof_of(cis),
                             "pending": REFRESHER.is_pending(cis)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)  # strip any ?src=... query before matching
        m = re.fullmatch(r"/api/refresh/(\d{8})", parts.path)
        if not m:
            self._send(404, {"error": "not found"})
            return
        src = parse_qs(parts.query).get("src", ["auto"])[0]
        result = REFRESHER.request(m.group(1), src)
        code = 429 if result.get("status") == "busy" else 200
        self._send(code, result)


REFRESHER: Refresher | None = None  # set in main(), read by _Handler


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default="127.0.0.1", show_default=True, envvar="REFRESH_HOST",
              help="Bind address (env REFRESH_HOST). Use 0.0.0.0 behind the proxy.")
@click.option("--port", type=int, default=8460, show_default=True, envvar="REFRESH_PORT",
              help="Port to listen on (env REFRESH_PORT).")
@click.option("--rate", type=float, default=120.0, show_default=True,
              envvar="REFRESH_RATE_SECONDS",
              help="Base seconds between outbound ANSM fetches on the CRAWLER lane "
                   "(env REFRESH_RATE_SECONDS). A random 0..min(rate,10)s jitter is "
                   "added. Default 120 (~2 min) is a gentle background trickle; "
                   "on-demand clicks use the separate, faster --demand-rate.")
@click.option("--demand-rate", type=float, default=5.0, show_default=True,
              envvar="REFRESH_DEMAND_RATE_SECONDS",
              help="Base seconds between outbound ANSM fetches on the ON-DEMAND lane "
                   "(button/auto), decoupled from the crawler so a click is not stuck "
                   "behind it (env REFRESH_DEMAND_RATE_SECONDS). A lone click after an "
                   "idle period fetches almost at once; this only spaces out bursts of "
                   "distinct pages. Keep it small but non-zero to stay polite.")
@click.option("--min-interval", type=float, default=3600.0, show_default=True,
              envvar="REFRESH_MIN_INTERVAL_SECONDS",
              help="Per-CIS anti-hammer floor: a drug refreshed more recently than "
                   "this many seconds is reported 'fresh' without re-fetching "
                   "(env REFRESH_MIN_INTERVAL_SECONDS).")
@click.option("--queue-max", type=int, default=200, show_default=True,
              envvar="REFRESH_QUEUE_MAX",
              help="Max pending ON-DEMAND refreshes; further requests get 'busy' "
                   "(env REFRESH_QUEUE_MAX). The crawler is unbounded (it holds no queue).")
@click.option("--crawl/--no-crawl", default=True, show_default=True,
              envvar="REFRESH_CRAWL",
              help="Run the perpetual background crawler that freshens every page in "
                   "frequency order on its own slow --rate lane (env REFRESH_CRAWL). "
                   "--no-crawl leaves only the button/auto on-demand refreshes.")
@click.option("--crawl-ttl-days", type=int, default=365, show_default=True,
              envvar="REFRESH_CRAWL_TTL_DAYS",
              help="Crawler staleness threshold: it refreshes any page whose captured "
                   "copy is older than this, then idles until the oldest one ages past "
                   "it again (env REFRESH_CRAWL_TTL_DAYS). Default 365 (~12 months).")
@click.option("--eu-crawl/--no-eu-crawl", default=True, show_default=True,
              envvar="REFRESH_EMA_CRAWL",
              help="Run the perpetual crawler for the EMA /eu/ pages, on its own slow "
                   "--eu-rate lane and separate manifest (env REFRESH_EMA_CRAWL). The "
                   "EMA is strict about automated access, so keep it slow. --no-eu-crawl "
                   "leaves /eu/ pages to the on-demand button + manual scrape-ema.py.")
@click.option("--eu-rate", type=float, default=300.0, show_default=True,
              envvar="REFRESH_EMA_RATE_SECONDS",
              help="Base seconds between outbound EMA fetches on the /eu/ crawler lane "
                   "(env REFRESH_EMA_RATE_SECONDS). Default 300 (~5 min): a deliberately "
                   "gentle trickle, since the EMA is strict about scraping.")
@click.option("--eu-crawl-ttl-days", type=int, default=180, show_default=True,
              envvar="REFRESH_EMA_CRAWL_TTL_DAYS",
              help="Staleness threshold for the /eu/ crawler (env "
                   "REFRESH_EMA_CRAWL_TTL_DAYS). Default 180 (~6 months): EMA SmPC PDFs "
                   "change rarely, so a long window keeps EMA traffic minimal.")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request HTTP timeout in seconds.")
@click.option("--gzip/--no-gzip", "gzip_overlay", default=True, show_default=True,
              envvar="RCP_OVERLAY_GZIP",
              help="Store overlays gzip-compressed (matches scrape-rcp.py; env RCP_OVERLAY_GZIP).")
@click.option("--user-agent", "user_agent", default=None,
              help="Override the HTTP User-Agent sent to the ANSM site.")
@click.option("--embed-notify-url", default="http://embed:8461", show_default=True,
              envvar="EMBED_NOTIFY_URL",
              help="Base URL of the sibling embed service to ping after a fresh "
                   "overlay lands, so it re-embeds the page for semantic search "
                   "(env EMBED_NOTIFY_URL). Best-effort and content-hash gated on "
                   "the embed side; set empty to disable (the embedder is optional "
                   "and its own reconcile sweep is the backstop).")
@click.option("--log-level", default="INFO", show_default=True, envvar="REFRESH_LOG_LEVEL",
              type=click.Choice(
                  ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
                  case_sensitive=False),
              help="Minimum log level (env REFRESH_LOG_LEVEL). Default INFO keeps the "
                   "per-request DEBUG chatter (status polls, refresh POSTs) out of the "
                   "logs; the /api/health check is never logged at any level.")
def main(host: str, port: int, rate: float, demand_rate: float, min_interval: float,
         queue_max: int, crawl: bool, crawl_ttl_days: int, eu_crawl: bool,
         eu_rate: float, eu_crawl_ttl_days: int, timeout: float,
         gzip_overlay: bool, user_agent: str | None, embed_notify_url: str,
         log_level: str) -> None:
    """Run the RCP refresh service (see module docstring)."""
    global REFRESHER
    # Replace loguru's default DEBUG sink with one at the chosen level, so the
    # noisy per-request lines (and the every-30s healthcheck) stay out of the
    # container logs unless someone raises the level for troubleshooting.
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper())
    ua = user_agent or ("justelesRCP-refresh/1.0 (RCP freshness bot; "
                        "contact hedv10g9@mailer.me)")
    # The on-demand and crawler workers each build/serve on their own thread; the
    # crawler builds its frequency-ordered page list inside its worker, so the HTTP
    # server below starts accepting (and /api/health passes) without waiting on I/O.
    REFRESHER = Refresher(rate=rate, demand_rate=demand_rate, min_interval=min_interval,
                          timeout=timeout, gzip_overlay=gzip_overlay, user_agent=ua,
                          queue_max=queue_max, crawl=crawl, crawl_ttl_days=crawl_ttl_days,
                          eu_crawl=eu_crawl, eu_rate=eu_rate, eu_crawl_ttl_days=eu_crawl_ttl_days,
                          embed_notify_url=embed_notify_url)
    REFRESHER.start()

    # SIGHUP arms a full re-crawl of both lanes without dropping any overlays
    # (deploy.sh --rebuild sends it once the container is up). The handler stays
    # tiny and lock-free (request_recrawl only flips per-lane flags + Events); the
    # workers do the sweeping. SIGHUP's default action would kill the process, so
    # installing this handler is what keeps `docker kill --signal=SIGHUP` from
    # stopping the container.
    def _on_recrawl(signum, frame):
        armed = REFRESHER.request_recrawl()
        logger.info("SIGHUP: armed a full re-crawl (~{} page(s)); overlays keep "
                    "serving until each is re-fetched", armed)
    signal.signal(signal.SIGHUP, _on_recrawl)

    logger.info("refresh service on {}:{} (crawl-rate {}s, demand-rate {}s, "
                "min-interval {}s, overlay={}, rcp-crawl={}, eu-crawl={}, embed-notify={})",
                host, port, rate, demand_rate, min_interval,
                "gzip" if gzip_overlay else "plain",
                f"on ttl={crawl_ttl_days}d" if crawl else "off",
                f"on rate={eu_rate}s ttl={eu_crawl_ttl_days}d" if eu_crawl else "off",
                embed_notify_url or "off")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

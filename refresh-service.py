# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "lxml", "brotli", "loguru", "click"]
# ///
"""Tiny companion service: on-demand, single-CIS RCP refresh.

The public site stays 100% static: Caddy serves precomputed files read-only and
nothing dynamic runs to render a page. This service is the ONE runtime piece,
deliberately minimal, behind two things:

  * the "Rafraichir maintenant" button on an RCP page, and
  * the automatic background refresh a page older than a year triggers on load.

It exposes a handful of JSON endpoints (see ``_Handler``); the important one is
``POST /api/refresh/<cis>``. That re-scrapes ONE drug from the live ANSM site,
writes/refreshes its overlay (``data/rcp/<cis>.html[.gz]``), re-renders just that
page into ``dist/`` (so Caddy serves the fresh bytes, precompressed siblings and
all), and updates the scrape manifest. Everything else on the site is untouched.

Politeness is enforced HERE, never trusted to the caller (a user-triggered live
scraper is exactly what a government site would read as abuse):

  * a GLOBAL rate limit serialises every outbound ANSM fetch (one worker thread,
    a minimum gap + jitter between requests), so no amount of clicking or traffic
    can exceed a steady, gentle trickle; and
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
import sys
import threading
import time
from datetime import datetime, timezone
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

CIS_RE = re.compile(r"\d{8}")
# Trigger sources tracked for the crawl stats: a manual button click ("user"),
# the >1-year auto-refresh a page fires on load ("auto"), and the optional
# startup batch ("startup", set internally). Any other value a caller passes is
# treated as "auto".
_SOURCES = ("user", "auto", "startup")


def _asof_today() -> str:
    """Today's date (UTC) as ``YYYY-MM-DD`` for stamping a freshly scraped page."""
    return datetime.now(timezone.utc).date().isoformat()


class Refresher:
    """Serialises on-demand refreshes behind one rate-limited worker thread.

    A single worker means every outbound fetch is naturally ordered; the worker
    sleeps ``rate`` seconds (+ jitter) between fetches so the aggregate request
    rate to ANSM is bounded no matter how many callers enqueue work. A CIS already
    queued or in flight is not enqueued again (dedup), and a CIS refreshed within
    ``min_interval`` seconds is reported "fresh" without any fetch at all.
    """

    def __init__(self, *, rate: float, min_interval: float, timeout: float,
                 gzip_overlay: bool, user_agent: str, queue_max: int) -> None:
        self.rate = rate
        self.min_interval = min_interval
        self.timeout = timeout
        self.gzip_overlay = gzip_overlay
        self.user_agent = user_agent
        self._queue: queue.Queue[str] = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        # cis -> trigger source ("user" | "auto" | "startup") while queued/in flight
        self._pending: dict[str, str] = {}
        self._manifest = scrape.load_manifest()
        self._last_fetch_monotonic = 0.0
        # Crawl statistics, surfaced in the logs and at GET /api/stats. Counts of
        # *completed* refreshes by outcome (ok/empty/error) and by trigger source
        # (user = button, auto = >1yr page-load refresh, startup = boot batch),
        # plus request-level short-circuits (fresh = min-interval hit, busy = queue
        # full). ok+empty+error == user+auto+startup by construction.
        self._stats = {"ok": 0, "empty": 0, "error": 0,
                       "user": 0, "auto": 0, "startup": 0,
                       "fresh": 0, "busy": 0}
        # Startup freshening batch progress (see enqueue_startup_batch): total
        # enqueued, how many have completed, and the monotonic start for the ETA.
        self._batch_total = 0
        self._batch_done = 0
        self._batch_start = 0.0
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
        # backlink to a pageless CIS (which would 404, e.g. HELICOBACTER).
        page_cis = build.page_cis_from_dist()
        xref = build.build_xref_index(names, page_cis)
        logger.info(
            "primed render: {} names, {} pages, {} backlink terms",
            len(names), len(page_cis), len(xref),
        )
        build._init_worker(names, tpl, xref)
        self._worker = threading.Thread(target=self._run, name="refresher", daemon=True)

    def start(self) -> None:
        self._worker.start()

    # -- state helpers -------------------------------------------------------

    def _entry(self, cis: str) -> dict | None:
        with self._lock:
            entry = self._manifest.get(cis)
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
        """Rough seconds to drain ``n`` queued fetches at the global rate limit.

        Each fetch waits the base rate plus, on average, half the 0..min(rate,10)s
        jitter, plus ~1s for the request itself. Good enough for an ETA hint.
        """
        return n * (self.rate + min(self.rate, 10.0) / 2 + 1.0)

    def stats(self) -> dict:
        """Snapshot of the crawl counters (served at GET /api/stats)."""
        with self._lock:
            snap = dict(self._stats)
            queued = self._queue.qsize()
            pending = len(self._pending)
            batch = {"total": self._batch_total, "done": self._batch_done}
        snap["done"] = snap["ok"] + snap["empty"] + snap["error"]
        snap["queued"] = queued
        snap["pending"] = pending
        snap["eta_seconds"] = round(self._eta_seconds(queued), 1)
        snap["startup_batch"] = batch
        return snap

    # -- public API ----------------------------------------------------------

    def request(self, cis: str, source: str = "auto") -> dict:
        """Enqueue a refresh for one CIS; return a small status dict.

        ``source`` is the trigger, tallied for the crawl stats: "user" (button),
        "auto" (the >1-year page-load refresh) or "startup" (boot batch); any
        other value is treated as "auto". A manual click on an already-queued
        item upgrades the recorded source to "user" so the stats credit the human
        action.

        ``fresh``  - refreshed within min_interval, nothing to do.
        ``queued`` - accepted (either just now or already in flight).
        ``busy``   - the work queue is full; the caller should retry later.
        """
        source = source if source in _SOURCES else "auto"
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
                if source == "user":
                    self._pending[cis] = "user"  # a click upgrades a queued auto/startup item
            else:
                try:
                    self._queue.put_nowait(cis)
                except queue.Full:
                    self._stats["busy"] += 1
                    return {"status": "busy"}
                self._pending[cis] = source
                pending_n = self._queue.qsize()
        if not already:
            logger.info("queued {} [{}] (pending={})", cis, source, pending_n)
        return {"status": "queued", "asof": self.asof_of(cis)}

    # -- worker --------------------------------------------------------------

    def _throttle(self) -> None:
        """Block until at least ``rate`` (+ jitter) seconds since the last fetch."""
        gap = self.rate + scrape.random.uniform(0.0, min(self.rate, 10.0))
        wait = self._last_fetch_monotonic + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_fetch_monotonic = time.monotonic()

    def _run(self) -> None:
        headers = {"User-Agent": self.user_agent}
        with scrape.httpx.Client(follow_redirects=True, timeout=self.timeout,
                                 headers=headers) as client:
            while True:
                cis = self._queue.get()
                with self._lock:
                    source = self._pending.get(cis, "auto")
                try:
                    self._process(client, cis, source)
                except Exception as exc:  # never let the worker thread die
                    logger.error("refresh {} [{}] failed: {}", cis, source, exc)
                    with self._lock:
                        self._manifest[cis] = {"last_fetch": scrape._now_iso(),
                                               "status": "error", "error": str(exc)[:200]}
                    self._persist_manifest()
                    self._record(cis, source, "error", f"ERROR {str(exc)[:120]}")
                finally:
                    with self._lock:
                        self._pending.pop(cis, None)
                    self._queue.task_done()

    def _persist_manifest(self) -> None:
        """Persist the scrape manifest, best-effort and NEVER fatal.

        The manifest is only a TTL cache (build.py re-derives each page's capture
        date from the overlay itself), so a failure to write it must not sink a
        refresh. It is snapshotted under the lock and written outside it, and is
        always called AFTER the page has been re-rendered, so the user-visible
        page update lands even when /app/data cannot be written. save_manifest
        already falls back to an in-place write when its atomic temp+rename cannot
        work (an EROFS .tmp on the read-only refresh rootfs); this additionally
        swallows even that fallback failing, logging instead of raising.
        """
        with self._lock:
            snapshot = dict(self._manifest)
        try:
            scrape.save_manifest(snapshot)
        except OSError as exc:
            logger.warning("could not persist scrape manifest ({}); "
                           "refresh already applied", exc)

    def _record(self, cis: str, source: str, outcome: str, result: str) -> None:
        """Tally one completed refresh and emit its progress line.

        ``outcome`` is 'ok' | 'empty' | 'error'. A startup-batch item logs a
        progress bar (done/total + ETA over the batch); an on-demand item logs
        the live queue depth + ETA instead. A compact aggregate line follows at
        the first completion, every 10th, and whenever the queue drains, so the
        overall run is visible at INFO without the per-request DEBUG chatter.
        """
        with self._lock:
            self._stats[outcome] += 1
            self._stats[source] += 1
            if source == "startup":
                self._batch_done += 1
            snap = dict(self._stats)
            batch_done, batch_total = self._batch_done, self._batch_total
            to_go = self._queue.qsize()
        done = snap["ok"] + snap["empty"] + snap["error"]
        if source == "startup" and batch_total:
            logger.info("{} | startup {} -> {}",
                        scrape._progress(batch_done, batch_total, self._batch_start),
                        cis, result)
        else:
            logger.info("refreshed {} [{}] -> {} | to-go={} eta {}", cis, source,
                        result, to_go, scrape._fmt_dur(self._eta_seconds(to_go)))
        if done == 1 or done % 10 == 0 or to_go == 0:
            logger.info(
                "stats | done={} (startup={} auto={} user={}) ok={} empty={} err={} "
                "| to-go={} eta {}",
                done, snap["startup"], snap["auto"], snap["user"],
                snap["ok"], snap["empty"], snap["error"],
                to_go, scrape._fmt_dur(self._eta_seconds(to_go)),
            )

    def _process(self, client, cis: str, source: str) -> None:
        self._throttle()
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
        self._persist_manifest()


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
@click.option("--rate", type=float, default=2.0, show_default=True,
              envvar="REFRESH_RATE_SECONDS",
              help="Base seconds between outbound ANSM fetches, GLOBAL across all "
                   "callers (env REFRESH_RATE_SECONDS). Random 0..min(rate,10)s added.")
@click.option("--min-interval", type=float, default=3600.0, show_default=True,
              envvar="REFRESH_MIN_INTERVAL_SECONDS",
              help="Per-CIS anti-hammer floor: a drug refreshed more recently than "
                   "this many seconds is reported 'fresh' without re-fetching "
                   "(env REFRESH_MIN_INTERVAL_SECONDS).")
@click.option("--queue-max", type=int, default=200, show_default=True,
              envvar="REFRESH_QUEUE_MAX",
              help="Max pending refreshes; further requests get 'busy' (env REFRESH_QUEUE_MAX).")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request HTTP timeout in seconds.")
@click.option("--gzip/--no-gzip", "gzip_overlay", default=True, show_default=True,
              envvar="RCP_OVERLAY_GZIP",
              help="Store overlays gzip-compressed (matches scrape-rcp.py; env RCP_OVERLAY_GZIP).")
@click.option("--user-agent", "user_agent", default=None,
              help="Override the HTTP User-Agent sent to the ANSM site.")
@click.option("--log-level", default="INFO", show_default=True, envvar="REFRESH_LOG_LEVEL",
              type=click.Choice(
                  ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
                  case_sensitive=False),
              help="Minimum log level (env REFRESH_LOG_LEVEL). Default INFO keeps the "
                   "per-request DEBUG chatter (status polls, refresh POSTs) out of the "
                   "logs; the /api/health check is never logged at any level.")
def main(host: str, port: int, rate: float, min_interval: float, queue_max: int,
         timeout: float, gzip_overlay: bool, user_agent: str | None, log_level: str) -> None:
    """Run the on-demand RCP refresh service (see module docstring)."""
    global REFRESHER
    # Replace loguru's default DEBUG sink with one at the chosen level, so the
    # noisy per-request lines (and the every-30s healthcheck) stay out of the
    # container logs unless someone raises the level for troubleshooting.
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper())
    ua = user_agent or ("justelesRCP-refresh/1.0 (RCP freshness bot; "
                        "contact hedv10g9@mailer.me)")
    REFRESHER = Refresher(rate=rate, min_interval=min_interval, timeout=timeout,
                          gzip_overlay=gzip_overlay, user_agent=ua, queue_max=queue_max)
    REFRESHER.start()
    logger.info("refresh service on {}:{} (rate {}s, min-interval {}s, overlay={})",
                host, port, rate, min_interval, "gzip" if gzip_overlay else "plain")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

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
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        self._pending: set[str] = set()  # queued or currently being fetched
        self._manifest = scrape.load_manifest()
        self._last_fetch_monotonic = 0.0
        # Prime build.py's render globals (names + page template) once, so
        # render_record() can run outside its normal pool worker.
        names = build.load_names()
        tpl = (build.SRC / "rcp.html").read_text(encoding="utf-8")
        build._init_worker(names, tpl)
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

    # -- public API ----------------------------------------------------------

    def request(self, cis: str) -> dict:
        """Enqueue a refresh for one CIS; return a small status dict.

        ``fresh``  - refreshed within min_interval, nothing to do.
        ``queued`` - accepted (either just now or already in flight).
        ``busy``   - the work queue is full; the caller should retry later.
        """
        if self._recently_fetched(cis):
            return {"status": "fresh", "asof": self.asof_of(cis)}
        with self._lock:
            if cis in self._pending:
                return {"status": "queued", "asof": self.asof_of(cis)}
            try:
                self._queue.put_nowait(cis)
            except queue.Full:
                return {"status": "busy"}
            self._pending.add(cis)
        logger.info("queued {} (pending={})", cis, self._queue.qsize())
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
                try:
                    self._process(client, cis)
                except Exception as exc:  # never let the worker thread die
                    logger.error("refresh {} failed: {}", cis, exc)
                    with self._lock:
                        self._manifest[cis] = {"last_fetch": scrape._now_iso(),
                                               "status": "error", "error": str(exc)[:200]}
                        scrape.save_manifest(self._manifest)
                finally:
                    with self._lock:
                        self._pending.discard(cis)
                    self._queue.task_done()

    def _process(self, client, cis: str) -> None:
        self._throttle()
        logger.info("fetching {} from {}", cis, scrape.PAGE_URL.format(cis=cis))
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
            scrape.save_manifest(self._manifest)
        if rcp == "":
            logger.info("refreshed {} -> no RCP (empty overlay)", cis)
            return
        # Re-render just this page (writes dist/rcp/<slug>.html + .gz/.br).
        row = build.render_record((cis, rcp, asof))
        if row is None:
            logger.warning("refreshed {} but render produced nothing", cis)
        else:
            logger.info("refreshed {} -> {} ({} bytes)", cis, row["slug"], len(rcp))


class _Handler(BaseHTTPRequestHandler):
    """Minimal JSON API. Routes:

    ``POST /api/refresh/<cis>`` - enqueue a refresh; -> {status, asof?}.
    ``GET  /api/status/<cis>``  - {asof, pending} so the button can poll.
    ``GET  /api/health``        - {ok: true} for container healthchecks.
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
        logger.debug("http {} - {}", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send(200, {"ok": True})
            return
        m = re.fullmatch(r"/api/status/(\d{8})", self.path)
        if m:
            cis = m.group(1)
            self._send(200, {"asof": REFRESHER.asof_of(cis),
                             "pending": REFRESHER.is_pending(cis)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        m = re.fullmatch(r"/api/refresh/(\d{8})", self.path)
        if not m:
            self._send(404, {"error": "not found"})
            return
        result = REFRESHER.request(m.group(1))
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
def main(host: str, port: int, rate: float, min_interval: float, queue_max: int,
         timeout: float, gzip_overlay: bool, user_agent: str | None) -> None:
    """Run the on-demand RCP refresh service (see module docstring)."""
    global REFRESHER
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

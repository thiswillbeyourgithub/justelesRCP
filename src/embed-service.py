# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "onnxruntime",
#   "tokenizers",
#   "numpy",
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "loguru",
#   "click",
# ]
# ///
"""Warm server-side embedder for per-drug semantic search (see CLAUDE.md / the plan).

This is the companion of refresh-service.py: a small, hardened, read-only container
that keeps ONE ONNX encoder resident (onnx_embed.Encoder) and does two jobs behind
Caddy's ``/api/sem/*`` proxy:

1. **Query embedding** (``POST /api/sem/embed {q}``): embeds the reader's question in
   ~30 ms on the request thread and returns the int8 vector, so the browser downloads
   NO model (the previous design shipped ~120 MB per visitor). Client-side, the page's
   already-fetched ``<slug>.vec.json`` section vectors are cosine-ranked against it.
2. **Passage embedding** (background): re-embeds every CRAWLED page (an overlay in
   data/rcp or data/eu; NEVER the frozen 2022 baseline) into
   ``dist/<rcp|eu>/<slug>.vec.json`` via the SHARED build.write_vec_json, and does so
   as soon as a page is crawled (refresh-service.py notifies us) plus a periodic
   reconcile sweep. On-demand: when a reader opens the box on a not-yet-embedded page,
   ``POST /api/sem/page/<cis>`` embeds it right away (front of the queue); if the page
   has no overlay at all (still baseline), we ask refresh to crawl it first, then embed
   on the resulting notify.

Design properties that satisfy the requirements:
- Queries run on request threads (ThreadingHTTPServer), so they are served IMMEDIATELY
  and never wait behind the background page-embedding worker: "embed the query
  directly, drop/defer the crawl embedding" is structural, not scheduled.
- The "except when it's embedding the page the reader is viewing" carve-out falls out
  of the per-CIS dedup: if the worker is already embedding the viewed page, the reader's
  request finds it pending and waits for that same run instead of restarting it.
- Staleness is a CONTENT HASH baked into each .vec.json (build.raw_hash), NOT mtime, so
  a button refresh that re-fetched identical text does NOT trigger a re-embed. A cheap
  mtime gate pre-filters the reconcile sweep; the hash is the authoritative gate before
  the (costly) encode.

onnxruntime + tokenizers only (NO torch), so the image stays small. Imports build.py by
path (like refresh-service.py) and onnx_embed.py; both are import-safe. Nothing is ever
logged that contains a query's text.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import signal
import struct
import sys
import threading
import time
import urllib.request
from collections import Counter, OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click
from loguru import logger

HERE = Path(__file__).resolve().parent


def _load_module(filename: str, name: str):
    """Import a sibling script by path (matches refresh-service.py). build.py is
    import-safe (``__main__``-guarded)."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_module("build.py", "build")          # segmentation + shared vec writer
onnx_embed = _load_module("onnx_embed.py", "onnx_embed")  # warm ONNX encoder

# Overlay lanes (build.OVERLAY_LANES) and the CIS matcher (build.CIS_RE) are defined
# once in build.py; reuse them here rather than re-declaring. The reconcile sweep also
# iterates build.iter_overlay_paths, so the "which overlays exist" logic lives in one
# place.
# src values a caller may pass on /api/sem/page; anything else is treated as "user".
_SOURCES = {"user", "crawl"}

# Human labels for the queue "source" values so the logs read plainly (the raw values
# are user/crawl/sweep). "reader" = a visitor opened the search box on this page;
# "scraper" = the refresh service just (re-)crawled it and notified us; "backlog" =
# the periodic reconcile sweep working through the catalog. A query is NOT a queue
# source (queries run on request threads, never enqueued); it is counted separately.
_SOURCE_LABEL = {"user": "reader", "crawl": "scraper", "sweep": "backlog"}

# Emit a rolling-aggregate progress line every N background page embeds, so throughput
# + RAM are visible even when no reconcile pass has logged recently.
_AGG_EVERY = 50


def _rss_mb() -> float | None:
    """Current resident set size (MB) of this process, from /proc/self/status. None on
    a platform without it (the container is Linux). No psutil dependency."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # value is in kB
    except (OSError, ValueError):
        return None
    return None


def _peak_rss_mb() -> float | None:
    """High-water-mark RSS (MB) via getrusage: the largest the process ever grew to, so
    a plateau after warm-up shows the model+arena ceiling. None if unavailable."""
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None
    return kb / 1024.0 if sys.platform != "darwin" else kb / (1024.0 * 1024.0)


def _mb(x: float | None) -> str:
    return f"{x:.0f} MB" if x is not None else "n/a"


def _rate(chars_per_s: float) -> str:
    """Compact 'characters embedded per second'."""
    if chars_per_s >= 1000:
        return f"{chars_per_s / 1000:.1f}k c/s"
    return f"{chars_per_s:.0f} c/s"


class Embedder:
    """Owns the warm encoder + a single background page-embedding worker with a
    priority queue (on-demand/notify at the front, the reconcile sweep at the back)."""

    def __init__(self, encoder, model: str, *, backlog: bool, backlog_rate: float,
                 reconcile_seconds: float, queue_max: int, refresh_url: str,
                 timeout: float, min_chars: int, max_chars: int,
                 max_concurrent_queries: int = 8, query_wait: float = 2.0,
                 sem_floor: float = 0.0, model_rss: float | None = None) -> None:
        self.encoder = encoder
        self.model = model
        # Candidate relevance floor (raw cosine) applied CLIENT-SIDE: a section below it is
        # not a search candidate. Ranking runs in the browser, so we only carry the value
        # and hand it out in each embed response (see embed_query); the gate itself lives
        # in src/rcp-semsearch.js. Clamped to [-1, 1] (cosine range); 0.0 keeps everything.
        self.sem_floor = min(1.0, max(-1.0, float(sem_floor)))
        # RSS (MB) right after the model loaded, so a per-embed line can report how much
        # the process has grown BEYOND the resident weights (the encode activations /
        # onnxruntime arena). Set from main() where both readings are taken.
        self._model_rss = model_rss
        self.backlog = backlog
        self.backlog_rate = max(0.0, backlog_rate)
        self.reconcile_seconds = max(5.0, reconcile_seconds)
        self.queue_max = max(1, queue_max)
        self.refresh_url = (refresh_url or "").rstrip("/")
        self.timeout = timeout
        self.min_chars = max(1, min_chars)
        self.max_chars = max(self.min_chars, max_chars)

        # Bound how many query encodes run at once so a flood of same-origin query POSTs
        # can't pin every core (each request runs on its own ThreadingHTTPServer thread,
        # which is otherwise unbounded). A brief wait absorbs normal bursts; past that a
        # request is shed with 503 rather than piling onto the CPU. Per-IP rate limiting
        # still belongs at the upstream proxy (see docs).
        self.query_wait = max(0.0, query_wait)
        self._query_slots = threading.BoundedSemaphore(max(1, int(max_concurrent_queries)))

        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._pending: "OrderedDict[str, str]" = OrderedDict()  # cis -> source
        self._running: str | None = None
        self._wake = threading.Event()
        self._stats = {"embedded": 0, "skipped": 0, "errors": 0,
                       "queries": 0, "queries_shed": 0, "crawl_triggered": 0,
                       "chars": 0, "embed_seconds": 0.0}
        # Monotonic start mark: the public /api/sem/summary reports uptime, and it tells
        # the /status reader the counters above are "since last reboot".
        self._started = time.monotonic()
        # Snapshot of the last reconcile scan (how many crawled overlays exist and how
        # many still lack a fresh .vec.json), so the /status page can answer "is the
        # embedder behind the crawler?" without an expensive per-request full scan.
        self._last_scan: dict = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._worker, name="embed-worker", daemon=True).start()
        if self.backlog:
            threading.Thread(target=self._reconcile_loop, name="embed-reconcile",
                             daemon=True).start()

    # -- path / staleness helpers -----------------------------------------
    def _overlay_for(self, cis: str):
        """(raw, subdir) for a CRAWLED page (non-empty overlay in data/rcp or data/eu),
        else None. A zero-byte overlay ("scraped, no RCP") and a baseline-only page both
        return None (not embeddable)."""
        for subdir, odir in build.OVERLAY_LANES:
            path = build._overlay_path(cis, odir)
            if path is None:
                continue
            raw = build._read_overlay(path)
            if raw.strip():
                return raw, subdir
            return None  # zero-byte sentinel: scraped, no body
        return None

    # Page resolution + vec-meta reading live in build.py (shared with embed-rcp.py's
    # offline pre-bake, so the two never disagree); these are thin adapters.
    def _dist_page(self, cis: str, subdir: str) -> Path | None:
        return build.dist_page_for(cis, subdir)

    @staticmethod
    def _vec_path(page: Path) -> Path:
        return build.vec_path_for(page)

    def _read_vec_meta(self, vec: Path) -> dict | None:
        """{src_hash, model} baked into an existing .vec.json (or its .gz), else None."""
        return build.read_vec_meta(vec)

    def _is_embedded(self, cis: str) -> bool:
        """The page has a .vec.json whose baked src_hash + model match its CURRENT
        overlay (so a reader can search it right now)."""
        ov = self._overlay_for(cis)
        if ov is None:
            return False
        raw, subdir = ov
        page = self._dist_page(cis, subdir)
        if page is None:
            return False
        meta = self._read_vec_meta(self._vec_path(page))
        return bool(meta and meta["src_hash"] == build.raw_hash(raw)
                    and meta["model"] == self.model
                    and meta.get("dim") in (0, self.encoder.dim))

    # -- queue -------------------------------------------------------------
    def _enqueue(self, cis: str, source: str, front: bool) -> str:
        """'queued' (newly added), 'dup' (already pending/running), or 'busy' (queue
        full; only 'user' may exceed the cap)."""
        with self._lock:
            if cis == self._running or cis in self._pending:
                return "dup"
            if len(self._queue) >= self.queue_max and source != "user":
                return "busy"
            self._pending[cis] = source
            (self._queue.appendleft if front else self._queue.append)(cis)
        self._wake.set()
        return "queued"

    def _take(self) -> tuple[str, str]:
        while True:
            with self._lock:
                if self._queue:
                    cis = self._queue.popleft()
                    return cis, self._pending.get(cis, "sweep")
            self._wake.wait(timeout=5.0)
            self._wake.clear()

    def _queue_summary(self) -> tuple[int, str]:
        """(pages still waiting, 'reader=a scraper=b backlog=c') so a log line spells
        out what the queue number MEANS: how many pages are planned, split by origin."""
        with self._lock:
            depth = len(self._queue)
            counts = Counter(self._pending.values())
        breakdown = " ".join(f"{_SOURCE_LABEL.get(s, s)}={counts.get(s, 0)}"
                             for s in ("user", "crawl", "sweep"))
        return depth, breakdown

    def _log_aggregate(self) -> None:
        """Rolling throughput + RAM summary (mean chars/s over all embeds, RSS + peak),
        so speed and memory are legible without reading every per-page line."""
        with self._lock:
            s = dict(self._stats)
        secs = s["embed_seconds"]
        mean_rate = s["chars"] / secs if secs > 0 else 0.0
        depth, breakdown = self._queue_summary()
        logger.info(
            "progress: {} pages / {} chars embedded, mean {} | {} skipped, {} errors | "
            "queries {} ({} shed) | RSS {} peak {} | queue {} ({})",
            s["embedded"], s["chars"], _rate(mean_rate), s["skipped"], s["errors"],
            s["queries"], s["queries_shed"], _mb(_rss_mb()), _mb(_peak_rss_mb()),
            depth, breakdown)

    # -- the actual embedding ---------------------------------------------
    def _embed_page(self, cis: str, info: dict | None = None) -> str:
        """Embed ONE crawled page's sections into its .vec.json. Returns
        "ok"|"fresh"|"no-page"|"absent". The segment->encode->hash-gate->write core is
        build.embed_page_to_vec (shared with embed-rcp.py); here we only resolve the
        overlay (crawled-only; baseline pages return "absent"). ``info`` (optional) is
        filled with ``lane`` (rcp/eu) and, on the encode path, ``chunks``/``chars`` so
        the worker can log the page + throughput."""
        ov = self._overlay_for(cis)
        if ov is None:
            return "absent"
        raw, subdir = ov
        if info is not None:
            info["lane"] = subdir
        return build.embed_page_to_vec(cis, raw, subdir, self.encoder,
                                       model=self.model, stats=info)

    def _worker(self) -> None:
        while True:
            cis, source = self._take()
            with self._lock:
                self._running = cis
            result = "error"
            info: dict = {}
            t0 = time.perf_counter()
            try:
                result = self._embed_page(cis, info)
            except Exception as exc:  # never let one bad page kill the worker
                logger.warning("embed cis {} failed: {}", cis, exc)
            dt = time.perf_counter() - t0
            embedded_n = None
            with self._lock:
                self._running = None
                self._pending.pop(cis, None)
                if result == "ok":
                    self._stats["embedded"] += 1
                    self._stats["chars"] += info.get("chars", 0)
                    self._stats["embed_seconds"] += dt
                    embedded_n = self._stats["embedded"]
                elif result == "error":
                    self._stats["errors"] += 1
                else:
                    self._stats["skipped"] += 1
            if result == "ok":
                chars = info.get("chars", 0)
                rss = _rss_mb()
                act = rss - self._model_rss if (rss is not None and self._model_rss) else None
                depth, breakdown = self._queue_summary()
                logger.info(
                    "embedded {} [{}] {} chars / {} chunks in {} ms = {} | src={} | "
                    "RSS {}{} | queue {} ({})",
                    cis, info.get("lane", "?"), chars, info.get("chunks", 0),
                    round(dt * 1000), _rate(chars / dt if dt > 0 else 0.0),
                    _SOURCE_LABEL.get(source, source), _mb(rss),
                    f" act +{act:.0f} MB" if act is not None else "", depth, breakdown)
                if embedded_n and embedded_n % _AGG_EVERY == 0:
                    self._log_aggregate()
            # Space out ONLY the background sweep so queries + on-demand keep the CPU.
            if source == "sweep" and self.backlog_rate:
                time.sleep(self.backlog_rate)

    # -- reconcile sweep ---------------------------------------------------
    def _scan_and_enqueue(self, check_model: bool = False) -> int:
        """Enqueue every crawled page whose .vec.json is missing or stale (cheap
        stat-only mtime gate via build.vec_is_fresh; the src_hash+model hash is the
        authoritative gate in _embed_page). Backstop for missed notifies + manual
        scrape-*.py runs. ``check_model`` (the first pass, see _reconcile_loop) also
        re-embeds mtime-fresh pages whose baked model OR served width (EMBED_OUT_DIM)
        differs from the current one, so a model/dim swap isn't hidden by the mtime gate
        forever."""
        queued = 0
        overlays = 0  # crawled pages seen on disk this pass
        stale = 0     # of those, how many lack a fresh .vec.json (the embed backlog)
        for cis, ov, subdir in build.iter_overlay_paths():
            page = self._dist_page(cis, subdir)
            if page is None:
                continue
            overlays += 1
            vec = self._vec_path(page)
            if build.vec_is_fresh(vec, ov, self.model, check_model=check_model,
                                  dim=self.encoder.dim):
                continue
            stale += 1
            if self._enqueue(cis, "sweep", front=False) == "queued":
                queued += 1
        # Record for /api/sem/summary: total crawled pages vs how many still await an
        # embed, so the /status page can show whether embedding trails the crawl.
        self._last_scan = {"overlays": overlays, "stale": stale,
                           "at": time.monotonic()}
        return queued

    def _reconcile_loop(self) -> None:
        # The FIRST pass also reads each mtime-fresh vec's baked model so a model swap
        # (which leaves the vec newer than its unchanged overlay) is re-embedded; later
        # passes are the cheap mtime-only gate.
        check_model = True
        while True:
            try:
                n = self._scan_and_enqueue(check_model=check_model)
                if n:
                    depth, breakdown = self._queue_summary()
                    logger.info("reconcile: queued {} stale page(s) | queue {} ({})",
                                n, depth, breakdown)
            except Exception as exc:
                logger.warning("reconcile scan failed: {}", exc)
            # While a backlog is draining, emit the throughput + RAM summary each pass so
            # speed/memory stay visible even between the every-50-pages worker aggregates.
            if self._queue:
                self._log_aggregate()
            # Bound how long cached query hashes+vectors linger even while idle: the
            # lazy per-request purge only fires when a query arrives, so sweep here too.
            self.encoder.purge_expired_queries()
            check_model = False
            time.sleep(self.reconcile_seconds)

    # -- request handlers (called from the HTTP threads) -------------------
    def request_page(self, cis: str, source: str = "user") -> dict:
        """Ensure this page gets embedded. Returns {status}:
        fresh|queued|crawling|unavailable|busy."""
        source = source if source in _SOURCES else "user"
        ov = self._overlay_for(cis)
        if ov is None:
            # No overlay yet (baseline-only): ask refresh to crawl it; the resulting
            # overlay-write notify (or the reconcile sweep) then drives the embed.
            return {"status": self._trigger_crawl(cis)}
        raw, subdir = ov
        page = self._dist_page(cis, subdir)
        if page is None:
            return {"status": "unavailable"}
        if self._is_embedded(cis):
            return {"status": "fresh"}
        # user-viewed and crawl-notify both jump the sweep (front); reader waits least.
        status = self._enqueue(cis, source, front=True)
        result = "queued" if status == "dup" else status
        # Surface on-demand work (a reader opened the box, or the scraper notified us);
        # DEBUG for a duplicate, since it just re-hit an already-planned page.
        logger.log("DEBUG" if status == "dup" else "INFO",
                   "page requested {} [{}] src={} -> {}", cis, subdir,
                   _SOURCE_LABEL.get(source, source), result)
        return {"status": result}

    def status_page(self, cis: str) -> dict:
        with self._lock:
            pending = cis == self._running or cis in self._pending
        return {"embedded": self._is_embedded(cis), "pending": pending}

    def _trigger_crawl(self, cis: str) -> str:
        if not self.refresh_url:
            return "unavailable"
        url = f"{self.refresh_url}/api/refresh/{cis}?src=user"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
        except Exception as exc:  # refresh off / unreachable: degrade gracefully
            logger.debug("crawl trigger for {} failed: {}", cis, exc)
            return "unavailable"
        with self._lock:
            self._stats["crawl_triggered"] += 1
        return "crawling" if ok else "unavailable"

    def acquire_query_slot(self) -> bool:
        """Reserve one of the bounded concurrent-encode slots (waits up to query_wait).
        False -> the encoder is saturated, the caller should shed the request (503)."""
        if self._query_slots.acquire(timeout=self.query_wait):
            return True
        with self._lock:
            self._stats["queries_shed"] += 1
        return False

    def release_query_slot(self) -> None:
        self._query_slots.release()

    def embed_query(self, q: str) -> dict:
        """Embed ONE query -> int8 base64 vector (same wire format as the passage
        vectors, so the client dequantises both with decodeVec). The query text is
        never logged or persisted. Call under acquire_query_slot()/release_query_slot()
        so concurrent encodes stay bounded."""
        t0 = time.perf_counter()
        vec = self.encoder.encode_query(q)
        qi = build.quantize_int8(vec.tolist())
        b64 = base64.b64encode(struct.pack(f"{len(qi)}b", *qi)).decode("ascii")
        with self._lock:
            self._stats["queries"] += 1
        # LENGTH + timing only, NEVER the text (privacy). DEBUG so a query flood does not
        # spam INFO; the running count rides the periodic aggregate + /api/sem/stats.
        logger.debug("query embedded: {} chars in {} ms",
                     len(q), round((time.perf_counter() - t0) * 1000))
        return {"q": b64, "dim": len(qi), "query_prefix": self.encoder.query_prefix,
                "floor": self.sem_floor}

    def stats(self) -> dict:
        with self._lock:
            base = {"enabled": self.backlog, "model": self.model,
                    "dim": self.encoder.dim,
                    "queue": len(self._queue), "pending": len(self._pending),
                    "running": self._running, **self._stats}
        # RAM + throughput, so the operator can size the box and estimate speed without
        # scraping the logs. rss = resident now, peak = high-water, model = weights-only.
        secs = base["embed_seconds"]
        base["mean_chars_per_s"] = round(base["chars"] / secs) if secs > 0 else 0
        base["rss_mb"] = round(_rss_mb() or 0)
        base["peak_rss_mb"] = round(_peak_rss_mb() or 0)
        base["model_rss_mb"] = round(self._model_rss or 0)
        return base

    def public_summary(self) -> dict:
        """A curated, public-safe view of the embedder for the /status page, served at
        GET /api/sem/summary. Derived from stats() (single source of truth): it keeps the
        reader-facing progress (pages/queries embedded since boot, throughput, backlog)
        and the "behind the crawler?" gauge, but DROPS the server-internal memory figures
        (rss/peak/model RSS) that the detailed /api/sem/stats (blocked at the edge) keeps
        for the operator. Query CONTENT is never tracked anywhere, only counts."""
        with self._lock:
            queue, pending, running = len(self._queue), len(self._pending), self._running
            last = dict(self._last_scan)
        s = self.stats()
        summary = {
            "enabled": s["enabled"], "model": s["model"], "dim": s["dim"],
            "uptime_seconds": round(time.monotonic() - self._started, 1),
            # Since last reboot.
            "pages": {"embedded": s["embedded"], "skipped": s["skipped"],
                      "errors": s["errors"], "chars": s["chars"],
                      "mean_chars_per_s": s["mean_chars_per_s"]},
            "queries": {"embedded": s["queries"], "shed": s["queries_shed"],
                        "crawl_triggered": s["crawl_triggered"]},
            # Live embed backlog (pages waiting / in flight right now).
            "backlog": {"queue": queue, "pending": pending, "running": running},
        }
        # "Is the embedder behind the crawler?" from the last reconcile scan: total
        # crawled overlays on disk vs how many still lack a fresh vector.
        if last:
            awaiting = last.get("stale", 0)
            crawled = last.get("overlays", 0)
            summary["crawl_gap"] = {
                "crawled_pages": crawled,
                "awaiting_embed": awaiting,
                "embedded_pct": round(100.0 * (crawled - awaiting) / crawled, 1)
                                if crawled else 100.0,
                "scan_age_seconds": round(time.monotonic() - last["at"], 1),
            }
        return summary


class _Handler(BaseHTTPRequestHandler):
    """JSON API under /api/sem/*:

    ``POST /api/sem/embed`` {q}              -> {q: base64-int8 vec, dim, query_prefix}
    ``POST /api/sem/page/<cis>[?src=user|crawl]`` -> {status}
    ``GET  /api/sem/page/<cis>``             -> {embedded, pending}
    ``GET  /api/sem/stats``                  -> full counters + RAM gauge (INTERNAL:
                                                blocked at the edge)
    ``GET  /api/sem/summary``                -> curated, public-safe view for /status
    ``GET  /api/sem/health``                 -> {ok: true} (never logged)
    """

    server_version = "justelesRCP-embed"
    # Bound a stalled read so a slow client cannot pin a server thread. Applied as
    # the socket timeout by StreamRequestHandler; defence-in-depth behind Caddy.
    timeout = 60

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up before we finished writing: it navigated away, or the
            # frontend's AbortController cancelled a superseded /embed request (common,
            # since editing the query cancels the in-flight encode). There is nobody to
            # send to, so this is routine, not a fault: drop the connection instead of
            # letting it bubble up to socketserver as a per-request traceback.
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        # Healthcheck fires every 30s forever; never log it. Everything else is DEBUG,
        # so it stays quiet at INFO. The query TEXT is in the POST body, never in the
        # request line logged here, so it is never written to the logs.
        if self.path == "/api/sem/health":
            return
        logger.debug("http {} - {}", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/api/sem/health":
            self._send(200, {"ok": True})
            return
        if self.path == "/api/sem/stats":
            self._send(200, EMBEDDER.stats())
            return
        if self.path == "/api/sem/summary":  # public curated view for the /status page
            self._send(200, EMBEDDER.public_summary())
            return
        m = re.fullmatch(r"/api/sem/page/(\d{8})", self.path)
        if m:
            self._send(200, EMBEDDER.status_page(m.group(1)))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/api/sem/embed":
            self._handle_query()  # reads its own body
            return
        # /api/sem/page and unknown routes carry no body from our frontend, but drain
        # anything a client sends so a stray body can't desync the next keep-alive
        # request on this connection.
        self._drain_body()
        m = re.fullmatch(r"/api/sem/page/(\d{8})", parts.path)
        if m:
            src = parse_qs(parts.query).get("src", ["user"])[0]
            result = EMBEDDER.request_page(m.group(1), src)
            code = 429 if result.get("status") == "busy" else 200
            self._send(code, result)
            return
        self._send(404, {"error": "not found"})

    def _drain_body(self) -> None:
        """Read and discard a request body so keep-alive stays in sync. A small body is
        drained; a large or malformed one just closes the connection (our frontend sends
        none, so this is purely defensive)."""
        length = self._content_length()
        if not length:  # 0 (none) or None (malformed)
            if length is None:
                self.close_connection = True
            return
        if length > 65536:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except Exception:
            self.close_connection = True

    def _content_length(self) -> int | None:
        """Parsed Content-Length: 0 when absent, a clamped non-negative int when valid,
        or None when the header is present but not a number (a malformed header should be
        a clean 400, not an unhandled ValueError -> 500 traceback)."""
        raw = self.headers.get("Content-Length")
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    def _handle_query(self) -> None:
        length = self._content_length()
        if length is None:
            self._send(400, {"error": "invalid content-length"})
            return
        # Hard cap the raw body so a giant POST can't tie up the encoder.
        if length > 8192:
            self._send(413, {"error": "query too long"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw or b"{}")
            q = (data.get("q") or "").strip()
        except (ValueError, AttributeError):
            self._send(400, {"error": "invalid json"})
            return
        if len(q) < EMBEDDER.min_chars or len(q) > EMBEDDER.max_chars:
            self._send(400, {"error": f"query must be {EMBEDDER.min_chars}"
                                      f"-{EMBEDDER.max_chars} chars"})
            return
        # Bound concurrent encodes (acquire only AFTER validation, so a bad request
        # never holds a slot). Saturated -> 503 so the client backs off.
        if not EMBEDDER.acquire_query_slot():
            self._send(503, {"error": "busy"})
            return
        try:
            self._send(200, EMBEDDER.embed_query(q))
        finally:
            EMBEDDER.release_query_slot()


EMBEDDER: Embedder | None = None  # set in main(), read by _Handler


class _QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that treats a client hangup as routine, not an error.

    A reader who navigates away, or whose superseded /embed request the frontend's
    AbortController cancels, drops the connection mid-exchange. The default
    handle_error then dumps a BrokenPipeError/ConnectionResetError traceback per
    hangup (noise, not a fault). _send already swallows the write side; this also
    covers a reset while READING the request body. Log it at DEBUG and move on."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            logger.debug("client {} hung up mid-request", client_address)
            return
        super().handle_error(request, client_address)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default="127.0.0.1", show_default=True, envvar="EMBED_HOST",
              help="Bind address (env EMBED_HOST). Use 0.0.0.0 behind the Caddy proxy.")
@click.option("--port", type=int, default=8461, show_default=True, envvar="EMBED_PORT",
              help="Port to listen on (env EMBED_PORT).")
@click.option("--model-dir", default=str(onnx_embed.DEFAULT_MODEL_DIR), show_default=True,
              envvar="EMBED_MODEL_DIR",
              help="Directory of the ONNX model + tokenizer (env EMBED_MODEL_DIR). "
                   "Mounted read-only from ./models by scripts/download-model.sh.")
@click.option("--out-dim", type=int, default=256, show_default=True,
              envvar="EMBED_OUT_DIM",
              help="Matryoshka (MRL) embedding width to truncate to (env EMBED_OUT_DIM). "
                   "256 suits arctic-embed-l-v2.0; 0 keeps the full model width. Changing "
                   "it re-embeds the whole catalog (the width is baked into each .vec.json "
                   "and gated on, so query and passage vectors always share one width).")
@click.option("--sem-floor", type=float, default=0.0, show_default=True,
              envvar="EMBED_SEM_FLOOR",
              help="Minimum raw cosine (-1..1) for a section to be a search candidate "
                   "(env EMBED_SEM_FLOOR); the gate runs client-side, so this value is "
                   "handed to the browser in each /api/sem/embed response. 0.0 keeps every "
                   "section (only the hybrid rank + result cap prune); raise it (e.g. 0.5) "
                   "to demand real similarity.")
@click.option("--intra-threads", type=int, default=4, show_default=True,
              envvar="EMBED_INTRA_THREADS",
              help="onnxruntime intra-op threads (env EMBED_INTRA_THREADS). Query embeds "
                   "are tiny; this mainly speeds background page embedding.")
@click.option("--min-query-chars", type=int, default=5, show_default=True,
              envvar="EMBED_MIN_QUERY_CHARS",
              help="Reject queries shorter than this (env EMBED_MIN_QUERY_CHARS).")
@click.option("--max-query-chars", type=int, default=400, show_default=True,
              envvar="EMBED_MAX_QUERY_CHARS",
              help="Reject queries longer than this (env EMBED_MAX_QUERY_CHARS).")
@click.option("--query-cache", type=int, default=256, show_default=True,
              envvar="EMBED_QUERY_CACHE",
              help="Bounded LRU of query-hash -> vector (env EMBED_QUERY_CACHE), so "
                   "repeated/edited queries recompute nothing.")
@click.option("--query-cache-ttl", type=float, default=60.0, show_default=True,
              envvar="EMBED_QUERY_CACHE_TTL_SECONDS",
              help="Seconds a query-hash -> vector entry may live before it is purged "
                   "(env EMBED_QUERY_CACHE_TTL_SECONDS), bounding how long any "
                   "query-derived data is retained. 0 disables expiry (LRU only).")
@click.option("--backlog/--no-backlog", default=True, show_default=True,
              envvar="EMBED_ENABLE",
              help="Run the perpetual reconcile sweep that (re-)embeds every crawled "
                   "page whose vectors are missing/stale (env EMBED_ENABLE). --no-backlog "
                   "leaves only on-demand + notify-driven embedding.")
@click.option("--backlog-rate", type=float, default=2.0, show_default=True,
              envvar="EMBED_BACKLOG_RATE_SECONDS",
              help="Seconds to pause after each BACKGROUND (sweep) page, to leave CPU "
                   "for queries (env EMBED_BACKLOG_RATE_SECONDS). On-demand/notify embeds "
                   "are not throttled.")
@click.option("--reconcile-seconds", type=float, default=60.0, show_default=True,
              envvar="EMBED_RECONCILE_SECONDS",
              help="How often the reconcile sweep rescans overlays (env "
                   "EMBED_RECONCILE_SECONDS).")
@click.option("--queue-max", type=int, default=500, show_default=True,
              envvar="EMBED_QUEUE_MAX",
              help="Max pending pages before non-user requests get 'busy' "
                   "(env EMBED_QUEUE_MAX).")
@click.option("--max-concurrent-queries", type=int, default=8, show_default=True,
              envvar="EMBED_MAX_CONCURRENT_QUERIES",
              help="Max query encodes running at once (env EMBED_MAX_CONCURRENT_QUERIES); "
                   "a flood past this is shed with 503 so it can't pin every core. Per-IP "
                   "rate limiting still belongs at the upstream proxy.")
@click.option("--refresh-url", default="http://refresh:8460", show_default=True,
              envvar="REFRESH_TRIGGER_URL",
              help="Base URL of the refresh service, used to crawl a baseline page on "
                   "first search (env REFRESH_TRIGGER_URL). Empty disables the auto-crawl "
                   "(a baseline page then reports 'unavailable').")
@click.option("--timeout", type=float, default=15.0, show_default=True,
              help="Timeout for the outbound crawl-trigger POST to refresh.")
@click.option("--log-level", default="INFO", show_default=True, envvar="EMBED_LOG_LEVEL",
              type=click.Choice(
                  ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
                  case_sensitive=False),
              help="Minimum log level (env EMBED_LOG_LEVEL). /api/sem/health is never "
                   "logged; query text is never logged.")
def main(host, port, model_dir, out_dim, sem_floor, intra_threads, min_query_chars,
         max_query_chars, query_cache, query_cache_ttl, backlog, backlog_rate,
         reconcile_seconds, queue_max, max_concurrent_queries, refresh_url, timeout,
         log_level) -> None:
    """Run the semantic-search embedder (see module docstring)."""
    global EMBEDDER
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper())

    logger.info("loading model from {} (kept warm)", model_dir)
    baseline_rss = _rss_mb()  # process RSS before the weights load (Python + onnxruntime lib)
    try:
        encoder = onnx_embed.Encoder(
            model_dir=model_dir, model_name=onnx_embed.RUNTIME_MODEL,
            intra_threads=intra_threads, query_cache=query_cache,
            query_ttl=query_cache_ttl, out_dim=out_dim,
        )
    except FileNotFoundError as exc:
        # A misconfig, not the "feature off" path (that is: don't run this container).
        sys.exit(f"embed service: {exc}")

    model_rss = _rss_mb()  # after the InferenceSession + weights are resident
    weights = (model_rss - baseline_rss
               if (model_rss is not None and baseline_rss is not None) else None)
    logger.info("model loaded: {} (dim={}) | process RSS {} (weights ~{}, before-load {}) "
                "| intra-threads={}", onnx_embed.RUNTIME_MODEL, encoder.dim, _mb(model_rss),
                _mb(weights), _mb(baseline_rss), intra_threads)
    logger.info("log legend: a page-embed line's 'src=' is reader (a visitor opened the "
                "search box) / scraper (refresh crawled it + notified) / backlog (the "
                "reconcile sweep); 'queue N (reader=.. scraper=.. backlog=..)' is pages "
                "STILL WAITING by origin; 'act +X MB' is RSS beyond the resident weights "
                "(encode arena); queries run on request threads (not queued), timed at "
                "DEBUG and counted in the 'progress:' aggregate + /api/sem/stats")

    EMBEDDER = Embedder(
        encoder, onnx_embed.RUNTIME_MODEL, backlog=backlog, backlog_rate=backlog_rate,
        reconcile_seconds=reconcile_seconds, queue_max=queue_max, refresh_url=refresh_url,
        timeout=timeout, min_chars=min_query_chars, max_chars=max_query_chars,
        max_concurrent_queries=max_concurrent_queries, sem_floor=sem_floor,
        model_rss=model_rss,
    )
    EMBEDDER.start()

    # Ignore SIGHUP so a stray `docker kill --signal=SIGHUP` (aimed at the refresh
    # container) can never take this one down; we have nothing to re-arm here.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    logger.info("embed service on {}:{} (model={}, backlog={}, reconcile={}s, "
                "min/max query chars={}/{}, sem-floor={})", host, port,
                onnx_embed.RUNTIME_MODEL, "on" if backlog else "off", reconcile_seconds,
                min_query_chars, max_query_chars, EMBEDDER.sem_floor)
    _QuietHTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

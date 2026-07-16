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
import gzip
import importlib.util
import json
import os
import re
import signal
import struct
import sys
import threading
import time
import urllib.request
from collections import OrderedDict, deque
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

_CIS_RE = re.compile(r"^\d{8}$")
# Overlay lanes: (dist subdir, overlay dir). A CIS renders under exactly one.
_LANES = (("rcp", build.RCP_OVERLAY_DIR), ("eu", build.EU_OVERLAY_DIR))
# src values a caller may pass on /api/sem/page; anything else is treated as "user".
_SOURCES = {"user", "crawl"}


class Embedder:
    """Owns the warm encoder + a single background page-embedding worker with a
    priority queue (on-demand/notify at the front, the reconcile sweep at the back)."""

    def __init__(self, encoder, model: str, *, backlog: bool, backlog_rate: float,
                 reconcile_seconds: float, queue_max: int, refresh_url: str,
                 timeout: float, min_chars: int, max_chars: int) -> None:
        self.encoder = encoder
        self.model = model
        self.backlog = backlog
        self.backlog_rate = max(0.0, backlog_rate)
        self.reconcile_seconds = max(5.0, reconcile_seconds)
        self.queue_max = max(1, queue_max)
        self.refresh_url = (refresh_url or "").rstrip("/")
        self.timeout = timeout
        self.min_chars = max(1, min_chars)
        self.max_chars = max(self.min_chars, max_chars)

        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._pending: "OrderedDict[str, str]" = OrderedDict()  # cis -> source
        self._running: str | None = None
        self._wake = threading.Event()
        self._stats = {"embedded": 0, "skipped": 0, "errors": 0,
                       "queries": 0, "crawl_triggered": 0}

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
        for subdir, odir in _LANES:
            path = build._overlay_path(cis, odir)
            if path is None:
                continue
            raw = build._read_overlay(path)
            if raw.strip():
                return raw, subdir
            return None  # zero-byte sentinel: scraped, no body
        return None

    def _dist_page(self, cis: str, subdir: str) -> Path | None:
        hits = sorted((build.DIST / subdir).glob(f"{cis}-*.html"))
        return hits[0] if hits else None

    @staticmethod
    def _vec_path(page: Path) -> Path:
        return page.parent / (page.stem + ".vec.json")

    def _read_vec_meta(self, vec: Path) -> dict | None:
        """{src_hash, model} baked into an existing .vec.json (or its .gz), else None."""
        for p in (vec, vec.with_name(vec.name + ".gz")):
            if not p.exists():
                continue
            try:
                data = p.read_bytes()
                if p.suffix == ".gz":
                    data = gzip.decompress(data)
                d = json.loads(data)
                return {"src_hash": d.get("src_hash"), "model": d.get("model")}
            except Exception:
                return None
        return None

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
                    and meta["model"] == self.model)

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

    # -- the actual embedding ---------------------------------------------
    def _embed_page(self, cis: str) -> str:
        ov = self._overlay_for(cis)
        if ov is None:
            return "absent"
        raw, subdir = ov
        page = self._dist_page(cis, subdir)
        if page is None:
            return "no-page"  # overlay but no rendered page: nothing to attach a vec to
        vec = self._vec_path(page)
        src_hash = build.raw_hash(raw)
        meta = self._read_vec_meta(vec)
        if meta and meta["src_hash"] == src_hash and meta["model"] == self.model:
            # Unchanged (e.g. a no-op refresh rewrote identical bytes): skip the encode
            # and bump the vec mtime so the reconcile mtime-gate stops re-queuing it.
            try:
                os.utime(vec, None)
            except OSError:
                pass
            return "fresh"
        chunks = build.section_chunks(raw, cis)
        # Even with no searchable sections, persist an empty payload carrying src_hash,
        # so the hash gate marks it fresh and we don't re-embed it every sweep.
        texts = [c[2] for c in chunks]
        vecs = self.encoder.encode_passages(texts) if texts else []
        payload = build.vec_payload(chunks, vecs, self.model,
                                    self.encoder.query_prefix, src_hash)
        build.write_vec_json(vec, payload)
        return "ok"

    def _worker(self) -> None:
        while True:
            cis, source = self._take()
            with self._lock:
                self._running = cis
            result = "error"
            try:
                result = self._embed_page(cis)
            except Exception as exc:  # never let one bad page kill the worker
                logger.warning("embed cis {} failed: {}", cis, exc)
            finally:
                with self._lock:
                    self._running = None
                    self._pending.pop(cis, None)
                    if result == "ok":
                        self._stats["embedded"] += 1
                    elif result == "error":
                        self._stats["errors"] += 1
                    else:
                        self._stats["skipped"] += 1
            if result == "ok":
                logger.info("embedded {} ({}), queue={}", cis, source, len(self._queue))
            # Space out ONLY the background sweep so queries + on-demand keep the CPU.
            if source == "sweep" and self.backlog_rate:
                time.sleep(self.backlog_rate)

    # -- reconcile sweep ---------------------------------------------------
    def _scan_and_enqueue(self) -> int:
        """Enqueue every crawled page whose .vec.json is missing or older than its
        overlay (cheap stat-only mtime gate; the hash is the authoritative gate in
        _embed_page). Backstop for missed notifies + manual scrape-*.py runs."""
        queued = 0
        for subdir, odir in _LANES:
            if not odir.is_dir():
                continue
            seen: set[str] = set()
            for ov in (*odir.glob("*.html"), *odir.glob("*.html.gz")):
                cis = ov.name.split(".", 1)[0]
                if cis in seen or not _CIS_RE.match(cis):
                    continue
                seen.add(cis)
                page = self._dist_page(cis, subdir)
                if page is None:
                    continue
                vec = self._vec_path(page)
                try:
                    if vec.exists() and vec.stat().st_mtime >= ov.stat().st_mtime:
                        continue
                except OSError:
                    pass
                if self._enqueue(cis, "sweep", front=False) == "queued":
                    queued += 1
        return queued

    def _reconcile_loop(self) -> None:
        while True:
            try:
                n = self._scan_and_enqueue()
                if n:
                    logger.info("reconcile: queued {} stale page(s) (queue={})",
                                n, len(self._queue))
            except Exception as exc:
                logger.warning("reconcile scan failed: {}", exc)
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
        return {"status": "queued" if status == "dup" else status}

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

    def embed_query(self, q: str) -> dict:
        """Embed ONE query -> int8 base64 vector (same wire format as the passage
        vectors, so the client dequantises both with decodeVec). The query text is
        never logged or persisted."""
        vec = self.encoder.encode_query(q)
        qi = build.quantize_int8(vec.tolist())
        b64 = base64.b64encode(struct.pack(f"{len(qi)}b", *qi)).decode("ascii")
        with self._lock:
            self._stats["queries"] += 1
        return {"q": b64, "dim": len(qi), "query_prefix": self.encoder.query_prefix}

    def stats(self) -> dict:
        with self._lock:
            return {"enabled": self.backlog, "model": self.model,
                    "queue": len(self._queue), "pending": len(self._pending),
                    "running": self._running, **self._stats}


class _Handler(BaseHTTPRequestHandler):
    """JSON API under /api/sem/*:

    ``POST /api/sem/embed`` {q}              -> {q: base64-int8 vec, dim, query_prefix}
    ``POST /api/sem/page/<cis>[?src=user|crawl]`` -> {status}
    ``GET  /api/sem/page/<cis>``             -> {embedded, pending}
    ``GET  /api/sem/stats``                  -> counters + queue gauge
    ``GET  /api/sem/health``                 -> {ok: true} (never logged)
    """

    server_version = "justelesRCP-embed"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        m = re.fullmatch(r"/api/sem/page/(\d{8})", self.path)
        if m:
            self._send(200, EMBEDDER.status_page(m.group(1)))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/api/sem/embed":
            self._handle_query()
            return
        m = re.fullmatch(r"/api/sem/page/(\d{8})", parts.path)
        if m:
            src = parse_qs(parts.query).get("src", ["user"])[0]
            result = EMBEDDER.request_page(m.group(1), src)
            code = 429 if result.get("status") == "busy" else 200
            self._send(code, result)
            return
        self._send(404, {"error": "not found"})

    def _handle_query(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
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
        self._send(200, EMBEDDER.embed_query(q))


EMBEDDER: Embedder | None = None  # set in main(), read by _Handler


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default="127.0.0.1", show_default=True, envvar="EMBED_HOST",
              help="Bind address (env EMBED_HOST). Use 0.0.0.0 behind the Caddy proxy.")
@click.option("--port", type=int, default=8461, show_default=True, envvar="EMBED_PORT",
              help="Port to listen on (env EMBED_PORT).")
@click.option("--model-dir", default=str(onnx_embed.DEFAULT_MODEL_DIR), show_default=True,
              envvar="EMBED_MODEL_DIR",
              help="Directory of the ONNX model + tokenizer (env EMBED_MODEL_DIR). "
                   "Mounted read-only from ./models by download-model.sh.")
@click.option("--intra-threads", type=int, default=4, show_default=True,
              envvar="EMBED_INTRA_THREADS",
              help="onnxruntime intra-op threads (env EMBED_INTRA_THREADS). Query embeds "
                   "are tiny; this mainly speeds background page embedding.")
@click.option("--min-query-chars", type=int, default=10, show_default=True,
              envvar="EMBED_MIN_QUERY_CHARS",
              help="Reject queries shorter than this (env EMBED_MIN_QUERY_CHARS).")
@click.option("--max-query-chars", type=int, default=400, show_default=True,
              envvar="EMBED_MAX_QUERY_CHARS",
              help="Reject queries longer than this (env EMBED_MAX_QUERY_CHARS).")
@click.option("--query-cache", type=int, default=256, show_default=True,
              envvar="EMBED_QUERY_CACHE",
              help="Bounded LRU of query-string -> vector (env EMBED_QUERY_CACHE), so "
                   "repeated/edited queries recompute nothing.")
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
def main(host, port, model_dir, intra_threads, min_query_chars, max_query_chars,
         query_cache, backlog, backlog_rate, reconcile_seconds, queue_max,
         refresh_url, timeout, log_level) -> None:
    """Run the semantic-search embedder (see module docstring)."""
    global EMBEDDER
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper())

    logger.info("loading model from {} (kept warm)", model_dir)
    try:
        encoder = onnx_embed.Encoder(
            model_dir=model_dir, model_name=onnx_embed.RUNTIME_MODEL,
            intra_threads=intra_threads, query_cache=query_cache,
        )
    except FileNotFoundError as exc:
        # A misconfig, not the "feature off" path (that is: don't run this container).
        sys.exit(f"embed service: {exc}")

    EMBEDDER = Embedder(
        encoder, onnx_embed.RUNTIME_MODEL, backlog=backlog, backlog_rate=backlog_rate,
        reconcile_seconds=reconcile_seconds, queue_max=queue_max, refresh_url=refresh_url,
        timeout=timeout, min_chars=min_query_chars, max_chars=max_query_chars,
    )
    EMBEDDER.start()

    # Ignore SIGHUP so a stray `docker kill --signal=SIGHUP` (aimed at the refresh
    # container) can never take this one down; we have nothing to re-arm here.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    logger.info("embed service on {}:{} (model={}, backlog={}, reconcile={}s, "
                "min/max query chars={}/{})", host, port, onnx_embed.RUNTIME_MODEL,
                "on" if backlog else "off", reconcile_seconds,
                min_query_chars, max_query_chars)
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

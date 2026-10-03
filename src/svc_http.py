"""Shared plumbing for the two runtime services and the scripts that import siblings.

``refresh-service.py`` and ``embed-service.py`` each ran a tiny JSON API on
``ThreadingHTTPServer`` with the same handler helpers (JSON send, quiet logging that
never logs the healthcheck, Content-Length parsing, body draining, a socket timeout)
and the same hangup-tolerant server class. They live here once. ``load_sibling`` is
the by-path importer for the hyphenated sibling scripts (``scrape-rcp.py`` & co),
also used by ``embed-rcp.py`` and ``scrape-ema.py``.

Not a PEP 723 script: it is imported, so its deps are its importers' deps (stdlib +
loguru, which every importer already lists).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

HERE = Path(__file__).resolve().parent

# Largest stray request body _drain_body reads before giving up and closing the
# connection instead. No endpoint consumes a body except the embed query, which
# reads its own.
_DRAIN_MAX = 65536


def load_sibling(filename: str, name: str):
    """Import a sibling script of src/ by path (its ``-`` name isn't a valid import).

    The module is cached in ``sys.modules`` under ``name``, so two importers of the
    same sibling (refresh-service.py and scrape-ema.py both need scrape-rcp.py) share
    ONE instance instead of each executing its own copy. Every target guards its
    CLI/build behind ``if __name__ == '__main__'``, so importing only defines things."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # registered first, like a normal import
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def setup_logging(level: str) -> None:
    """Replace loguru's default DEBUG sink with one at ``level``, so per-request
    DEBUG chatter (status polls, the every-30s healthcheck) stays out of container
    logs unless someone lowers the level to troubleshoot."""
    logger.remove()
    logger.add(sys.stderr, level=level.upper())


class JSONHandler(BaseHTTPRequestHandler):
    """Base for the services' minimal JSON APIs. Subclasses set ``health_path`` (never
    logged) and ``server_version`` and implement the ``do_*`` routes."""

    health_path = ""
    # Bound a stalled read (a client that opens a connection and sends bytes slowly)
    # so it cannot pin a server thread indefinitely. StreamRequestHandler applies this
    # as the socket timeout. Caddy fronts us, so this is defence-in-depth against a
    # slowloris that somehow reaches a service directly.
    timeout = 60

    def _path_parts(self):
        """The request target split into path and query (``?src=...`` stripped off
        the path before route matching)."""
        return urlsplit(self.path)

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
            # frontend aborted a poll / a superseded /embed request. Nobody to send to:
            # routine, not a fault. Drop the connection instead of letting it bubble up
            # to socketserver as a per-request traceback.
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:  # route through loguru
        # The container healthcheck fires every 30s forever; logging it would bury the
        # meaningful lines, so drop it entirely (even at DEBUG). Everything else is
        # DEBUG, so it stays quiet at the default INFO level. Only the request LINE is
        # logged here, never a body (the embed query text is in the POST body).
        if self.path == self.health_path:
            return
        logger.debug("http {} - {}", self.address_string(), fmt % args)

    def _content_length(self) -> int | None:
        """Parsed Content-Length: 0 when absent, a clamped non-negative int when
        valid, None when present but not a number (a malformed header should get a
        clean 400, not an unhandled ValueError -> 500 traceback)."""
        raw = self.headers.get("Content-Length")
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    def _drain_body(self) -> None:
        """Read and discard any request body so a leftover body cannot desync the
        keep-alive connection Caddy holds to us. Capped: an over-long or malformed body
        just closes the connection instead of tying up the socket. Caddy also caps the
        body upstream (request_body), so this is the belt to that suspenders."""
        length = self._content_length()
        if not length:
            if length is None:  # malformed header: don't trust the framing, close
                self.close_connection = True
            return
        if length > _DRAIN_MAX:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except Exception:
            self.close_connection = True


class QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that treats a client hangup as routine, not an error.

    A reader who navigates away, or whose poll / superseded request is aborted, drops
    the connection mid-exchange. The default handle_error then dumps a
    BrokenPipeError/ConnectionResetError traceback per hangup (noise, not a fault).
    ``JSONHandler._send`` already swallows the write side; this also covers a reset
    while READING the request body. Log it at DEBUG and move on."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            logger.debug("client {} hung up mid-request", client_address)
            return
        super().handle_error(request, client_address)

"""Server-side umami page views for the pages an AI assistant reads.

The site's analytics are a page view the BROWSER sends. An assistant reading
/llms.txt or /md/... runs no script, so without this nobody would know whether those
pages are used. refresh-service.py, which answers them (llm_pages.py), calls
``Tracker.track`` once per such request instead.

What is sent: the public PATH (never a query string), the request's User-Agent, and
the assistant's name as umami's ``browser``, so umami's Browser breakdown reads
"Claude-User", "ChatGPT-User"... instead of "unknown". No client IP: for these
requests it is an AI provider's data centre. umami drops bot user agents unless the
umami instance sets DISABLE_BOT_CHECK.

Configured by the browser analytics' own ANALYTICS_URL (the tracker script, whose
origin also answers /api/send) and ANALYTICS_WEBSITE_ID; either unset, nothing is
sent. Each send runs in its own daemon thread with a timeout, is never retried, and
never delays the answer it is about.

Pure stdlib. The same rules, in JavaScript, are justelesdocs' server/umami.mjs.

Written by Claude Code (Opus 5.5).
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from typing import Callable, Mapping
from urllib.parse import urljoin

# Sends in flight at once, beyond which a page view is dropped: a crawler hammering
# /md/ must not pile up threads in the refresh container.
MAX_IN_FLIGHT = 8
SEND_TIMEOUT_S = 3

# User agents worth naming, first match wins. A browser matches none and is left to
# umami's own parser.
AGENTS = (
    "Claude-User", "Claude-SearchBot", "ClaudeBot", "ChatGPT-User", "OAI-SearchBot", "GPTBot",
    "Perplexity-User", "PerplexityBot", "MistralAI-User", "Gemini", "Google-Extended", "Googlebot",
    "bingbot", "Applebot", "Amazonbot", "meta-externalagent", "Bytespider", "CCBot", "DuckAssistBot",
    "curl", "Wget", "python-requests", "python-httpx", "aiohttp", "Go-http-client", "node-fetch", "undici",
)
_AGENT_RE = re.compile("|".join(re.escape(a) for a in AGENTS), re.IGNORECASE)
_HOST_RE = re.compile(r"[a-z0-9.-]+(:\d{1,5})?", re.IGNORECASE)


def agent_of(user_agent: str | None) -> str | None:
    """The name umami shows as the browser, or None for an ordinary browser."""
    found = _AGENT_RE.search(user_agent or "")
    return next(a for a in AGENTS if a.lower() == found[0].lower()) if found else None


def pageview(website_id: str, host: str, path: str, user_agent: str | None) -> dict:
    """The /api/send body for one page view. ``path`` loses any query string here."""
    payload = {"website": website_id, "hostname": re.sub(r":\d+$", "", host),
               "url": path.split("?")[0], "userAgent": (user_agent or "unknown")[:500]}
    agent = agent_of(user_agent)
    if agent:
        payload["browser"] = agent
    return {"type": "event", "payload": payload}


class Tracker:
    """A page-view sender; ``enabled`` is False, and ``track`` a no-op, without config."""

    def __init__(self, env: Mapping[str, str] = os.environ,
                 opener: Callable = urllib.request.urlopen, log: Callable[[str], None] = print):
        self.website_id = (env.get("ANALYTICS_WEBSITE_ID") or "").strip()
        url = (env.get("ANALYTICS_URL") or "").strip()
        self.endpoint = urljoin(url, "/api/send") if url.startswith(("http://", "https://")) else ""
        self.enabled = bool(self.endpoint and self.website_id)
        self._opener, self._log = opener, log
        self._slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)

    def track(self, headers: Mapping[str, str], path: str) -> threading.Thread | None:
        """Send one page view in the background; the thread, or None when skipped."""
        host = headers.get("Host") or ""
        # Same rule as the browser's tracker (ANALYTICS_DNT), for the rare person who
        # opens these pages by hand.
        if (not self.enabled or headers.get("DNT") == "1" or not _HOST_RE.fullmatch(host)
                or not self._slots.acquire(blocking=False)):
            return None
        user_agent = headers.get("User-Agent")
        body = json.dumps(pageview(self.website_id, host, path, user_agent)).encode()
        request = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": (user_agent or "unknown")[:500]})

        def send() -> None:
            try:
                self._opener(request, timeout=SEND_TIMEOUT_S).close()
            except Exception as error:  # an HTTPError too: logged, never retried
                self._log(f"umami failed for {path.split('?')[0]}: {error}")
            finally:
                self._slots.release()

        thread = threading.Thread(target=send, name="umami-send", daemon=True)
        thread.start()
        return thread

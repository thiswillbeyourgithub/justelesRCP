"""New-drug discovery: the pure pieces the refresh service's discovery thread uses.

The refresh service already re-crawls every page it knows, but before this module
it never learned about a drug that appeared AFTER the last deploy: its crawl order
is restricted to CIS that already render a page, and the catalog (CIS_bdpm.txt)
only moved when ``scripts/download-data.sh`` ran on the dev machine. So a new drug
waited for a manual deploy, and a catalog drug never scraped at all (no page yet)
waited forever.

The refresh service now, once a day (``REFRESH_DISCOVER_HOURS``):

1. downloads the LIVE BDPM files (``download_bdpm``) into ``data/live/`` (a writable
   mount, the read-only copies deploy.sh ships stay untouched) and validates them;
2. diffs the catalog against the CIS it already knew (``diff_catalog``), and on the
   very first run also picks up the catalog CIS that have no page and were never
   fetched (``backlog_cis``), a one-time catch-up shown apart on /status;
3. hands each one to the matching crawler lane (EMA when centrally authorized,
   ANSM otherwise), which fetches, renders and serves it, and keeps a record of
   every step in ``data/live/new-drugs.json`` (``DiscoveryState``) for /status.

Everything here is I/O-light and testable without the service (see
test_discover.py). The orchestration (threads, lanes, rendering) lives in
refresh-service.py. Written with Claude Code.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bdpm  # pure-stdlib sibling: read_catalog

# The live daily BDPM download ("base officielle"), the SAME source and files as
# scripts/download-data.sh (which the dev machine runs before a build).
BDPM_LIVE_URL = "https://base-donnees-publique.medicaments.gouv.fr/download/file"
BDPM_FILES = ("CIS_bdpm.txt", "CIS_COMPO_bdpm.txt", "CIS_GENER_bdpm.txt")
# A real CIS_bdpm.txt lists ~15.8k drugs. Anything far below is a truncated or
# error download, and diffing it would report thousands of "removed" drugs.
MIN_CATALOG_ROWS = 10000
# Download ceiling per file (the biggest, COMPO, is ~3 MB).
MAX_FILE_BYTES = 50 * 1024 * 1024

# A new drug the ANSM has no RCP for yet (status "empty") or whose fetch failed is
# retried on each discovery run at most every RETRY_GAP_DAYS, for RETRY_DAYS after
# it was found: a freshly authorized drug often gets its RCP a few days later.
RETRY_DAYS = 90
RETRY_GAP_DAYS = 7
# How far back /status lists the new drugs one by one.
RECENT_DAYS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).replace(microsecond=0).isoformat()


def download_bdpm(get, dest: Path) -> int:
    """Download the live BDPM files into ``dest`` and return the catalog's row count.

    ``get(url) -> bytes`` does the HTTP (the caller passes its own client, so this
    stays testable). Each file lands in a temp file first; CIS_bdpm.txt must parse
    to at least MIN_CATALOG_ROWS drugs, the optional joins must be non-empty and not
    an HTML error page. Only once every file is checked are they all renamed into
    place, so a failed run never leaves a half-updated set. A failed optional join
    keeps its previous copy. Raises (ValueError / the client's error) when the
    catalog itself fails: nothing is replaced then.
    """
    dest.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    rows = 0
    try:
        for name in BDPM_FILES:
            required = name == "CIS_bdpm.txt"
            try:
                body = get(f"{BDPM_LIVE_URL}/{name}")
                if not body or len(body) > MAX_FILE_BYTES or body.lstrip()[:1] == b"<":
                    raise ValueError(f"{name}: empty, oversized or HTML body")
            except Exception:
                if required:
                    raise
                continue  # optional join: keep the previous copy
            tmp = dest / f".{name}.tmp"
            tmp.write_bytes(body)
            staged.append((tmp, dest / name))
            if required:
                rows = len(bdpm.read_catalog(tmp))
                if rows < MIN_CATALOG_ROWS:
                    raise ValueError(f"{name}: only {rows} drugs (< {MIN_CATALOG_ROWS})")
        for tmp, final in staged:
            tmp.replace(final)
        staged = []
        return rows
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


def newest_bdpm_dir(*dirs: Path) -> Path | None:
    """The directory holding the most recent CIS_bdpm.txt among ``dirs`` (by mtime),
    or None if none has one. The service starts on whichever is newer: the copy
    deploy.sh shipped, or the one its last discovery run downloaded."""
    best, best_mtime = None, -1.0
    for d in dirs:
        try:
            mtime = (d / "CIS_bdpm.txt").stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = d, mtime
    return best


def diff_catalog(known: set[str], current: set[str]) -> tuple[list[str], list[str]]:
    """(new, removed) CIS between the previously known catalog and the current one,
    each sorted. ``known`` empty means "first run": nothing is new or removed (the
    caller seeds it), since every drug of the catalog would otherwise look new."""
    if not known:
        return [], []
    return sorted(current - known), sorted(known - current)


def backlog_cis(current: set[str], has_page: set[str], attempted: set[str]) -> list[str]:
    """Catalog CIS with no rendered page that NO lane ever tried to fetch (no entry
    in either scrape manifest): drugs added to the BDPM since the 2022 baseline that
    nobody scraped. Picked up once, on the first discovery run."""
    return sorted(current - has_page - attempted)


class DiscoveryState:
    """The discovery record, persisted as JSON (``data/live/new-drugs.json``)::

        {"last_check": iso, "last_ok": iso, "last_error": str, "last_found": iso,
         "catalog_size": int, "known": [cis, ...], "backlog_done": bool,
         "drugs": {cis: {"name", "lane": "ansm"|"ema", "kind": "new"|"backlog",
                         "found": iso, "status": "pending"|"ok"|"empty"|"error",
                         "tried": iso, "fetched": iso, "slug": str, "error": str}},
         "removed": {cis: {"name", "date": iso}},
         "ema": {"last_check", "last_ok", "last_error", "timestamp",
                 "revised": {cis: {"name", "revised": date, "found": iso,
                                   "fetched": iso}}}}

    Not thread-safe on its own: the refresh service guards it with its ``_lock``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.data.setdefault("known", [])
        self.data.setdefault("drugs", {})
        self.data.setdefault("removed", {})
        self.data.setdefault("ema", {}).setdefault("revised", {})

    @property
    def drugs(self) -> dict[str, dict]:
        return self.data["drugs"]

    def save(self) -> None:
        """Atomic write (temp + rename), so a reader never sees a torn file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.path)

    def seed_known(self, cis) -> None:
        """Before the very first run, take the catalog the service started with as
        the known one, so drugs the live catalog added since the last deploy count
        as new (not as a silent seed)."""
        if not self.data["known"]:
            self.data["known"] = sorted(cis)

    def apply_catalog(self, names: dict[str, str], lane_of, has_page: set[str],
                      attempted: set[str]) -> list[str]:
        """Diff ``names`` (the current catalog, CIS -> name) against the known one,
        record the new and removed drugs, and return the CIS to fetch now: the new
        ones, the one-time backlog (first run only), plus any earlier one still due a (re)try (see
        ``due_for_fetch``). ``lane_of(cis) -> "ansm"|"ema"``."""
        now = _iso()
        current = set(names)
        known = set(self.data["known"])
        new, removed = diff_catalog(known, current)
        kinds = {c: "new" for c in new}
        if not self.data.get("backlog_done"):
            kinds.update((c, "backlog") for c in backlog_cis(current, has_page, attempted)
                         if c not in kinds)
            self.data["backlog_done"] = True
        for cis, kind in kinds.items():
            if cis not in self.drugs:
                self.drugs[cis] = {"name": names[cis], "lane": lane_of(cis), "kind": kind,
                                   "found": now, "status": "pending"}
        for cis in removed:
            self.data["removed"][cis] = {"name": self.drugs.get(cis, {}).get("name", ""),
                                         "date": now}
        if new:
            self.data["last_found"] = now
        self.data["known"] = sorted(current)
        self.data["catalog_size"] = len(current)
        return self.due_for_fetch()

    def due_for_fetch(self, now: datetime | None = None) -> list[str]:
        """Discovered CIS to (re)hand to a lane: every one still pending (e.g. after a
        restart lost the in-memory queue), and the NEW ones that came back empty or
        failed, at most every RETRY_GAP_DAYS for RETRY_DAYS after being found (not the
        backlog: ~1k mostly RCP-less drugs retried weekly would hog the ANSM lane)."""
        now = now or _now()
        out = []
        for cis, d in self.drugs.items():
            if d.get("status") == "pending":
                out.append(cis)
                continue
            if d.get("status") not in ("empty", "error") or d.get("kind") != "new":
                continue  # the one-time backlog is not retried
            found, tried = _parse(d.get("found")), _parse(d.get("tried"))
            if found and now - found > timedelta(days=RETRY_DAYS):
                continue
            if tried and now - tried < timedelta(days=RETRY_GAP_DAYS):
                continue
            out.append(cis)
        return sorted(out)

    def note_result(self, cis: str, outcome: str, slug: str = "", error: str = "") -> bool:
        """Record a lane's outcome ('ok' | 'empty' | 'error') for a discovered CIS.
        Returns False (and changes nothing) for a CIS discovery does not track."""
        d = self.drugs.get(cis)
        if d is None:
            return False
        now = _iso()
        d["tried"] = now
        d["status"] = outcome
        if outcome == "ok":
            d["fetched"] = now
            d["slug"] = slug
            d.pop("error", None)
        elif error:
            d["error"] = error[:200]
        return True

    def search_rows(self) -> list[dict]:
        """search-index.json-shaped rows ({cis, name, slug[, eu]}) for the discovered
        drugs that now have a page, so the client search finds them before the next
        full build bakes them into search-index.json."""
        rows = []
        for cis, d in sorted(self.drugs.items()):
            if d.get("status") == "ok" and d.get("slug"):
                row = {"cis": cis, "name": d["name"], "slug": d["slug"]}
                if d.get("lane") == "ema":
                    row["eu"] = 1
                rows.append(row)
        return rows

    def summary(self, indexed=lambda d: None, now: datetime | None = None) -> dict:
        """The public /status view: when discovery last ran and found something, counts
        by kind / lane / status, and the recent new drugs one by one. ``indexed(drug)``
        tells whether a fetched page already has its semantic-search vectors (True /
        False, or None when unknown)."""
        now = now or _now()
        counts: dict[str, dict[str, dict[str, int]]] = {}
        recent = []
        cutoff = now - timedelta(days=RECENT_DAYS)
        for cis, d in self.drugs.items():
            kind, lane, status = d.get("kind", "new"), d.get("lane", "ansm"), d.get("status")
            c = counts.setdefault(kind, {}).setdefault(
                lane, {"pending": 0, "ok": 0, "empty": 0, "error": 0, "indexed": 0})
            c[status] = c.get(status, 0) + 1
            is_indexed = indexed(d) if status == "ok" and kind == "new" else None
            if is_indexed:
                c["indexed"] += 1
            if kind == "new" and (_parse(d.get("found")) or now) >= cutoff:
                recent.append({"cis": cis, "name": d.get("name", ""), "lane": lane,
                               "found": d.get("found"), "status": status,
                               "tried": d.get("tried"), "fetched": d.get("fetched"),
                               "slug": d.get("slug", ""), "indexed": is_indexed})
        recent.sort(key=lambda r: (r["found"] or "", r["cis"]), reverse=True)
        removed = sorted(({"cis": c, **v} for c, v in self.data["removed"].items()
                          if (_parse(v.get("date")) or now) >= cutoff),
                         key=lambda r: r.get("date") or "", reverse=True)
        ema = self.data["ema"]
        revised = sorted(({"cis": c, **v} for c, v in ema["revised"].items()
                          if (_parse(v.get("found")) or now) >= cutoff),
                         key=lambda r: (r.get("revised") or "", r["cis"]), reverse=True)
        return {
            "last_check": self.data.get("last_check"),
            "last_ok": self.data.get("last_ok"),
            "last_error": self.data.get("last_error", ""),
            "last_found": self.data.get("last_found"),
            "catalog_size": self.data.get("catalog_size", 0),
            "recent_days": RECENT_DAYS,
            "counts": counts,
            "recent": recent,
            "removed": removed,
            "ema": {"last_check": ema.get("last_check"), "last_ok": ema.get("last_ok"),
                    "last_error": ema.get("last_error", ""),
                    "timestamp": ema.get("timestamp", ""),
                    "revised_pending": sum(1 for r in ema["revised"].values()
                                           if not r.get("fetched")),
                    "revised": revised},
        }

    def note_revisions(self, revised: dict[str, str], names: dict[str, str]) -> None:
        """Record the /eu/ pages whose EMA PDF was revised after our last fetch
        (``{cis: revision date}``). An entry already known for the same revision
        keeps its first ``found`` date."""
        now = _iso()
        log = self.data["ema"]["revised"]
        for cis, date in revised.items():
            if log.get(cis, {}).get("revised") != date:
                log[cis] = {"name": names.get(cis, ""), "revised": date, "found": now}

    def note_revision_fetched(self, cis: str) -> None:
        entry = self.data["ema"]["revised"].get(cis)
        if entry is not None and not entry.get("fetched"):
            entry["fetched"] = _iso()


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

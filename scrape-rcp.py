# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "loguru", "httpx", "lxml"]
# ///
"""Freshness scraper for justelesRCP: refresh RCPs from the live ANSM site.

Why this exists
---------------
The only packaged bulk dump of RCP *HTML* that has ever existed is the "Défi
iDoc Santé" ``CIS_RCP.csv`` on data.gouv.fr, and it is a frozen snapshot from
2 May 2022. The official BDPM download page is refreshed near-daily but ships
only structured metadata (names, prices, compositions), never the RCP body. The
current RCP HTML lives only on each drug's page.

So to serve current RCPs without giving up the static, zero-runtime, hardened
architecture (Caddy read-only, no app server), this script runs as a background
job that scrapes drug pages and writes a per-CIS *overlay* file that ``build.py``
prefers over the 2022 baseline cell (see ``RCP_OVERLAY_DIR`` in build.py). It is
NOT a request-time renderer: nothing dynamic runs at serve time.

What it does
------------
1. Builds the CIS universe from ``data/CIS_bdpm.txt`` (the official mapping).
2. Orders candidates by a frequency list (``--frequency`` JSONL of
   ``{"term": <drug/substance name>, "score": <higher = sooner>}``) so the drugs
   people actually read are freshened first. Terms are matched to each CIS's
   denomination by accent-folded tokens; a drug that no term matches is given
   the 25th-percentile score so it still scrapes at a middling rank. Falls back
   to the CIS_bdpm file order when no list is given.
3. Skips any CIS refreshed within ``--ttl-days`` (default 30) per the scrape
   manifest, then takes the first ``--limit`` still-due CIS.
4. Fetches ``/medicament/<cis>/extrait``, extracts the RCP fragment, and writes
   an overlay: ``data/rcp/<cis>.html.gz`` by default (gzip, ``--gzip`` / env
   ``RCP_OVERLAY_GZIP``) or plain ``data/rcp/<cis>.html`` with ``--no-gzip``.
   build.py reads either format transparently, so the choice only trades disk /
   rsync size for greppability. A zero-byte file means "scraped, this drug has
   no RCP", which build.py treats as a real value and skips (not a fallback).
5. Records ``{last_fetch, hash, status, http}`` per CIS in
   ``data/.scrape-manifest.json`` for the TTL and change detection.

Politeness
----------
One request every ``--rate`` seconds (default 2.0, env ``RCP_SCRAPE_RATE_SECONDS``)
plus up to ``min(rate, 10)`` seconds of random jitter, redirects followed, an
identifying User-Agent. Set a larger ``--rate`` (e.g. 120) for a slow background
trickle of one RCP every couple of minutes. A one-time full scrape (``--all``) of
~15k drugs at 2 s each is ~8 h. Progress (a bar with elapsed/ETA) and the trigger
of each fetch ("user" for ``--only``, "timer" for the automatic queue) are logged
per drug; add finer per-step detail at DEBUG.

Routine freshening no longer needs this script: the refresh service
(``refresh-service.py``) runs a perpetual, frequency-ordered crawler that keeps the
whole catalog under its TTL on its own. This CLI stays useful for a one-time ``--all``
seed, an ad-hoc ``--only`` batch, or a scrape on a host with no refresh service.
After a run, rebuild with ``uv run build.py`` (incremental: only changed drugs
re-render), e.g. ``uv run scrape-rcp.py --all && uv run build.py``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
from loguru import logger
from lxml import html as lxml_html

import bdpm  # shared, pure-stdlib BDPM tokenising + frequency scoring

ROOT = Path(__file__).parent
DATA = ROOT / "data"
BDPM_PATH = DATA / "CIS_bdpm.txt"
# Two more optional BDPM exports (same zip as CIS_bdpm) used only to match drugs
# to the frequency list, never to build pages. They let a drug match a term via
# its active substance or reference brand even when the commercial name hides
# them (e.g. XENAZINE -> tétrabénazine, a REMINYL generic -> galantamine). Both
# are optional: absent, matching gracefully falls back to the name alone.
#   COMPO: CIS -> active-substance denominations (col 3).
#   GENER: generic-group label "SUBSTANCE ... - REFERENCE_BRAND ...", per CIS.
COMPO_PATH = DATA / "CIS_COMPO_bdpm.txt"
GENER_PATH = DATA / "CIS_GENER_bdpm.txt"
RCP_OVERLAY_DIR = DATA / "rcp"
# Manifest lives beside the data (gitignored) and drives the TTL: a CIS whose
# last_fetch is younger than --ttl-days is not re-fetched.
MANIFEST_PATH = DATA / ".scrape-manifest.json"
# Default frequency list (drug name -> priority score) used to order the scrape
# queue; copy your own here or pass --frequency. See load_frequency / the module
# docstring for the expected JSONL shape.
DEFAULT_FREQUENCY = DATA / "drugs_frequency.jsonl"

# The live drug page. The old affichageDoc.php?specid=<cis>&typedoc=R endpoint
# now 301-redirects here; httpx follows the redirect either way.
PAGE_URL = "https://base-donnees-publique.medicaments.gouv.fr/medicament/{cis}/extrait"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (used for manifest timestamps)."""
    return datetime.now(timezone.utc).isoformat()


def extract_rcp(page_html: str) -> str:
    """Extract the RCP body from a ``/medicament/<cis>/extrait`` page.

    The page is a DSFR tabbed document bundling three tabs (fiche info, RCP,
    notice). The RCP lives in ``#tabpanel-rcp-panel`` and its actual body (the
    ``AmmAnnexeTitre*`` markup) is the ``div#contenu`` content column; the
    sibling column is the site's own "Sommaire" nav, which we drop. Note that
    ``id="contenu"`` is NOT unique on the page (the notice panel has one too),
    so the lookup is scoped *inside* the RCP panel.

    The extracted body is re-wrapped in the exact envelope the 2022 dump used
    (``<div id="textDocument">...</div>``) so that ``build.clean_rcp`` handles a
    scraped overlay byte-for-byte like a baseline cell, with no build.py change.

    Parameters
    ----------
    page_html : str
        Full HTML of the drug page.

    Returns
    -------
    str
        The wrapped RCP HTML, or ``""`` when the page carries no RCP (phyto /
        homéo specialities, or an "en cours de mise à jour" placeholder), which
        the caller stores as an empty overlay file.
    """
    doc = lxml_html.fromstring(page_html)
    panels = doc.xpath("//*[@id='tabpanel-rcp-panel']")
    if not panels:
        return ""
    panel = panels[0]
    # Prefer the explicit content column; fall back to any column in the panel
    # that actually holds RCP headings, in case the id ever changes.
    columns = panel.xpath(".//div[@id='contenu']") or panel.xpath(
        ".//div[contains(@class, 'fr-col') and .//*[contains(@class, 'AmmAnnexeTitre1')]]"
    )
    if not columns:
        return ""
    body = columns[0]
    # No RCP headings -> treat as "no RCP" rather than emitting empty chrome.
    if not body.xpath(".//*[contains(@class, 'AmmAnnexeTitre1')]"):
        return ""
    # Drop DSFR interactive chrome that the content column carries around the
    # actual RCP (print/share toolbars, buttons, scripts). The RCP body itself
    # is plain AmmAnnexeTitre*/DateNotif markup with no fr-* classes, so removing
    # anything marked screen-only (fr-no-print) or interactive is safe and makes
    # the overlay match the clean 2022 dump. drop_tree() detaches in place.
    junk = body.xpath(
        ".//script | .//style | .//button | .//nav | .//form"
        " | .//*[contains(concat(' ', normalize-space(@class), ' '), ' fr-no-print ')]"
    )
    for node in junk:
        if node.getparent() is not None:  # skip nodes already removed with a parent
            node.drop_tree()
    inner = "".join(lxml_html.tostring(child, encoding="unicode") for child in body)
    return (
        '<!DOCTYPE html>\n<html>\n<body><div id="textDocument">'
        f"{inner}</div></body>\n</html>"
    )


def load_manifest() -> dict:
    """Load the scrape manifest, or an empty mapping when it is missing, empty,
    unreadable, or not a JSON object.

    The tolerant read matters for the refresh service: the manifest is
    bind-mounted as a single file into that (read-only) container, so if the
    file did not exist on the host at first ``up`` Docker silently creates a
    *directory* in its place (``read_text`` -> IsADirectoryError), and an empty
    ``touch``ed file is not valid JSON either. Either would otherwise crash the
    service at startup; degrade to an empty manifest instead (mirrors build.py's
    _load_scrape_dates). deploy.sh separately heals the directory case."""
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(manifest: dict) -> None:
    """Persist the manifest. Prefer an atomic temp-file + rename (crash-safe for
    the long batch scraper); fall back to an in-place write when that cannot work.

    The fallback exists for the refresh container: there the manifest is a single
    bind-mounted file on an otherwise read-only rootfs, so a sibling ``.tmp`` in
    ``data/`` cannot be created (EROFS) and you cannot rename onto a bind-mount
    point (EBUSY). Writing in place is not crash-atomic, but load_manifest
    tolerates a truncated/invalid manifest (returns {}), so at worst a crash
    mid-write costs the TTL cache, never correctness."""
    payload = json.dumps(manifest, ensure_ascii=False, indent=0)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(MANIFEST_PATH)
    except OSError:
        # Best-effort cleanup of a partial temp, then write in place. The unlink
        # must NOT be allowed to abort the fallback: on the read-only refresh
        # rootfs the temp was never created and unlink can raise EROFS (errno 30,
        # NOT FileNotFoundError, so missing_ok would re-raise it), which would
        # skip the in-place write entirely and lose the manifest update.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        MANIFEST_PATH.write_text(payload, encoding="utf-8")


def is_due(entry: dict | None, ttl_days: int) -> bool:
    """Return True if a CIS should be (re)fetched given its manifest entry.

    Due when never fetched, previously errored, or last fetched longer ago than
    ``ttl_days``. A malformed/absent timestamp is treated as due.
    """
    if not entry or entry.get("status") == "error":
        return True
    last = entry.get("last_fetch")
    if not last:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    except ValueError:
        return True
    return age.days >= ttl_days


def write_overlay(cis: str, rcp_html: str, gzip_overlay: bool) -> Path:
    """Write the overlay for a CIS atomically (temp file + rename); return its path.

    With ``gzip_overlay`` the RCP is stored gzip-compressed as ``<cis>.html.gz``
    (a large space/transfer win over ~15k HTML files), otherwise plain as
    ``<cis>.html``. build.py reads either format transparently, so the two are
    interchangeable and the flag can be flipped at will. To keep exactly one
    overlay per CIS, the other-format sibling is removed after writing.

    The "scraped, but no RCP" case (``rcp_html == ""``) is stored as a zero-byte
    file (never a gzip of the empty string) so both sides recognise the sentinel
    by size alone without gunzipping.
    """
    RCP_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    plain = RCP_OVERLAY_DIR / f"{cis}.html"
    gz = RCP_OVERLAY_DIR / f"{cis}.html.gz"
    dest, sibling = (gz, plain) if gzip_overlay else (plain, gz)
    if rcp_html == "":
        payload = b""  # zero-byte sentinel in either mode
    elif gzip_overlay:
        payload = gzip.compress(rcp_html.encode("utf-8"))
    else:
        payload = rcp_html.encode("utf-8")
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    sibling.unlink(missing_ok=True)  # never leave both formats for one CIS
    return dest


def fetch_one(client: httpx.Client, cis: str) -> tuple[str, int]:
    """Fetch a drug page and return its raw HTML plus the final HTTP status."""
    resp = client.get(PAGE_URL.format(cis=cis))
    return resp.text, resp.status_code


def _fmt_dur(seconds: float) -> str:
    """Format a duration as H:MM:SS, or MM:SS when under an hour."""
    s = int(max(0.0, seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _progress(done: int, total: int, start: float, width: int = 24) -> str:
    """Render a textual progress bar with percent, elapsed time and ETA.

    Emitted as a normal log line (not drawn in place) so it reads identically in
    an interactive terminal and in a cron log file, and never fights loguru for
    the current line the way an animated bar would. ETA is a simple linear
    extrapolation from the average time per drug so far (the fixed inter-request
    delay dominates, so it is a good estimate).
    """
    frac = done / total if total else 1.0
    filled = round(frac * width)
    bar = "#" * filled + "-" * (width - filled)
    elapsed = time.monotonic() - start
    eta = (elapsed / done) * (total - done) if done else 0.0
    return f"[{bar}] {frac * 100:3.0f}% {done}/{total} elapsed {_fmt_dur(elapsed)} eta {_fmt_dur(eta)}"


def build_queue(
    ttl_days: int,
    *,
    force: bool = False,
    frequency: Path | None = None,
    restrict: set[str] | None = None,
) -> list[str]:
    """Ordered list of CIS due to be (re)fetched, most-important-first.

    Single source of truth for the scrape queue, shared by this script's batch
    CLI (``main``) and the refresh service's optional startup freshening batch,
    so both order by the SAME frequency scoring and honour the SAME TTL instead
    of duplicating the logic.

    Ordering is the frequency list (popular drugs first), falling back to the
    ``CIS_bdpm`` file order when no list is present. ``restrict``, when given,
    keeps only those CIS afterwards: the refresh service passes the set of CIS
    that actually render a page, since it has no ``CIS_RCP.csv`` and must not
    enqueue a pageless drug. A CIS is "due" when it was never fetched, previously
    errored, or last fetched longer ago than ``ttl_days`` (see ``is_due``);
    ``force`` returns every candidate regardless of the manifest.
    """
    if not BDPM_PATH.exists():
        raise SystemExit(f"missing {BDPM_PATH} (run download-data.sh first)")
    catalog = bdpm.read_catalog(BDPM_PATH)
    # Order the queue by frequency score (falling back to CIS_bdpm file order
    # when no list is given): popular drugs get refreshed first.
    freq_path = frequency or (DEFAULT_FREQUENCY if DEFAULT_FREQUENCY.exists() else None)
    if freq_path:
        signals = bdpm.substance_signals(catalog, COMPO_PATH, GENER_PATH)
        single, multi, p25 = bdpm.load_frequency(freq_path)
        scores, matched = bdpm.score_catalog(signals, single, multi, p25)
        ordered = bdpm.order_by_score(catalog, scores)
        if not COMPO_PATH.exists():
            logger.warning(
                "{} absent: matching on drug names only (re-run download-data.sh "
                "to add the substance/generic join)", COMPO_PATH.name,
            )
        logger.info(
            "frequency {}: {} single + {} multi terms, p25={} fallback; "
            "{}/{} CIS matched a term ({}%)",
            freq_path.name, len(single), len(multi), p25,
            len(matched), len(catalog), round(100 * len(matched) / len(catalog)),
        )
    else:
        ordered = [cis for cis, _ in catalog]
        logger.warning("no --frequency file; using CIS_bdpm file order")
    if restrict is not None:
        keep = set(restrict)
        ordered = [c for c in ordered if c in keep]
    manifest = load_manifest()
    due = ordered if force else [c for c in ordered if is_due(manifest.get(c), ttl_days)]
    logger.info(
        "{} CIS in catalog{}, {} due (ttl={}d)",
        len(catalog),
        f", {len(ordered)} with a page" if restrict is not None else "",
        len(due), ttl_days,
    )
    return due


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--limit", type=int, default=60, show_default=True,
              help="Max drugs to (re)fetch this run. Ignored with --all/--only.")
@click.option("--all", "fetch_all", is_flag=True,
              help="Fetch every due CIS (one-time full scrape; overrides --limit).")
@click.option("--only", multiple=True, metavar="CIS",
              help="Fetch these exact CIS codes, ignoring TTL and ordering. Repeatable.")
@click.option("--ttl-days", type=int, default=30, show_default=True,
              help="Skip a CIS refreshed more recently than this many days.")
@click.option("--rate", type=float, default=2.0, show_default=True,
              envvar="RCP_SCRAPE_RATE_SECONDS",
              help="Base seconds between requests, i.e. how often to scrape one RCP "
                   "(env RCP_SCRAPE_RATE_SECONDS). A random 0..min(rate,10)s is added "
                   "to each gap so timing is not perfectly periodic.")
@click.option("--gzip/--no-gzip", "gzip_overlay", default=True, show_default=True,
              envvar="RCP_OVERLAY_GZIP",
              help="Store overlays gzip-compressed (<cis>.html.gz) instead of plain "
                   "(<cis>.html); env RCP_OVERLAY_GZIP. build.py reads either transparently.")
@click.option("--force", is_flag=True, help="Ignore the TTL for selected drugs.")
@click.option("--frequency", type=click.Path(exists=True, path_type=Path), default=None,
              help="JSONL of {term, score} priorities (higher=first); matched to drug "
                   f"names. Defaults to {DEFAULT_FREQUENCY} if present.")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request timeout in seconds.")
@click.option("--user-agent",
              default=None,
              help="Override the HTTP User-Agent sent to the ANSM site.")
def main(limit: int, fetch_all: bool, only: tuple[str, ...], ttl_days: int,
         rate: float, gzip_overlay: bool, force: bool, frequency: Path | None,
         timeout: float, user_agent: str | None) -> None:
    """Refresh RCP overlay files from the live ANSM site (see module docstring)."""
    # Identifying User-Agent with a reachable contact so ANSM can get in touch
    # (or block) rather than seeing an anonymous bot. Override with --user-agent.
    ua = user_agent or "justelesRCP-scraper/1.0 (RCP freshness bot; contact hedv10g9@mailer.me)"

    if only:
        targets = list(dict.fromkeys(only))  # dedupe, keep order
        logger.info("scraping {} explicitly requested CIS", len(targets))
    else:
        # Ordering + TTL selection is shared with the refresh service's startup
        # batch, so it lives in build_queue (single source of truth).
        due = build_queue(ttl_days, force=force, frequency=frequency)
        targets = due if fetch_all else due[:limit]
        logger.info("fetching {} this run", len(targets))

    if not targets:
        logger.info("nothing due; done")
        return

    manifest = load_manifest()
    headers = {"User-Agent": ua}
    # A run is homogeneous: either the operator explicitly asked for these CIS
    # (--only, "user") or they came off the automatic frequency queue ("timer",
    # the cron/background freshener). Every line is tagged so a user-triggered
    # refresh is always distinguishable from the timer-driven one.
    source = "user" if only else "timer"
    total = len(targets)
    logger.info(
        "fetching {} CIS [{}], overlay={}, base rate {}s + up to {}s jitter",
        total, source, "gzip" if gzip_overlay else "plain",
        rate, round(min(rate, 10.0), 1),
    )
    n_ok = n_empty = n_err = 0
    start = time.monotonic()
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        for i, cis in enumerate(targets, 1):
            logger.debug("[{}/{}] {} {}: GET {}", i, total, source, cis, PAGE_URL.format(cis=cis))
            try:
                page, status = fetch_one(client, cis)
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                logger.debug("[{}/{}] {} {}: extracting RCP from {} bytes of HTML",
                             i, total, source, cis, len(page))
                rcp = extract_rcp(page)
                dest = write_overlay(cis, rcp, gzip_overlay)
                logger.debug("[{}/{}] {} {}: wrote {} ({} bytes on disk)",
                             i, total, source, cis, dest.name, dest.stat().st_size)
                digest = hashlib.sha256(rcp.encode("utf-8")).hexdigest()
                manifest[cis] = {
                    "last_fetch": _now_iso(), "hash": digest,
                    "status": "empty" if rcp == "" else "ok", "http": status,
                }
                if rcp == "":
                    n_empty += 1
                    result = "no RCP (empty overlay)"
                else:
                    n_ok += 1
                    result = f"{len(rcp)} bytes"
                logger.info("{} | {} {} -> {}", _progress(i, total, start), source, cis, result)
            except Exception as exc:  # network / parse error: record and move on
                n_err += 1
                manifest[cis] = {"last_fetch": _now_iso(), "status": "error", "error": str(exc)[:200]}
                logger.error("{} | {} {} -> ERROR {}", _progress(i, total, start), source, cis, exc)
            # Persist periodically so a long run survives interruption.
            if i % 25 == 0:
                save_manifest(manifest)
            if i < total and rate > 0:
                # Base politeness gap + up to min(rate, 10)s of jitter so requests
                # are not perfectly periodic (gentler on the origin, and matches the
                # "~every N s +- random" background trickle the design calls for).
                time.sleep(rate + random.uniform(0.0, min(rate, 10.0)))

    save_manifest(manifest)
    logger.info("done [{}]: {} RCPs, {} empty, {} errors in {}",
                source, n_ok, n_empty, n_err, _fmt_dur(time.monotonic() - start))
    logger.info("now rebuild: uv run build.py")


if __name__ == "__main__":
    main()

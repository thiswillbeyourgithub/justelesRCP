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
2. Orders candidates by popularity (``--popularity`` file of CIS in decreasing
   sold-units order) so the drugs people actually read are freshened first;
   falls back to the CIS_bdpm file order when no list is given.
3. Skips any CIS refreshed within ``--ttl-days`` (default 30) per the scrape
   manifest, then takes the first ``--limit`` still-due CIS.
4. Fetches ``/medicament/<cis>/extrait``, extracts the RCP fragment, and writes
   ``data/rcp/<cis>.html`` (an empty file means "scraped, this drug has no RCP",
   which build.py treats as a real value and skips, not a baseline fallback).
5. Records ``{last_fetch, hash, status, http}`` per CIS in
   ``data/.scrape-manifest.json`` for the TTL and change detection.

Politeness
----------
One request every ``--rate`` seconds (default 2.0), redirects followed, a plain
identifying User-Agent. A one-time full scrape (``--all``) of ~15k drugs at 2 s
each is ~8 h; the routine cron freshener does a small ``--limit`` batch.

After a run, rebuild with ``uv run build.py`` (incremental: only changed drugs
re-render). Typical cron: ``uv run scrape-rcp.py --limit 60 && uv run build.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
from loguru import logger
from lxml import html as lxml_html

ROOT = Path(__file__).parent
DATA = ROOT / "data"
BDPM_PATH = DATA / "CIS_bdpm.txt"
RCP_OVERLAY_DIR = DATA / "rcp"
# Manifest lives beside the data (gitignored) and drives the TTL: a CIS whose
# last_fetch is younger than --ttl-days is not re-fetched.
MANIFEST_PATH = DATA / ".scrape-manifest.json"

# The live drug page. The old affichageDoc.php?specid=<cis>&typedoc=R endpoint
# now 301-redirects here; httpx follows the redirect either way.
PAGE_URL = "https://base-donnees-publique.medicaments.gouv.fr/medicament/{cis}/extrait"

# A CIS code is exactly 8 digits; used to harvest codes leniently from an
# arbitrary popularity export regardless of its column layout.
CIS_RE = re.compile(r"\b(\d{8})\b")


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
    """Load the scrape manifest, or an empty mapping if it does not exist yet."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    """Write the manifest atomically (temp file + rename) to survive crashes."""
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def read_cis_universe() -> list[str]:
    """Return all CIS codes from ``CIS_bdpm.txt`` in file order.

    The official BDPM file is latin-1, tab-separated, with the 8-digit CIS in
    the first column (same source build.load_names reads for names).
    """
    codes: list[str] = []
    with BDPM_PATH.open(encoding="latin-1") as fh:
        for line in fh:
            cis = line.split("\t", 1)[0].strip()
            if cis:
                codes.append(cis)
    return codes


def read_popularity(path: Path) -> list[str]:
    """Harvest CIS codes from a popularity export, in file (decreasing) order.

    The parser is deliberately lenient: it pulls the first 8-digit code from
    each line, so most sold-units exports keyed by CIS work without reshaping.
    Order is preserved and duplicates dropped (first occurrence wins).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = CIS_RE.search(line)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            ordered.append(match.group(1))
    return ordered


def order_candidates(universe: list[str], popularity: list[str]) -> list[str]:
    """Order the CIS universe by popularity first, then the remaining codes.

    Popularity codes not present in the universe are ignored; universe codes
    absent from the popularity list keep their original (file) order at the end.
    """
    in_universe = set(universe)
    head = [cis for cis in popularity if cis in in_universe]
    head_set = set(head)
    tail = [cis for cis in universe if cis not in head_set]
    return head + tail


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


def write_overlay(cis: str, rcp_html: str) -> None:
    """Write ``data/rcp/<cis>.html`` atomically (temp file + rename)."""
    RCP_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    dest = RCP_OVERLAY_DIR / f"{cis}.html"
    tmp = dest.with_suffix(".html.tmp")
    tmp.write_text(rcp_html, encoding="utf-8")
    tmp.replace(dest)


def fetch_one(client: httpx.Client, cis: str) -> tuple[str, int]:
    """Fetch a drug page and return its raw HTML plus the final HTTP status."""
    resp = client.get(PAGE_URL.format(cis=cis))
    return resp.text, resp.status_code


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
              help="Seconds to wait between requests (politeness).")
@click.option("--force", is_flag=True, help="Ignore the TTL for selected drugs.")
@click.option("--popularity", type=click.Path(exists=True, path_type=Path),
              help="File listing CIS in decreasing sold-units order (freshen first).")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request timeout in seconds.")
@click.option("--user-agent",
              default=None,
              help="Override the HTTP User-Agent sent to the ANSM site.")
def main(limit: int, fetch_all: bool, only: tuple[str, ...], ttl_days: int,
         rate: float, force: bool, popularity: Path | None, timeout: float,
         user_agent: str | None) -> None:
    """Refresh RCP overlay files from the live ANSM site (see module docstring)."""
    # TODO: set a real contact/repo URL in the default User-Agent so ANSM can
    # reach the operator; kept generic here to avoid hardcoding any identity.
    ua = user_agent or "justelesRCP-scraper/0.1 (RCP freshness bot)"

    if only:
        targets = list(dict.fromkeys(only))  # dedupe, keep order
        logger.info("scraping {} explicitly requested CIS", len(targets))
    else:
        if not BDPM_PATH.exists():
            raise SystemExit(f"missing {BDPM_PATH} (run download-data.sh first)")
        universe = read_cis_universe()
        pop = read_popularity(popularity) if popularity else []
        if popularity:
            logger.info("popularity list: {} CIS from {}", len(pop), popularity)
        else:
            logger.warning("no --popularity file; using CIS_bdpm file order")
        ordered = order_candidates(universe, pop)
        manifest = load_manifest()
        due = ordered if force else [c for c in ordered if is_due(manifest.get(c), ttl_days)]
        targets = due if fetch_all else due[:limit]
        logger.info(
            "{} CIS in universe, {} due (ttl={}d), fetching {} this run",
            len(universe), len(due), ttl_days, len(targets),
        )

    if not targets:
        logger.info("nothing due; done")
        return

    manifest = load_manifest()
    headers = {"User-Agent": ua}
    n_ok = n_empty = n_err = 0
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        for i, cis in enumerate(targets, 1):
            try:
                page, status = fetch_one(client, cis)
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                rcp = extract_rcp(page)
                write_overlay(cis, rcp)
                digest = hashlib.sha256(rcp.encode("utf-8")).hexdigest()
                manifest[cis] = {
                    "last_fetch": _now_iso(), "hash": digest,
                    "status": "empty" if rcp == "" else "ok", "http": status,
                }
                if rcp == "":
                    n_empty += 1
                    logger.info("[{}/{}] {} -> no RCP (empty overlay)", i, len(targets), cis)
                else:
                    n_ok += 1
                    logger.info("[{}/{}] {} -> {} bytes", i, len(targets), cis, len(rcp))
            except Exception as exc:  # network / parse error: record and move on
                n_err += 1
                manifest[cis] = {"last_fetch": _now_iso(), "status": "error", "error": str(exc)[:200]}
                logger.error("[{}/{}] {} -> {}", i, len(targets), cis, exc)
            # Persist periodically so a long run survives interruption.
            if i % 25 == 0:
                save_manifest(manifest)
            if i < len(targets) and rate > 0:
                time.sleep(rate)

    save_manifest(manifest)
    logger.info("done: {} RCPs, {} empty, {} errors", n_ok, n_empty, n_err)
    logger.info("now rebuild: uv run build.py")


if __name__ == "__main__":
    main()

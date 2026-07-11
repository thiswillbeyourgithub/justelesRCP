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
import math
import os
import re
import time
import unicodedata
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
    """Load the scrape manifest, or an empty mapping if it does not exist yet."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    """Write the manifest atomically (temp file + rename) to survive crashes."""
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def read_catalog() -> list[tuple[str, str]]:
    """Return ``(cis, denomination)`` pairs from ``CIS_bdpm.txt`` in file order.

    The official BDPM file is latin-1, tab-separated: column 0 is the 8-digit
    CIS, column 1 the drug name (same source build.load_names reads for names).
    File order is preserved and used as the stable tiebreak for equal scores.
    """
    catalog: list[tuple[str, str]] = []
    with BDPM_PATH.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t")
            cis = parts[0].strip()
            if cis:
                name = parts[1].strip() if len(parts) > 1 else ""
                catalog.append((cis, name))
    return catalog


def _tokens(text: str) -> set[str]:
    """Normalise a drug name/term to a set of comparable word tokens.

    Uppercases, strips accents (paracétamol -> PARACETAMOL) and splits on any
    non-alphanumeric run, so a term matches a denomination regardless of case,
    accents, dosage punctuation or word order.
    """
    folded = unicodedata.normalize("NFKD", text.upper())
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return {tok for tok in re.split(r"[^A-Z0-9]+", folded) if tok}


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy default method); pct in [0, 1]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def load_frequency(path: Path) -> tuple[dict[str, float], list[tuple[frozenset[str], float]], float]:
    """Load a frequency list and return matchers plus the 25th-percentile score.

    The file is JSONL with at least ``term`` (a drug/substance name) and ``score``
    (higher = higher scrape priority), e.g.
    ``{"term": "DOLIPRANE", "type": "brand", "score": 10}``. Terms are normalised
    to tokens; single-word terms go into a fast ``{token: score}`` map and
    multi-word terms into a ``[(token_set, score)]`` list matched by subset (so
    "ACETYLSALICYLIQUE ACIDE" still matches "... ACIDE ACETYLSALICYLIQUE ..."). A
    term seen twice keeps its highest score.

    Returns ``(single, multi, p25)`` where ``p25`` is the 25th percentile of all
    scores, used as the fallback priority for drugs no term matches.
    """
    single: dict[str, float] = {}
    multi: list[tuple[frozenset[str], float]] = []
    scores: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        term, score = row.get("term"), row.get("score")
        if term is None or score is None:
            continue
        score = float(score)
        scores.append(score)
        toks = _tokens(term)
        if len(toks) == 1:
            word = next(iter(toks))
            single[word] = max(single.get(word, score), score)
        elif toks:
            multi.append((frozenset(toks), score))
    return single, multi, _percentile(scores, 0.25)


def score_catalog(
    catalog: list[tuple[str, str]],
    single: dict[str, float],
    multi: list[tuple[frozenset[str], float]],
    fallback: float,
) -> dict[str, float]:
    """Score every CIS by the best frequency term matching its denomination.

    A CIS scores the max over: single-word terms present in its name tokens, and
    multi-word terms whose whole token set is a subset of the name tokens. A CIS
    no term matches gets ``fallback`` (the p25 priority), so it is still scraped
    at a middling rank rather than being starved to the very end of the queue.
    """
    scores: dict[str, float] = {}
    for cis, name in catalog:
        toks = _tokens(name)
        best = max((single[t] for t in toks if t in single), default=None)
        for term_toks, score in multi:
            if (best is None or score > best) and term_toks <= toks:
                best = score
        scores[cis] = fallback if best is None else best
    return scores


def order_by_score(catalog: list[tuple[str, str]], scores: dict[str, float]) -> list[str]:
    """Order CIS by descending score, breaking ties by original file order."""
    order = {cis: i for i, (cis, _) in enumerate(catalog)}
    return sorted((cis for cis, _ in catalog), key=lambda c: (-scores[c], order[c]))


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
@click.option("--frequency", type=click.Path(exists=True, path_type=Path), default=None,
              help="JSONL of {term, score} priorities (higher=first); matched to drug "
                   f"names. Defaults to {DEFAULT_FREQUENCY} if present.")
@click.option("--timeout", type=float, default=30.0, show_default=True,
              help="Per-request timeout in seconds.")
@click.option("--user-agent",
              default=None,
              help="Override the HTTP User-Agent sent to the ANSM site.")
def main(limit: int, fetch_all: bool, only: tuple[str, ...], ttl_days: int,
         rate: float, force: bool, frequency: Path | None, timeout: float,
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
        catalog = read_catalog()
        # Order the queue by frequency score (falling back to CIS_bdpm file order
        # when no list is given): popular drugs get refreshed first.
        freq_path = frequency or (DEFAULT_FREQUENCY if DEFAULT_FREQUENCY.exists() else None)
        if freq_path:
            single, multi, p25 = load_frequency(freq_path)
            scores = score_catalog(catalog, single, multi, p25)
            ordered = order_by_score(catalog, scores)
            matched = sum(1 for cis in scores if scores[cis] != p25)
            logger.info(
                "frequency {}: {} single + {} multi terms, p25={} fallback; "
                "{}/{} CIS matched a term",
                freq_path.name, len(single), len(multi), p25, matched, len(catalog),
            )
        else:
            ordered = [cis for cis, _ in catalog]
            logger.warning("no --frequency file; using CIS_bdpm file order")
        manifest = load_manifest()
        due = ordered if force else [c for c in ordered if is_due(manifest.get(c), ttl_days)]
        targets = due if fetch_all else due[:limit]
        logger.info(
            "{} CIS in catalog, {} due (ttl={}d), fetching {} this run",
            len(catalog), len(due), ttl_days, len(targets),
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

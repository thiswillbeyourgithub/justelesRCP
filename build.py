# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
#   "brotli>=1.1",
# ]
# ///
"""Build the static justelesRCP site from the ANSM RCP dump.

Pipeline (id + raw ANSM html) -> (id + cleaned, reskinned static page):

  data/CIS_RCP.csv   TSV: Code_CIS <TAB> RCP_html (CSV-quoted, multi-line);
                     frozen 2022 baseline dump (see download-data.sh)
  data/rcp/<cis>.html  optional freshness overlay from scrape-rcp.py; wins over
                     the baseline cell for that CIS (empty file = "no RCP")
  data/CIS_bdpm.txt  official BDPM CIS -> drug name mapping (see download-data.sh)
        |
        v
  dist/
    index.html            instant-search homepage
    style.css  search.js  assets
    search-index.json     [{cis, name, slug}, ...] for client-side search
    rcp/<cis>-<slug>.html cleaned + reskinned RCP page (one per drug)
  + .gz / .br precompressed siblings for every text asset (Caddy serves these).

Run:  uv run build.py         (reads ./data, writes ./dist)
"""

from __future__ import annotations

import base64
import copy
import csv
import gzip
import hashlib
import html as _stdhtml  # stdlib html.unescape (distinct from lxml's html, below)
import json
import os
import re
import shutil
import struct
import sys
import unicodedata
import urllib.parse
from datetime import date, datetime
from multiprocessing import Pool
from pathlib import Path

import brotli
from lxml import html as lxml_html

import bdpm  # shared, pure-stdlib BDPM tokenising + frequency scoring

__version__ = "0.25.0"  # single source of truth; bump patch/minor per change

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = ROOT / "src"
DIST = ROOT / "dist"

CSV_PATH = DATA / "CIS_RCP.csv"
BDPM_PATH = DATA / "CIS_bdpm.txt"
# Optional BDPM joins + the frequency list, used ONLY to build the cross-drug
# backlink index (build_xref_index): the active-substance composition (to seed
# substance link terms and detect mono-substance drugs) and the frequency list
# (to pick, per term, the single most-prescribed target page). All optional; when
# absent the backlink index just covers fewer terms (or none). Same files
# scrape-rcp.py uses to order its scrape queue.
COMPO_PATH = DATA / "CIS_COMPO_bdpm.txt"
GENER_PATH = DATA / "CIS_GENER_bdpm.txt"
FREQUENCY_PATH = DATA / "drugs_frequency.jsonl"
# Freshness overlay written by scrape-rcp.py: one <cis>.html per drug re-fetched
# from the live ANSM site. The CSV above is a frozen 2022 baseline (the only bulk
# RCP dump that exists); the overlay lets us serve current RCPs without abandoning
# the static architecture. An overlay file ALWAYS wins over the CSV cell for the
# same CIS, and an intentionally empty overlay file means "scraped, but this drug
# has no RCP" (so we skip it rather than fall back to a stale baseline cell).
RCP_OVERLAY_DIR = DATA / "rcp"
# EMA overlays: converted product-information HTML for centrally-authorized drugs
# (scrape-ema.py writes data/eu/<cis>.html[.gz], wrapper carries data-ema-date).
# build_stubs renders these as full /eu/ pages; kept separate from data/rcp so
# records() never turns one into a /rcp/ page (the drug's ANSM RCP stays empty).
EU_OVERLAY_DIR = DATA / "eu"
# Per-drug semantic-search section vectors live in dist/<rcp|eu>/<slug>.vec.json,
# written by the embed service (runtime) or embed-rcp.py (offline), NOT baked here:
# build.py only prunes orphaned .vec.json when a slug is dropped (see _prune loops).
# Incremental-build cache. Lives inside dist/ (already gitignored) and records,
# per CIS, the hash of the inputs that produced its page so an unchanged record
# can be reused instead of re-parsed and re-compressed. See main().
MANIFEST_PATH = DIST / ".build-manifest.json"
# Cache of which CIS have a NON-EMPTY baseline RCP cell in CIS_RCP.csv (i.e. would
# render a page). The bulk CSV is frozen (BASELINE_DATE), so this full ~18s parse
# runs once and is reused on every later build, keyed by the CSV's (size, mtime).
# Used by build_xref_index to only ever link to CIS that actually have a page. See
# _baseline_present_cis / _present_cis. Lives in dist/ (already gitignored).
PRESENT_CACHE_PATH = DIST / ".rcp-present.json"
# scrape-rcp.py's manifest: CIS -> {last_fetch (ISO UTC), ...}. Read here only to
# stamp each overlay-sourced page with a real "as of" date for the freshness
# banner (see _load_scrape_dates / _asof_html). Absent on a baseline-only build.
SCRAPE_MANIFEST_PATH = DATA / ".scrape-manifest.json"
# The frozen bulk RCP dump's date (data.gouv.fr upload). Every baseline-sourced
# page carries it as its "as of" date; overlay pages use their scrape date.
BASELINE_DATE = "2022-05-02"
# Official ANSM page for one drug (RCP + notice tabs). Each RCP page links it at
# the top so a reader who doubts our copy or spots a bug can check the source.
# Same URL scrape-rcp.py fetches from (PAGE_URL there); kept here to avoid a
# cross-import (build.py can't cheaply import scrape-rcp.py's httpx/click deps).
ANSM_PAGE_URL = "https://base-donnees-publique.medicaments.gouv.fr/medicament/{cis}/extrait"

# External-reference pill row shown at the top of every page (see _ref_links_html).
# BDPM reuses ANSM_PAGE_URL (keyed by CIS: the drug's official BDPM record). HAS,
# EMA and Vidal are full-text searches on the drug's active substance (see
# load_substances); the EMA query template is the exact one the EMA site uses for a
# document search.
HAS_SEARCH_URL = "https://www.has-sante.fr/jcms/fc_2875171/fr/resultat-de-recherche?text={q}"
EMA_REF_URL = (
    "https://www.ema.europa.eu/en/search?keywords=allwords"
    "&search_api_fulltext={q}&f%5B0%5D=ema_search_entity_is_document%3ADocument"
)
VIDAL_SEARCH_URL = "https://www.vidal.fr/recherche.html?query={q}"

# The RCP HTML field can be very large; lift the csv field-size ceiling.
csv.field_size_limit(sys.maxsize)


def _code_fingerprint() -> bytes:
    """This build script's own source, minus the __version__ line.

    Any change to the build logic or templates should bust the whole incremental
    cache (outputs may now differ). We exclude the __version__ assignment on
    purpose: the version is decoupled from page content (served at runtime via
    app-version.js), so a version-only bump must NOT force a full rebuild.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    return re.sub(r"(?m)^__version__\s*=.*$", "", src).encode("utf-8")


def _global_key(
    page_tpl: str,
    xref: dict[str, tuple[str, str, str]],
    substances: dict[str, str] | None = None,
) -> str:
    """Cache key that invalidates every record when the build code, the template,
    the cross-drug backlink index, OR the active-substance link map change.

    The backlink dictionary must be folded in because a page's injected links
    depend on the WHOLE index, not just that record's own inputs (which
    _record_hash covers): adding a drug can introduce a new term that changes an
    unrelated page's body, so a changed index has to bust the whole cache. The
    substance map is folded in for the same reason: it feeds the external-reference
    pill row (_ref_links_html) but is NOT part of _record_hash, so a composition
    change that alters a drug's substance links must bust the cache too."""
    h = hashlib.sha256()
    h.update(_code_fingerprint())
    h.update(b"\0")
    h.update(page_tpl.encode("utf-8"))
    h.update(b"\0")
    h.update(
        json.dumps(sorted((substances or {}).items()), ensure_ascii=False).encode("utf-8")
    )
    h.update(b"\0")
    fp = sorted((term, slug) for term, (_, slug, _) in xref.items())
    h.update(json.dumps(fp, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _record_hash(raw: str, mapped_name: str, asof: str) -> str:
    """Per-record cache key: the raw ANSM HTML, the CIS->name mapping value, and
    the "as of" date baked into the page's freshness banner.

    The rendered page is a pure function of (raw, mapped_name, asof, template,
    code); template/code are folded into the global key, so these three suffice
    here. asof is included so a re-scrape that refreshes the date without changing
    the HTML still re-renders the page with the new date. The parsed denomination
    is derived from raw, so it needs no separate input.
    """
    h = hashlib.sha256()
    h.update(raw.encode("utf-8"))
    h.update(b"\0")
    h.update(mapped_name.encode("utf-8"))
    h.update(b"\0")
    h.update(asof.encode("utf-8"))
    return h.hexdigest()


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def slugify(text: str) -> str:
    """ASCII, lowercase, hyphenated slug suitable for a URL path segment."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:80] or "rcp"


def load_names() -> dict[str, str]:
    """CIS code -> official drug name, from CIS_bdpm.txt (tab-separated, latin-1).

    Column layout per BDPM spec: 0=Code_CIS, 1=Denomination. Returns {} if the
    file is absent so the build can still run (falls back to HTML denomination).
    """
    if not BDPM_PATH.exists():
        print(f"  ! {BDPM_PATH.name} missing; falling back to HTML denomination")
        return {}
    names: dict[str, str] = {}
    with BDPM_PATH.open(encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) >= 2 and row[0].strip():
                names[row[0].strip()] = row[1].strip()
    print(f"  loaded {len(names)} names from {BDPM_PATH.name}")
    return names


_FR_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def _fr_date(iso: str) -> str:
    """'2022-05-02' -> '2 mai 2022' (human French date for the freshness banner)."""
    year, month, day = iso.split("-")
    return f"{int(day)} {_FR_MONTHS[int(month) - 1]} {year}"


# ANSM stamps every RCP body with its own revision date, e.g.
# <span class="DateNotif">ANSM - Mis à jour le : 07/02/2022</span>. All ~12k
# baseline pages and every live scrape carry it. We anchor on the class and grab
# the first DD/MM/YYYY in that element, tolerating extra classes/whitespace/
# entities without depending on the exact "Mis à jour le" wording.
_ANSM_DATE_RE = re.compile(
    r'class="[^"]*\bDateNotif\b[^"]*"[^>]*>[^<]*?(\d{1,2})/(\d{1,2})/(\d{4})'
)


def _ansm_date(html: str) -> str:
    """ANSM's own 'Mis à jour le' revision date (from the DateNotif element),
    as ISO 'YYYY-MM-DD', or '' if absent/unparseable.

    This is when the *official text* was last revised, which is distinct from our
    capture/scrape date (see _asof_html): a drug ANSM last touched in 2021 is
    still its current official text, so this is the meaningful headline date to
    show the reader. Parsed from the cleaned HTML so it matches what is on the
    page byte-for-byte."""
    m = _ANSM_DATE_RE.search(html)
    if not m:
        return ""
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:  # a malformed date like 32/13/2022
        return ""


def _load_scrape_dates() -> dict[str, str]:
    """CIS -> 'YYYY-MM-DD' of its last successful scrape, from scrape-rcp.py's
    manifest. Used to stamp each overlay-sourced page with a real 'as of' date;
    returns {} when the manifest is absent (baseline-only build)."""
    try:
        raw = json.loads(SCRAPE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    dates: dict[str, str] = {}
    for cis, entry in raw.items():
        stamp = (entry or {}).get("last_fetch")
        if not stamp:
            continue
        try:
            dates[cis] = datetime.fromisoformat(stamp).date().isoformat()
        except ValueError:
            continue
    return dates


def _load_ema_links() -> dict[str, str]:
    """CIS -> direct EMA product-information PDF URL, from scrape-rcp.py's manifest.

    scrape-rcp.py's extract_ema_pdf harvests, from each scraped ANSM page, the
    real href to the EMA-published SmPC/notice PDF (present only on centrally-
    authorized, empty-RCP drugs). build_stubs prefers this exact link over the
    EMA brand search. Returns {} when the manifest is absent (baseline-only
    build); a CIS not yet re-scraped since this feature landed simply won't
    appear and its stub falls back to the search URL."""
    try:
        raw = json.loads(SCRAPE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    links: dict[str, str] = {}
    for cis, entry in raw.items():
        url = (entry or {}).get("ema_pdf")
        if url:
            links[cis] = url
    return links


def _overlay_path(cis: str, overlay_dir: Path = RCP_OVERLAY_DIR) -> Path | None:
    """Return the overlay file for a CIS, or None if none exists.

    scrape-rcp.py stores each overlay either plain (``<cis>.html``) or gzipped
    (``<cis>.html.gz``), depending on RCP_OVERLAY_GZIP at scrape time, and keeps
    only one of the two. We read whichever is present transparently; if both
    somehow coexist (format flipped mid-cache), the newest by mtime wins.
    ``overlay_dir`` defaults to the ANSM overlays; build_stubs passes
    EU_OVERLAY_DIR for the converted EMA overlays.
    """
    cands = [
        p for p in (overlay_dir / f"{cis}.html.gz", overlay_dir / f"{cis}.html")
        if p.exists()
    ]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _read_overlay(path: Path) -> str:
    """Decode an overlay file to HTML text, transparently un-gzipping if needed.

    A zero-byte file is the "scraped, no RCP" sentinel and decodes to "" in
    either format (we never gzip an empty overlay), so the caller skips it
    without attempting to gunzip 0 bytes.
    """
    data = path.read_bytes()
    if not data:
        return ""
    if path.suffix == ".gz":
        return gzip.decompress(data).decode("utf-8")
    return data.decode("utf-8")


def _overlay_date(cis: str) -> str:
    """Fallback 'as of' date for an overlay with no manifest entry: the overlay
    file's own modification date. '' if it cannot be read."""
    path = _overlay_path(cis)
    if path is None:
        return ""
    try:
        return date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""


def _baseline_present_cis() -> set[str]:
    """CIS whose frozen baseline CSV cell has a non-empty RCP (would render a page).

    ~15% of CIS have an empty RCP field and are pageless; a cross-drug link must
    never target one (it would 404). The bulk CSV is frozen at BASELINE_DATE, so
    this ~18s full parse is memoised in PRESENT_CACHE_PATH keyed by the CSV's
    (size, mtime): the set is recomputed only if the CSV ever changes, keeping
    incremental rebuilds fast. Returns an empty set when there is no CSV (an
    overlay-only build); overlay presence is layered on in _present_cis.
    """
    if not CSV_PATH.exists():
        return set()
    st = CSV_PATH.stat()
    sig = [st.st_size, int(st.st_mtime)]
    try:
        cached = json.loads(PRESENT_CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("sig") == sig:
            return set(cached.get("cis", []))
    except (OSError, ValueError):
        pass
    present: set[str] = set()
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        next(reader, None)  # header
        for row in reader:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                present.add(row[0].strip())
    try:
        PRESENT_CACHE_PATH.write_text(
            json.dumps({"sig": sig, "cis": sorted(present)}), encoding="utf-8"
        )
    except OSError:
        pass
    return present


def _present_cis() -> set[str]:
    """Set of CIS that will actually render a page this build.

    The cached baseline presence set, adjusted by overlays exactly as records()
    resolves them: a non-empty overlay adds/keeps a CIS (including an overlay-only
    drug absent from the baseline), and an EMPTY overlay is the "scraped, no RCP"
    sentinel that removes it (the overlay wins over a non-empty baseline cell).
    build_xref_index restricts every link target to this set so no link 404s.
    """
    present = _baseline_present_cis()
    if RCP_OVERLAY_DIR.is_dir():
        overlay_cis = {
            p.name.split(".", 1)[0]
            for p in (
                *RCP_OVERLAY_DIR.glob("*.html"),
                *RCP_OVERLAY_DIR.glob("*.html.gz"),
            )
        }
        for cis in overlay_cis:
            path = _overlay_path(cis)
            if path is None:
                continue
            if _read_overlay(path).strip():
                present.add(cis)
            else:
                present.discard(cis)  # empty overlay: scraped, confirmed no RCP
    return present


def page_cis_from_dist() -> set[str]:
    """Set of CIS that have a built page on disk (``dist/rcp/<cis>-<slug>.html``).

    Derived by globbing the rendered output rather than the source data, for
    callers that lack the ANSM source (the refresh service mounts ``dist/rcp`` but
    not ``CIS_RCP.csv``). The main build uses _present_cis instead, which is
    source-derived and correct even before any page has been rendered.
    """
    rcp_dir = DIST / "rcp"
    if not rcp_dir.is_dir():
        return set()
    return {p.name.split("-", 1)[0] for p in rcp_dir.glob("*.html")}


def iter_rcp_raw(scrape_dates: dict[str, str] | None = None, stats: dict | None = None):
    """Yield ``(cis, raw, asof)`` for every drug that renders an RCP page.

    Sources merged with the scraped ``data/rcp`` overlay winning over the 2022 CSV
    cell (an empty overlay means "scraped, no RCP" and is skipped, not fallen back);
    overlay-only CIS absent from the baseline are yielded afterwards. ``asof`` is the
    freshness date (scrape date for overlay data, else BASELINE_DATE). This is the
    importable core of main()'s ``records()``; embed-rcp.py reuses it so the
    build-time embeddings cover exactly the pages the build renders. ``stats``, if
    given, gets ``stats['empty']`` incremented for each skipped empty RCP.
    """
    if scrape_dates is None:
        scrape_dates = _load_scrape_dates()

    def _overlay(cis: str) -> str | None:
        # A present overlay always supersedes the CSV cell (fresher); an empty file
        # is a real "scraped, no RCP" value returned as "" so the caller skips it.
        if not RCP_OVERLAY_DIR.is_dir():
            return None
        path = _overlay_path(cis)
        return _read_overlay(path) if path is not None else None

    def _bump_empty() -> None:
        if stats is not None:
            stats["empty"] = stats.get("empty", 0) + 1

    seen: set[str] = set()
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t")
            next(reader, None)  # header: Code_CIS / RCP_html
            for row in reader:
                if len(row) < 2:
                    continue
                cis = row[0].strip()
                seen.add(cis)
                overlay = _overlay(cis)
                if overlay is None:
                    raw, asof = row[1], BASELINE_DATE
                else:
                    raw, asof = overlay, scrape_dates.get(cis) or _overlay_date(cis)
                if not raw.strip():
                    _bump_empty()  # no published RCP (or empty overlay)
                    continue
                yield cis, raw, asof
    # Overlay-only drugs: scraped CIS that never existed in the 2022 baseline. The
    # CIS is the filename up to the first dot (8 digits, never dotted).
    if RCP_OVERLAY_DIR.is_dir():
        overlay_cis = {
            p.name.split(".", 1)[0]
            for p in (*RCP_OVERLAY_DIR.glob("*.html"), *RCP_OVERLAY_DIR.glob("*.html.gz"))
        }
        for cis in sorted(overlay_cis):
            if cis in seen:
                continue
            raw = _overlay(cis)
            if raw is None:
                continue
            if not raw.strip():
                _bump_empty()
                continue
            yield cis, raw, scrape_dates.get(cis) or _overlay_date(cis)


def iter_eu_raw():
    """Yield ``(cis, raw)`` for every /eu/ page that has a converted EMA overlay (a
    FULL page, rendered by render_eu_page), skipping lightweight stubs (which have no
    overlay = no captured text). The EMA counterpart of iter_rcp_raw, used by
    embed-rcp.py so per-drug semantic search covers /eu/ pages too. The overlay is
    the same ``<div id="textDocument">`` envelope, with sec-N anchors already baked
    by ema_pdf, so section_chunks segments it exactly like an RCP page. A CIS is
    either an ANSM RCP page OR an /eu/ page (never both), so their per-CIS
    <slug>.vec.json under dist/rcp vs dist/eu never collide across the two lanes."""
    if not EU_OVERLAY_DIR.is_dir():
        return
    eu_cis = {
        p.name.split(".", 1)[0]
        for p in (*EU_OVERLAY_DIR.glob("*.html"), *EU_OVERLAY_DIR.glob("*.html.gz"))
    }
    for cis in sorted(eu_cis):
        path = _overlay_path(cis, EU_OVERLAY_DIR)
        if path is None:
            continue
        raw = _read_overlay(path)
        if not raw.strip():  # zero-byte sentinel: scraped, no document
            continue
        yield cis, raw


# --- cross-drug backlinks (xref) --------------------------------------------
# An RCP body often names OTHER drugs or active substances (e.g. a
# contraindication mentioning "ritonavir"). build_xref_index() builds, once per
# build, a map of linkable term -> the single canonical page to link it to, and
# _linkify() wraps those mentions in the RCP body with <a> links (plus a
# "Médicaments liés" section, see _xref_html). Matching is deliberately
# conservative: whole-word, accent-folded, >= _XREF_MIN_LEN chars, a curated
# stoplist of common French/pharmaceutical words, each term linked only once per
# page, capped at _XREF_MAX_LINKS, and never linking a page to itself.
_XREF_MIN_LEN = 6
_XREF_MAX_LINKS = 15

# Common French + pharmaceutical words that occur inside substance/brand names
# (salts, dosage forms, generic vocabulary) but must NEVER become links: they are
# frequent in RCP prose and would produce noisy, wrong cross-links.
_XREF_STOP = frozenset("""
SODIUM CHLORHYDRATE SULFATE ACIDE POTASSIUM CALCIUM MAGNESIUM MONOHYDRATE
DIHYDRATE ANHYDRE HYDROXYDE PHOSPHATE CITRATE TARTRATE MALEATE MESILATE
BROMHYDRATE ACETATE CHLORURE GLUCOSE FUMARATE GLUCONATE LACTATE NITRATE
BENZOATE STEARATE OXALATE SUCCINATE DIPROPIONATE
COMPRIME COMPRIMES GELULE GELULES POUDRE SUSPENSION INJECTABLE PELLICULE
PELLICULEE ENROBE ENROBEE SECABLE DISPERSIBLE EFFERVESCENT EFFERVESCENTE
LIBERATION PROLONGEE MODIFIEE FLACON AMPOULE SACHET BUVABLE ORALE RECTALE
NASALE CUTANEE VAGINALE OPHTALMIQUE SOLUTION SOLVANT EMULSION DISPERSION
GRANULES POMMADE COLLYRE GOUTTES UNIDOSE RECIPIENT PERFUSION SIROP CREME
MICROGRAMMES MILLIGRAMMES GASTRORESISTANT GASTRORESISTANTE LYOPHILISAT
INHIBITEUR INHIBITEURS ENZYME RECEPTEUR RECEPTEURS SYSTEME TRAITEMENT
PATIENT PATIENTS EFFETS INDICATION POSOLOGIE GROSSESSE ALLAITEMENT VITAMINE
VITAMINES HORMONE PROTEINE PROTEINES HUMAINE HUMAIN ADULTE ADULTES ENFANT
ENFANTS ASSOCIATION RESISTANT PLAQUETTAIRE PLASMATIQUE SANGUINE CELLULE
CELLULES DILUTION DILUTIONS DEGRE COMPRISE COMPRISES HERBA PLANTA RADIX
FOLIUM RECOMBINANT ACTIVITE BYPASSING FACTEUR COAGULATION IMMUNOGLOBULINE
ALLERGENIQUE POLYOSIDE
""".split())


def _make_fold_table() -> dict[int, int]:
    """Length-preserving fold: lowercase -> uppercase, accented Latin -> base
    ASCII, everything else unchanged. Used so a text node's folded form aligns
    1:1 (same length) with the original, letting match offsets on the folded
    string index straight back into the original text (which keeps its case and
    accents in the visible link)."""
    table: dict[int, int] = {lo: lo - 32 for lo in range(ord("a"), ord("z") + 1)}
    for chars, base in (
        ("àâäáãå", "A"), ("ç", "C"), ("èéêë", "E"), ("ìíîï", "I"),
        ("ñ", "N"), ("òóôöõ", "O"), ("ùúûü", "U"), ("ýÿ", "Y"),
    ):
        for c in chars:
            table[ord(c)] = ord(base)
            table[ord(c.upper())] = ord(base)
    return table


_FOLD_TABLE = _make_fold_table()


def build_xref_index(
    names: dict[str, str], page_cis: set[str]
) -> dict[str, tuple[str, str, str]]:
    """Map a linkable term (accent-folded uppercase word) -> canonical target
    ``(cis, slug, display_name)``.

    Terms come from two sources: each drug's BRAND ROOT (the first clean word of
    its denomination, e.g. DOLIPRANE) and, for MONO-substance drugs only, its
    ACTIVE-SUBSTANCE tokens (e.g. RITONAVIR). Restricting substances to
    mono-substance drugs keeps "paracétamol" pointing at a paracetamol drug
    rather than some combination product that merely contains it. A term is only
    kept if it also appears in the frequency list of real drug/substance names:
    that whitelist is what stops descriptive words baked into substance
    denominations (e.g. STAMARIL's "virus de la fièvre jaune", HOLOCLAR's
    "cellules ... contenant ...") from turning common words like "fièvre" or
    "contenant" into links. Per term the target is chosen by prescription
    frequency (mono targets preferred, then the frequency score from the same
    list scrape-rcp.py uses, then file order), so e.g. RITONAVIR -> NORVIR,
    OMEPRAZOLE -> MOPRAL.

    ``page_cis`` is the set of CIS that actually render a page (see _present_cis /
    page_cis_from_dist). Only those may be link targets: ~15% of CIS have an empty
    RCP and are pageless, so linking to one would 404 (e.g. HELICOBACTER's only
    carriers are pageless breath-test diagnostics, which made "Helicobacter pylori"
    a broken link on amoxicillin pages). A term whose best-scored carrier is
    pageless falls back to its best carrier that does have a page, or drops out
    entirely if none do. Returns {} when CIS_bdpm.txt or the frequency list is
    missing (nothing safe to link)."""
    # is_file (not exists): a missing OR stray-directory single-file mount (a bad
    # bind mount in the refresh container) degrades to no backlinks, never a crash.
    if not BDPM_PATH.is_file() or not FREQUENCY_PATH.is_file():
        return {}
    catalog = bdpm.read_catalog(BDPM_PATH)
    signals = bdpm.substance_signals(catalog, COMPO_PATH, GENER_PATH)
    single, multi, p25 = bdpm.load_frequency(FREQUENCY_PATH)
    scores, _ = bdpm.score_catalog(signals, single, multi, p25)
    # Only real drug/substance names (single-word frequency terms) may become
    # link terms; this whitelist is the primary false-positive guard.
    whitelist = set(single)
    order = {cis: i for i, (cis, _) in enumerate(catalog)}
    keep = {cis for cis, _ in catalog}

    # Composition: per CIS, the count of distinct active principles (distinct
    # "numéro de liaison SA/FT", column 7 -> mono when 1) and the union of
    # active-substance-denomination tokens (column 3).
    liaisons: dict[str, set[str]] = {}
    sub_tokens: dict[str, set[str]] = {}
    if COMPO_PATH.is_file():
        with COMPO_PATH.open(encoding="latin-1") as fh:
            for line in fh:
                parts = line.split("\t")
                if len(parts) <= 7:
                    continue
                cis = parts[0].strip()
                if cis not in keep:
                    continue
                liaisons.setdefault(cis, set()).add(parts[7].strip())
                sub_tokens.setdefault(cis, set()).update(bdpm.tokens(parts[3]))

    best: dict[str, tuple[tuple, str]] = {}

    def consider(term: str, cis: str, mono: bool) -> None:
        if (len(term) < _XREF_MIN_LEN or term not in whitelist
                or term in _XREF_STOP or not term.isalpha()
                or cis not in page_cis):  # never target a pageless CIS (would 404)
            return
        key = (mono, scores.get(cis, 0.0), -order[cis])
        cur = best.get(term)
        if cur is None or key > cur[0]:
            best[term] = (key, cis)

    for cis, denom in catalog:
        head = denom.split(",")[0].strip()
        if not head:
            continue
        first = head.split()[0]  # brand root: a single clean alpha word only
        if first.isalpha():  # False for combos like "LOPINAVIR/RITONAVIR"
            mono = len(liaisons.get(cis, ())) <= 1
            for t in bdpm.tokens(first):
                consider(t, cis, mono)
    for cis, toks in sub_tokens.items():
        if len(liaisons.get(cis, ())) == 1:  # substances: mono-substance drugs only
            for t in toks:
                consider(t, cis, True)

    xref: dict[str, tuple[str, str, str]] = {}
    for term, (_, cis) in best.items():
        name = names.get(cis)
        if name:  # only link to a page we can name (and thus slug) deterministically
            xref[term] = (cis, f"{cis}-{slugify(name)}", name)
    return xref


def _skip_text(el) -> bool:
    """True for elements whose text must not be linkified: existing anchors (no
    nested links) and section headings / the denomination (keep titles clean)."""
    tag = el.tag
    if not isinstance(tag, str):
        return True  # comments / processing instructions
    if tag.lower() == "a":
        return True
    cls = el.get("class") or ""
    return "AmmAnnexeTitre" in cls or "AmmDenomination" in cls


def _match_spans(text: str, xref, cur_cis, state):
    """Find non-overlapping term matches in ``text``: a list of
    ``(start, end, matched_text, slug)`` or None. Whole-word (matches run on the
    accent-folded text, whose runs of [A-Z0-9] are whole words), each term linked
    once per page, self-links dropped, budget in ``state`` respected."""
    folded = text.translate(_FOLD_TABLE)
    spans = []
    for m in re.finditer(r"[A-Z0-9]+", folded):
        if state["left"] <= 0:
            break
        word = m.group()
        if word in state["terms"]:
            continue  # link each term only once per page (first occurrence)
        hit = xref.get(word)
        if hit is None:
            continue
        cis, slug, disp = hit
        if cis == cur_cis:
            continue  # never link a page to itself
        s, e = m.start(), m.end()
        spans.append((s, e, text[s:e], slug))
        state["terms"].add(word)
        state["left"] -= 1
        if slug not in state["slugs"]:
            state["slugs"].add(slug)
            state["links"].append((slug, disp))
    return spans or None


def _linkify(inner, xref, cur_cis) -> list[tuple[str, str]]:
    """Wrap dictionary-term mentions in ``inner`` (an lxml element) with <a>
    links, in place, and return the ordered distinct ``(slug, display)`` targets
    linked (for the "Médicaments liés" section). No-op returning [] when the xref
    index is empty."""
    if not xref:
        return []
    state = {"terms": set(), "slugs": set(), "links": [], "left": _XREF_MAX_LINKS}
    # Snapshot text carriers BEFORE mutating (inserting <a> nodes changes the
    # tree): each element's own text, and each child's tail (text after it in the
    # parent). Insertion positions are recomputed at apply time, so processing
    # order is safe.
    carriers = []  # (parent, child_or_None, text); child None => parent.text
    for el in inner.iter():
        if not _skip_text(el) and el.text:
            carriers.append((el, None, el.text))
        for child in el:
            if child.tail:
                carriers.append((el, child, child.tail))
    for parent, child, text in carriers:
        if state["left"] <= 0:
            break
        spans = _match_spans(text, xref, cur_cis, state)
        if not spans:
            continue
        anchors = []
        for i, (s, e, mtext, slug) in enumerate(spans):
            a = lxml_html.Element("a")
            a.set("class", "drug-xref")
            a.set("href", f"/rcp/{slug}")
            a.text = mtext
            nxt = spans[i + 1][0] if i + 1 < len(spans) else len(text)
            a.tail = text[e:nxt]
            anchors.append(a)
        head = text[: spans[0][0]]
        if child is None:  # parent.text: links go at the front, before any children
            parent.text = head
            for j, a in enumerate(anchors):
                parent.insert(j, a)
        else:  # child.tail: links go right after that child
            child.tail = head
            idx = parent.index(child)
            for j, a in enumerate(anchors):
                parent.insert(idx + 1 + j, a)
    return state["links"]


def _xref_html(links: list[tuple[str, str]]) -> str:
    """The bottom-of-page "Médicaments liés" section listing the distinct drugs
    linked from this RCP's text. Empty string when nothing was linked."""
    if not links:
        return ""
    items = "".join(
        f'<li><a href="/rcp/{_esc(slug)}">{_esc(disp)}</a></li>' for slug, disp in links
    )
    return (
        '<details class="drug-xref-list">'
        "<summary>Médicaments liés cités dans ce texte</summary>"
        f"<ul>{items}</ul>"
        '<p class="drug-xref-note">Liens ajoutés automatiquement par justelesRCP '
        "d'après les noms de médicaments et de substances cités ci-dessus ; "
        "ils ne font pas partie du texte officiel de l'ANSM.</p>"
        "</details>"
    )


# ANSM cruft we strip so a single stylesheet can own the look.
_STRIP_XPATH = (
    "//img[contains(@src, 'BackToTop')]"  # "back to top" arrows
    " | //a[.//img[contains(@src, 'BackToTop')]]"
    " | //script | //style"
)


def _parse_clean(raw: str):
    """Parse raw ANSM HTML and strip decoration (BackToTop arrows, script/style,
    inline font styling), returning the lxml document.

    Factored out of clean_rcp so section_chunks segments the IDENTICAL tree the
    rendered page is built from (same strip rules, so a chunk's text matches what
    the reader sees). Keeps the Amm* class hooks style.css restyles.
    """
    doc = lxml_html.fromstring(raw)
    for node in doc.xpath(_STRIP_XPATH):
        node.getparent().remove(node)
    # Drop inline font/size styling; keep the class hooks (AmmDenomination, ...).
    for node in doc.xpath("//*[@style]"):
        style = node.get("style")
        kept = [d for d in style.split(";") if d.strip() and "font" not in d.lower()]
        if kept:
            node.set("style", ";".join(kept))
        else:
            del node.attrib["style"]
    return doc


def _inner_of(doc):
    """The RCP body element: the ANSM ``<div id="textDocument">`` envelope, or the
    whole document if it is somehow absent. Shared by clean_rcp / section_chunks."""
    body = doc.xpath("//div[@id='textDocument']")
    return body[0] if body else doc


def _denomination(doc) -> str:
    """Drug name = section-1 denomination. Class hooks vary (AmmDenomination,
    AmmCorpsTexteGras, ...), so anchor on the 'RcpDenomination' name and take the
    first non-empty paragraph after the '1. DENOMINATION' title."""
    hit = doc.xpath("//*[contains(@class, 'AmmDenomination')]")
    if hit and hit[0].text_content().strip():
        return hit[0].text_content().strip()
    anchor = doc.xpath("//a[@name='RcpDenomination']")
    if anchor:
        title = anchor[0].getparent()
        for sib in title.itersiblings():
            if "AmmAnnexeTitre1" in (sib.get("class") or ""):
                break  # reached section 2 without finding a name
            text = sib.text_content().strip()
            if text:
                return text
    return ""


def _build_toc(inner) -> list[tuple[str, str]]:
    """Give each top-level section (AmmAnnexeTitre1, e.g. '1. DENOMINATION ...')
    a stable id and return [(id, title)] for the on-page table of contents.

    Mutates the tree in place (adds id="sec-N"), so the ids land in the rendered
    HTML and the sidebar links resolve to them.
    """
    toc: list[tuple[str, str]] = []
    for el in inner.xpath(".//*[contains(@class, 'AmmAnnexeTitre1')]"):
        title = " ".join(el.text_content().split())
        if not title:
            continue
        sec_id = f"sec-{len(toc)}"
        el.set("id", sec_id)
        toc.append((sec_id, title))
    return toc


def clean_rcp(
    raw: str, cis: str = "", xref: dict[str, tuple[str, str, str]] | None = None
) -> tuple[str, str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (denomination, cleaned_inner_html, toc, xref_links) for one RCP.

    Keeps the semantic structure (section anchors, headings, paragraphs) but
    removes ANSM decoration and inline font styling so style.css can reskin it.
    The toc is the list of top-level sections for the sidebar navigation; when an
    ``xref`` index is given, mentions of other drugs/substances in the body are
    wrapped in <a> links (skipping ``cis``'s own page) and the distinct targets
    are returned as ``xref_links`` for the "Médicaments liés" section.
    """
    doc = _parse_clean(raw)
    denom = _denomination(doc)
    inner = _inner_of(doc)
    toc = _build_toc(inner)
    # Inject cross-drug backlinks after the toc is built (so section ids/titles
    # are captured from the untouched headings) but before serialisation.
    xref_links = _linkify(inner, xref or {}, cis)
    cleaned = "".join(
        lxml_html.tostring(c, encoding="unicode") for c in inner.iterchildren()
    )
    return denom, cleaned, toc, xref_links


# --- per-section text chunks for semantic search ----------------------------
# One RCP is embedded per top-level section (the sec-N anchors the ToC uses), with
# long sections split into ~_SEC_CHUNK_CHARS word-windows so a query matches a
# specific passage, not a whole 2000-word section. section_chunks is the single
# source of truth for chunk<->sec-N alignment, shared with embed-rcp.py (build-time
# embeddings) so the vectors segment exactly what render_record bakes into the page.
_SEC_CHUNK_CHARS = 500
_SEC_SNIPPET_CHARS = 160
_SEC_MIN_CHARS = 24
_SEC_MAX_CHUNKS = 160


def quantize_int8(values) -> list[int]:
    """Symmetric int8 quantisation of an L2-normalised embedding (components in
    [-1, 1]): ``q = round(v * 127)`` clamped to [-127, 127]. The canonical formula,
    used by embed-rcp.py (build-time) and mirrored by src/rcp-semsearch.js
    (runtime ``q / 127``); ~1/127 max error, negligible for cosine ranking."""
    out = []
    for v in values:
        q = round(v * 127)
        out.append(127 if q > 127 else -127 if q < -127 else int(q))
    return out


def dequantize_int8(q) -> list[float]:
    """Inverse of quantize_int8 (the reference src/rcp-semsearch.js mirrors)."""
    return [x / 127 for x in q]


def raw_hash(raw: str) -> str:
    """Short content hash of an overlay's raw HTML, baked into its .vec.json as the
    self-describing staleness key (a re-crawl that produced identical bytes hashes the
    same -> no re-embed). SHARED by embed-service.py + embed-rcp.py so their staleness
    decisions never diverge; matches the 16-hex form embed-rcp.py used."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def _char_windows(text: str, limit: int) -> list[str]:
    """Split text into word-aligned windows no longer than ~limit chars."""
    windows: list[str] = []
    cur = ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > limit:
            windows.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        windows.append(cur)
    return windows


_TABLE_MAX_ROWS = 60  # cap a runaway table so one drug can't blow the chunk budget


def _linearize_table(tbl) -> list[str]:
    """Turn a data table into one self-contained line per body row, each cell
    prefixed with its column header ("Population: Adulte; Dose: 500 mg"), so a row
    stays meaningful (and retrievable) instead of being flattened into the section
    blob and split mid-row by _char_windows. Naive text_content() on a posology
    table loses the row/column association entirely.

    Returns [] when the element is NOT a header-led data table (fewer than 2 rows,
    single column, or empty/layout header), so the caller falls back to flat text.
    Pure lxml; the first <tr> is taken as the header row (thead or not)."""
    rows = tbl.xpath(".//tr")
    if len(rows) < 2:
        return []

    def _cells(tr):
        return [_norm_ws(c.text_content()) for c in tr.xpath("./th | ./td")]

    header = _cells(rows[0])
    if len(header) < 2 or not any(header):
        return []  # not a header-led data table -> caller keeps it flat
    lines: list[str] = []
    for tr in rows[1:]:
        vals = _cells(tr)
        if not any(vals):
            continue
        pairs = []
        for i, v in enumerate(vals):
            if not v:
                continue
            head = header[i] if i < len(header) else ""
            pairs.append(f"{head}: {v}" if head else v)
        if pairs:
            lines.append("; ".join(pairs))
        if len(lines) >= _TABLE_MAX_ROWS:
            break
    return lines


def section_chunks(raw: str, cis: str = "") -> list[tuple[str, str, str]]:
    """Segment one RCP into per-section embedding chunks aligned with ``sec-N``.

    Returns ``[(sec_id, snippet, chunk_text), ...]`` where ``sec_id`` is the SAME
    id render_record puts on the section heading (so a search hit scrolls to a real
    ``#sec-N``), ``snippet`` is a short display excerpt, and ``chunk_text`` is the
    section-title-prefixed text to embed (the model prefix, e.g. e5's "passage:",
    is added by the caller). Pure stdlib+lxml, no ML dependency; imported by
    embed-rcp.py. Each section's body is gathered from the heading's following
    siblings up to the next top-level heading (the same walk _denomination uses)
    and split into ~_SEC_CHUNK_CHARS windows.
    """
    try:
        doc = _parse_clean(raw)
    except Exception:  # a handful of dumps have markup lxml refuses
        return []
    inner = _inner_of(doc)
    headings = inner.xpath(".//*[contains(@class, 'AmmAnnexeTitre1')]")
    if not headings:
        return []
    # ANSM raw carries no ids: assign sec-N exactly as clean_rcp/_build_toc does for
    # the rendered page. A converted /eu/ overlay ALREADY carries sec-N ids from
    # ema_pdf (which render_eu_page keeps verbatim, and its ids are QRD-numbered, not
    # a 0-based run), so re-running _build_toc would renumber them and desync the
    # chunk anchors from the page. Only assign when they're absent.
    if not any(h.get("id") for h in headings):
        _build_toc(inner)  # assigns id="sec-N"; empty-title headings get none
    chunks: list[tuple[str, str, str]] = []

    def _add(sec_id: str, title: str, body: str) -> bool:
        """Append one (sec_id, snippet, chunk_text); return False once the per-page
        cap is hit (caller stops). Tiny trailing fragments are dropped, but a
        title-only chunk for an otherwise-empty section is kept (stays searchable)."""
        chunk_text = f"{title} : {body}".strip(" :") if body else title
        if not chunk_text:
            return True
        if body and len(chunk_text) < _SEC_MIN_CHARS:
            return True
        snippet = _norm_ws(body or title)[:_SEC_SNIPPET_CHARS]
        chunks.append((sec_id, snippet, chunk_text))
        return len(chunks) < _SEC_MAX_CHUNKS

    for h_el in inner.xpath(".//*[contains(@class, 'AmmAnnexeTitre1')]"):
        sec_id = h_el.get("id")
        if not sec_id:  # empty-title heading skipped by _build_toc
            continue
        title = _norm_ws(h_el.text_content())
        narrative: list[str] = []
        table_rows: list[str] = []  # each a self-contained linearised table row
        for sib in h_el.itersiblings():
            if "AmmAnnexeTitre1" in (sib.get("class") or ""):
                break  # next section
            if sib.tag == "table":
                rows = _linearize_table(sib)
                if rows:
                    table_rows.extend(rows)
                else:
                    narrative.append(sib.text_content())
                continue
            nested = list(sib.iter("table"))
            if not nested:
                narrative.append(sib.text_content())
                continue
            # A block wrapping table(s): linearise data tables row-by-row, keep any
            # non-data table flat, and take the block's narrative WITHOUT the tables
            # (a deepcopy with them stripped) so table text is never counted twice.
            for tbl in nested:
                rows = _linearize_table(tbl)
                if rows:
                    table_rows.extend(rows)
                else:
                    narrative.append(tbl.text_content())
            clone = copy.deepcopy(sib)
            for t in list(clone.iter("table")):
                if t.getparent() is not None:
                    t.getparent().remove(t)
            narrative.append(clone.text_content())
        body_all = _norm_ws(" ".join(narrative))
        windows = _char_windows(body_all, _SEC_CHUNK_CHARS)
        if not windows and not table_rows:
            windows = [""]  # title-only, so an empty section stays searchable
        for body in windows:
            if not _add(sec_id, title, body):
                return chunks
        # Table rows are kept intact (never merged into the narrative windows); only
        # a pathological giant row is itself word-windowed as a safety valve.
        for row in table_rows:
            for piece in _char_windows(row, _SEC_CHUNK_CHARS) or [row]:
                if not _add(sec_id, title, piece):
                    return chunks
    return chunks


# --- A-Z browse pages -------------------------------------------------------
_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]


def _letter_key(label: str) -> str:
    """URL/file key for a browse letter ('#' -> 'num', else lowercase)."""
    return "num" if label == "#" else label.lower()


def _first_letter(name: str) -> str:
    """Bucket a drug name under A-Z (accent-folded) or '#' for non-alpha."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    for ch in folded:
        if ch.isalpha():
            return ch.upper()
    return "#"


def _sort_key(name: str) -> str:
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()


def write_browse(index: list[dict[str, str]]) -> int:
    """Write /browse/ index + one alphabetical page per letter. Returns page count."""
    tpl = (SRC / "browse.html").read_text(encoding="utf-8")
    (DIST / "browse").mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[dict[str, str]]] = {}
    for entry in index:
        groups.setdefault(_first_letter(entry["name"]), []).append(entry)

    def nav(active: str) -> str:
        cells = []
        for label in _LETTERS:
            if label in groups:
                cls = "active" if label == active else ""
                cells.append(
                    f'<a class="{cls}" href="/browse/{_letter_key(label)}">{label}</a>'
                )
            else:
                cells.append(f'<span class="off">{label}</span>')
        return '<nav class="azbar">' + "".join(cells) + "</nav>"

    def emit(key: str, title: str, desc: str, heading: str, active: str, body: str):
        page = (
            tpl.replace("{{TITLE}}", _esc(title))
            .replace("{{DESC}}", _esc(desc))
            .replace("{{HEADING}}", _esc(heading))
            .replace("{{NAV}}", nav(active))
            .replace("{{BODY}}", body)
        )
        out = DIST / "browse" / f"{key}.html"
        out.write_text(page, encoding="utf-8")
        compress(out)

    # Index: a grid of letters with counts.
    tiles = "".join(
        f'<li><a href="/browse/{_letter_key(l)}"><b>{l}</b>'
        f"<span>{len(groups[l])}</span></a></li>"
        for l in _LETTERS
        if l in groups
    )
    emit(
        "index",
        "Parcourir les médicaments de A à Z | justelesRCP",
        "Liste alphabétique de tous les médicaments avec leur RCP (ANSM / BDPM).",
        "Parcourir de A à Z",
        "",
        f'<ul class="letter-grid">{tiles}</ul>',
    )

    # One page per letter, entries sorted by folded name.
    for label, entries in groups.items():
        entries.sort(key=lambda e: _sort_key(e["name"]))
        items = "".join(
            f'<li><a href="/rcp/{e["slug"]}">{_esc(e["name"])}</a></li>'
            for e in entries
        )
        emit(
            _letter_key(label),
            f"Médicaments : {label} | justelesRCP",
            f"Médicaments dont le nom commence par « {label} » ({len(entries)}).",
            f"Médicaments : {label}",
            label,
            f'<ul class="drug-list">{items}</ul>',
        )

    return len(groups) + 1


def compress(path: Path) -> None:
    """Write .gz and .br siblings so Caddy can serve precompressed."""
    raw = path.read_bytes()
    (path.with_suffix(path.suffix + ".gz")).write_bytes(
        gzip.compress(raw, compresslevel=9)
    )
    # q10 is ~5x faster than q11 for a fraction of a percent more size.
    (path.with_suffix(path.suffix + ".br")).write_bytes(
        brotli.compress(raw, quality=10)
    )


# --- semantic-search: the served .vec.json sidecar -------------------------
# Per-drug section vectors are computed server-side now (the warm ONNX encoder in
# embed-service.py, or embed-rcp.py offline), NOT baked from data/emb here. These two
# helpers are the SHARED writer both use, so the served format has one definition.
def vec_payload(chunks, vecs, model: str, query_prefix: str, src_hash: str) -> dict:
    """Build one page's served .vec.json dict from its section chunks + float vectors.

    ``chunks`` is section_chunks()'s ``[(sec_id, snippet, chunk_text), ...]``; ``vecs``
    is an aligned iterable of L2-normalised float vectors (one per chunk). Each vector
    is int8-quantised (quantize_int8, the canonical formula) and base64-packed: the
    SAME wire format src/rcp-semsearch.js decodes. ``src_hash`` (sha256 of the raw
    overlay) is baked in as the self-describing staleness key, so a re-crawl that
    produced identical bytes hashes the same and the embedder skips re-embedding."""
    out_chunks = []
    dim = 0
    for (sec_id, snippet, _text), vec in zip(chunks, vecs):
        q = quantize_int8(list(vec))
        dim = len(q)
        b64 = base64.b64encode(struct.pack(f"{len(q)}b", *q)).decode("ascii")
        out_chunks.append({"sec": sec_id, "snippet": snippet, "q": b64})
    return {
        "model": model,
        "dim": dim,
        "query_prefix": query_prefix,
        "src_hash": src_hash,
        "chunks": out_chunks,
    }


def write_vec_json(dist_path: Path, payload: dict) -> None:
    """Write one ``dist/<rcp|eu>/<slug>.vec.json`` (+ .gz/.br via compress) atomically.
    The vectors live in this SEPARATE sidecar, never inline in the page HTML, so a
    page stays refresh-safe (the refresh service rewrites only the .html)."""
    dist_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = dist_path.with_name(dist_path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dist_path)
    compress(dist_path)


# --- per-record rendering, run in a worker process pool ---------------------
_NAMES: dict[str, str] = {}
_TPL = ""
_XREF: dict[str, tuple[str, str, str]] = {}
_SUBSTANCES: dict[str, str] = {}


def _init_worker(
    names: dict[str, str],
    tpl: str,
    xref: dict[str, tuple[str, str, str]] | None = None,
    substances: dict[str, str] | None = None,
) -> None:
    global _NAMES, _TPL, _XREF, _SUBSTANCES
    _NAMES, _TPL, _XREF = names, tpl, xref or {}
    _SUBSTANCES = substances or {}


def _toc_html(toc: list[tuple[str, str]]) -> str:
    """Sidebar navigation for one RCP: a collapsible 'Sommaire' listing the
    top-level sections, plus the runtime version slot. Empty string when a
    document exposes no sections (nothing to navigate)."""
    if not toc:
        return ""
    links = "".join(
        f'<li><a href="#{sec_id}">{_esc(title)}</a></li>' for sec_id, title in toc
    )
    # No `open`: ships collapsed so on phones it doesn't bury the top of the
    # document. On wide screens style.css reveals the content regardless of the
    # open state (permanent sidebar); src/toc.js snaps it shut after a section
    # link is tapped on phones.
    return (
        '<details class="toc">'
        "<summary>Sommaire</summary>"
        f'<nav aria-label="Sommaire"><ol>{links}</ol></nav>'
        '<p class="ver" data-app-version></p>'
        "</details>"
    )


def _asof_html(ansm: str, asof: str) -> str:
    """Top-of-page freshness banner, built from two distinct dates:

    - ``ansm``: ANSM's own 'Mis à jour le' revision date (see _ansm_date). This
      is the headline 'à jour au' date the reader sees, because it is when the
      official text was last revised. Baked as ``data-rcp-ansm``.
    - ``asof``: OUR capture date (BASELINE_DATE for the 2022 dump, or the scrape
      ``last_fetch`` for an overlay). Shown as a small 'vérifiée par justelesRCP
      le' line and baked as ``data-rcp-asof``; app-init.js also keys the >1-year
      'notre copie' notice AND the on-demand refresh off it, so it MUST stay on
      the element even though ``ansm`` is what we headline.

    Absolute dates are baked so no-JS readers still see them; app-init.js turns
    each into a relative age client-side, keeping the page cacheable. Returns ''
    when no date is known (nothing to show). If ANSM's date is somehow missing
    the capture date becomes the headline, so the banner is never dateless."""
    if not ansm and not asof:
        return ""
    attrs = ""
    if ansm:
        attrs += f' data-rcp-ansm="{_esc(ansm)}"'
    if asof:
        attrs += f' data-rcp-asof="{_esc(asof)}"'
    head = ansm or asof
    checked = (
        f'<span class="rcp-checked">Version vérifiée par justelesRCP le '
        f"{_esc(_fr_date(asof))}.</span>"
        if ansm and asof  # only worth showing when it differs from the headline
        else ""
    )
    return (
        f'<p class="rcp-asof"{attrs}>'
        f'<span class="rcp-primary">Informations à jour au '
        f"{_esc(_fr_date(head))}.</span>{checked}</p>"
    )


def _source_button(url: str, label: str) -> str:
    """One '.official-link' button (external link to an authoritative source)."""
    return (
        f'<a class="official-link" href="{_esc(url)}" '
        f'target="_blank" rel="noopener">{label}</a>'
    )


def _official_source_html(*buttons: str) -> str:
    """Top-of-page '.rcp-source' row of source buttons, so a reader who doubts
    our copy or spots a rendering bug can open the official one in one click.
    One button on RCP pages (ANSM fiche); two on /eu/ pages (EMA PDF + search).
    Each arg is a ``_source_button`` fragment."""
    return f'<p class="rcp-source">{" ".join(buttons)}</p>'


def _clean_substance(text: str) -> str:
    """Reduce a COMPO active-substance denomination to a clean search term: drop a
    trailing salt/qualifier in parentheses and anything after a comma, collapse
    whitespace. 'RANITIDINE (CHLORHYDRATE DE)' -> 'RANITIDINE'; 'ARIPIPRAZOLE' ->
    'ARIPIPRAZOLE'."""
    text = _stdhtml.unescape(text).split("(")[0].split(",")[0]
    return " ".join(text.split())


def load_substances() -> dict[str, str]:
    """CIS -> a substance search string for the external-reference links (HAS, EMA,
    Vidal), from CIS_COMPO_bdpm.txt.

    Column layout per BDPM spec: 0=Code_CIS, 3=active-substance denomination,
    6='SA' (substance active) / 'FT' (fraction thérapeutique). Only 'SA' rows are
    kept so a drug is searched on its named active substance rather than a salt
    fraction; each denomination is cleaned (_clean_substance) and distinct
    substances of a combination drug are joined with a space. Returns {} if the
    file is absent (or is a stray mount directory), so the build still runs (the
    pill row then falls back to the drug's brand root, see _ref_links_html)."""
    if not COMPO_PATH.is_file():
        return {}
    ordered: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    with COMPO_PATH.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t")
            if len(parts) <= 7 or parts[6].strip() != "SA":
                continue
            cis = parts[0].strip()
            sub = _clean_substance(parts[3])
            if not sub or sub.upper() in seen.setdefault(cis, set()):
                continue
            seen[cis].add(sub.upper())
            ordered.setdefault(cis, []).append(sub)
    return {cis: " ".join(subs) for cis, subs in ordered.items()}


def _ref_pill(url: str, label: str) -> str:
    """One '.ref-pill' external-reference button (opens in a new tab)."""
    return (
        f'<a class="ref-pill" href="{_esc(url)}" '
        f'target="_blank" rel="noopener">{_esc(label)}</a>'
    )


def _ref_links_html(cis: str, name: str, include_ema: bool = True) -> str:
    """Top-of-page '.rcp-refs' row of external-reference pill buttons: BDPM (this
    drug's official BDPM record, keyed by CIS) first, then HAS, EMA and Vidal
    full-text searches on the drug's active substance (falling back to its brand
    root when the composition is unknown). Vidal is last per the product intent.

    ``include_ema`` is False on /eu/ pages, which already carry a direct EMA button
    (render_eu_page / the stub), so the EMA pill is dropped there to avoid a
    duplicate. Reads the process-wide substance map primed by _init_worker (pool
    workers) or set in main()/the refresh service. Returns '' when there is no CIS
    to link."""
    if not cis:
        return ""
    pills = [_ref_pill(ANSM_PAGE_URL.format(cis=cis), "BDPM")]
    query = _SUBSTANCES.get(cis) or _brand_root(name)
    if query:
        q = urllib.parse.quote_plus(query.lower())
        pills.append(_ref_pill(HAS_SEARCH_URL.format(q=q), "HAS"))
        if include_ema:
            pills.append(_ref_pill(EMA_REF_URL.format(q=q), "EMA"))
        pills.append(_ref_pill(VIDAL_SEARCH_URL.format(q=q), "Vidal"))
    return f'<p class="rcp-refs">{"".join(pills)}</p>'


def render_record(item: tuple[str, str, str]) -> dict[str, str] | None:
    """Clean one RCP, write its page + precompressed siblings, return index row."""
    cis, raw, asof = item
    try:
        denom, cleaned, toc, xref_links = clean_rcp(raw, cis, _XREF)
    except Exception:  # a few dumps have malformed markup
        return None
    name = _NAMES.get(cis) or denom or f"RCP {cis}"
    slug = f"{cis}-{slugify(name)}"
    page = (
        _TPL.replace("{{TITLE}}", _esc(name))
        .replace("{{HEADEXTRA}}", "")  # RCP pages are indexable; only /eu/ stubs opt out
        .replace("{{CIS}}", _esc(cis))
        .replace("{{TOC}}", _toc_html(toc))
        .replace(
            "{{ASOF}}",
            _asof_html(_ansm_date(cleaned), asof)
            + _official_source_html(_source_button(
                ANSM_PAGE_URL.format(cis=cis),
                "Consulter le RCP officiel sur le site de l'ANSM →",
            ))
            + _ref_links_html(cis, name),
        )
        .replace("{{CONTENT}}", cleaned)
        .replace("{{XREF}}", _xref_html(xref_links))
    )
    out = DIST / "rcp" / f"{slug}.html"
    out.write_text(page, encoding="utf-8")
    compress(out)
    return {"cis": cis, "name": name, "slug": slug}


# --- EU-authorization stub pages -------------------------------------------
# ~15% of CIS have an empty ANSM RCP; a large share of those are centrally
# authorized (EMA "procédure centralisée"), whose RCP text lives at the EMA, not
# the ANSM (e.g. ABILIFY/aripiprazole, many biologics and oncology drugs). They
# render no normal page and so were unfindable by search. build_stubs() gives
# each a lightweight /eu/ landing that (a) keeps it in search-index.json so a
# search for the brand resolves, (b) links to the official RCP on the EMA site,
# and (c) when a same-substance generic actually renders here, links to it. Stubs
# are noindex, kept out of /browse, and kept out of the RCP cross-link graph (own
# /eu/ path, so page_cis_from_dist's dist/rcp glob never picks them up). NO EMA
# content is fetched: the EMA link is a search URL by brand name (never a dead
# deep link), so this stays 100% static and scrape-free.
_EU_NUM_RE = re.compile(r"EU/\d/\d{2}/\d+")
_EMA_SEARCH = "https://www.ema.europa.eu/en/medicines?search_api_fulltext="


def _brand_root(name: str) -> str:
    """Leading brand words of a drug name, up to the first token containing a digit.

    'ABILIFY MAINTENA 300 mg, poudre...' -> 'ABILIFY MAINTENA'; 'ABILIFY 10 mg,
    comprimé' -> 'ABILIFY'. Used as the EMA search query so the link targets the
    product family, not one strength. Falls back to the full name when it opens
    with a number (e.g. '5-FLUOROURACILE ...')."""
    words: list[str] = []
    for tok in name.replace(",", " ").split():
        if any(ch.isdigit() for ch in tok):
            break
        words.append(tok)
    return " ".join(words) or name


def _ema_search_url(name: str) -> str:
    """EMA medicines-search URL for a drug's brand root. In a browser it filters
    the EMA medicine finder to the product; with JS off it still lands on the
    valid finder page. A search (not a constructed EPAR deep link) so it can never
    404, and it fetches nothing from the EMA at build time."""
    return _EMA_SEARCH + urllib.parse.quote_plus(_brand_root(name))


def load_cap_meta() -> dict[str, tuple[str, str, str]]:
    """CIS -> (name, eu_number, holder) for centrally-authorized products.

    A row qualifies when it carries an EU/x/xx/xxx marketing-authorization number
    or its procedure column says 'centralisée'. eu_number/holder are '' when
    absent (holder is the field right after the EU number). Returns {} when
    CIS_bdpm.txt is missing. Parses the same latin-1 TSV as load_names but is kept
    separate so load_names' contract (also used by the refresh service) is
    untouched."""
    if not BDPM_PATH.is_file():
        return {}
    meta: dict[str, tuple[str, str, str]] = {}
    with BDPM_PATH.open(encoding="latin-1") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) < 2 or not row[0].strip():
                continue
            m = _EU_NUM_RE.search("\t".join(row))
            if not (m or any("centralis" in c.lower() for c in row)):
                continue
            eu = m.group(0) if m else ""
            holder = ""
            if m:
                for i, c in enumerate(row):
                    if _EU_NUM_RE.search(c) and i + 1 < len(row):
                        holder = row[i + 1].strip()
                        break
            meta[row[0].strip()] = (row[1].strip(), eu, holder)
    return meta


def _auth_key(name: str, eu: str) -> str:
    """Group key uniting every presentation of ONE centrally-authorized product.

    A product's EU marketing-authorization number (e.g. 'EU/1/13/882') is shared by
    all its strengths/pack sizes (the CIS_bdpm column carries the product-level
    number, not a per-presentation suffix), so it groups them exactly; when a
    'centralisée' row carries no EU number we fall back to the brand root. All
    presentations of a product point at the SAME EMA product-information PDF, so one
    harvested link + one converted overlay serves the whole group (see resolve_eu)."""
    return eu or _brand_root(name)


def auth_groups(cap: dict[str, tuple[str, str, str]]) -> dict[str, list[str]]:
    """Authorization key -> sorted member CIS, from load_cap_meta()'s output.

    Members share one EMA PDF/overlay (see _auth_key), so a presentation with no
    overlay of its own can borrow a sibling's. Sorted for deterministic borrowing
    (keeps the incremental cache stable across runs)."""
    groups: dict[str, list[str]] = {}
    for cis, (name, eu, _holder) in cap.items():
        groups.setdefault(_auth_key(name, eu), []).append(cis)
    for members in groups.values():
        members.sort()
    return groups


def _stub_content(
    name: str, eu: str, holder: str, generic: str | None, ema_pdf: str = ""
) -> str:
    """Body HTML for one /eu/ stub (fills the RCP template's {{CONTENT}} slot).

    When ``ema_pdf`` is set (the real EMA product-information PDF href the
    scraper harvested from this drug's ANSM page), the "official RCP" button
    links straight to that document; otherwise it falls back to an EMA brand
    search (never 404s, fetches nothing at build time)."""
    out = [
        # The drug/presentation name header is emitted by the page template
        # ({{TITLE}}), shared with RCP + full /eu/ pages, so it is not repeated here.
        '<div class="rcp-stub">',
        '<p class="stub-lead">Ce médicament bénéficie d\'une autorisation de mise '
        "sur le marché (AMM) <strong>européenne centralisée</strong>. Son résumé "
        "des caractéristiques du produit (RCP) n'est pas publié par l'ANSM, mais "
        "par l'Agence européenne des médicaments (EMA).</p>",
    ]
    bits = []
    if eu:
        bits.append(f"N° d'AMM européenne : <strong>{_esc(eu)}</strong>")
    if holder:
        bits.append(f"Titulaire : {_esc(holder)}")
    if bits:
        out.append('<p class="stub-meta">' + "<br>".join(bits) + "</p>")
    if ema_pdf:
        href, label = ema_pdf, "Consulter le RCP officiel (PDF) sur le site de l'EMA →"
    else:
        href, label = _ema_search_url(name), "Consulter le RCP officiel sur le site de l'EMA →"
    out.append(
        '<p class="stub-actions"><a class="stub-ema" href="'
        f'{_esc(href)}" target="_blank" rel="noopener">{label}</a></p>'
    )
    if generic:
        q = urllib.parse.quote_plus(generic)
        out.append(
            '<p class="stub-generics">Génériques à base de '
            f"<strong>{_esc(generic.capitalize())}</strong> disponibles sur ce "
            f'site : <a href="/?q={q}">voir les résultats</a>.</p>'
        )
    out.append(
        '<p class="stub-note">Cette page existe pour que ce médicament reste '
        "trouvable ici ; le texte réglementaire complet reste sur le site de "
        "l'EMA.</p></div>"
    )
    return "".join(out)


_EU_DATE_RE = re.compile(r'data-ema-date="([^"]*)"')
_EU_FETCHED_RE = re.compile(r'data-ema-fetched="([^"]*)"')
_EU_PDF_RE = re.compile(r'data-ema-pdf="([^"]*)"')


def _eu_date(overlay_html: str) -> str:
    """The PDF's ModDate scrape-ema.py baked onto the EMA overlay wrapper: when the
    EMA last revised the official text. Headlined as the page's 'à jour au …' date
    (the analog of an ANSM RCP's revision date)."""
    m = _EU_DATE_RE.search(overlay_html)
    return m.group(1) if m else ""


def _eu_fetched(overlay_html: str) -> str:
    """OUR capture date (this scrape's date) baked onto the overlay wrapper. Used as
    ``data-rcp-asof`` (the 'vérifiée par justelesRCP le …' line) and the on-demand
    refresh key: it advances on every re-fetch, so a refresh is detectable even when
    the ModDate is unchanged. "" for an overlay seeded before this feature (the
    caller then falls back to the ModDate)."""
    m = _EU_FETCHED_RE.search(overlay_html)
    return m.group(1) if m else ""


def _eu_pdf(overlay_html: str) -> str:
    """The source EMA PDF URL scrape-ema.py baked onto the overlay wrapper (the
    exact doc we converted), unescaped back to a raw URL. "" if absent (an overlay
    seeded before this feature); the caller then falls back to the EMA search."""
    m = _EU_PDF_RE.search(overlay_html)
    return _stdhtml.unescape(m.group(1)) if m else ""


def _eu_via_archive(overlay_html: str) -> bool:
    """True when scrape-ema.py had to recover this overlay's PDF from the Internet
    Archive (the live EMA URL failed), stamped as data-ema-archive="1". Drives a
    visible note on the page. The source button still points at the real EMA PDF,
    never the archive URL (which we deliberately never expose)."""
    return 'data-ema-archive="1"' in overlay_html


def _eu_overlay_cached(cis: str, memo: dict[str, str]) -> str:
    """Read a CIS's converted EMA overlay HTML once, memoised (resolve_eu may probe
    the same sibling for several presentations of one product)."""
    if cis not in memo:
        path = _overlay_path(cis, EU_OVERLAY_DIR)
        memo[cis] = _read_overlay(path) if path is not None else ""
    return memo[cis]


def resolve_eu(cis: str, cap: dict[str, tuple[str, str, str]],
               groups: dict[str, list[str]], links: dict[str, str],
               memo: dict[str, str] | None = None) -> tuple[str, str]:
    """(overlay_html, pdf_url) for one /eu/ CIS, SHARED across its authorization group.

    All presentations of a centrally-authorized product publish one EMA PDF, so a
    presentation with no converted overlay/link of its own borrows a sibling's (see
    _auth_key/auth_groups). Preference: the CIS's own overlay + harvested link; else
    the freshest sibling overlay (ties broken by CIS, for a deterministic pick that
    doesn't churn the incremental cache) and any sibling's harvested link.
    ``overlay_html`` is "" when no group member has one yet; ``pdf_url`` is "" when
    no member has a harvested link AND no overlay bakes one. ``memo`` caches overlay
    reads across calls. Used by build_stubs (batch) and the refresh service
    (_eu_url, on-demand + crawler)."""
    memo = {} if memo is None else memo
    meta = cap.get(cis)
    key = _auth_key(meta[0], meta[1]) if meta else None
    own = _eu_overlay_cached(cis, memo)
    link = links.get(cis, "")
    if own.strip():
        return own, link or _eu_pdf(own)
    best_html, best_rank = "", None
    for sib in groups.get(key, ()):  # sorted members
        if sib == cis:
            continue
        if not link:  # borrow a harvested link even if no sibling overlay exists yet
            link = links.get(sib, "")
        html = _eu_overlay_cached(sib, memo)
        if html.strip():
            rank = (_eu_fetched(html) or _eu_date(html), sib)  # freshest, then CIS
            if best_rank is None or rank > best_rank:
                best_html, best_rank = html, rank
    if best_html:
        return best_html, link or _eu_pdf(best_html)
    return "", link


def _eu_pdf_url(overlay_html: str, name: str, fallback: str = "") -> str:
    """Resolve the 'consulter le PDF officiel' target for a /eu/ page: the exact
    PDF baked on the overlay, else a caller-supplied link, else the EMA brand
    search (never 404s). Shared by build_stubs (cache key) and render_eu_page."""
    return _eu_pdf(overlay_html) or fallback or _ema_search_url(name)


def _eu_toc(overlay_html: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Two-level ToC parsed from an EMA overlay: each collapsible ``<details>``
    group (its ``<summary id>``), with the numbered sections (``<h2 id>``) of the
    open group (the SmPC) as children. Mirrors ema_pdf.convert's structure."""
    try:
        root = lxml_html.fromstring(overlay_html)
    except Exception:
        return []
    groups = []
    for det in root.findall(".//details"):
        summ = det.find("summary")
        if summ is None or not summ.get("id"):
            continue
        subs = []
        if det.get("open") is not None:  # only the open group (SmPC) lists sections
            for h in det.findall(".//h2"):
                if h.get("id"):
                    subs.append((h.get("id"), h.text_content().strip()))
        groups.append((summ.get("id"), summ.text_content().strip(), subs))
    return groups


def _eu_toc_html(groups: list[tuple[str, str, list[tuple[str, str]]]]) -> str:
    """Sidebar ToC for a full /eu/ page: same ``<details class="toc">`` shell as
    _toc_html but two levels deep (annexes > SmPC sections)."""
    if not groups:
        return ""
    lis = []
    for sid, label, subs in groups:
        sub = ""
        if subs:
            sub = "<ol>" + "".join(
                f'<li><a href="#{s}">{_esc(lbl)}</a></li>' for s, lbl in subs
            ) + "</ol>"
        lis.append(f'<li><a href="#{sid}">{_esc(label)}</a>{sub}</li>')
    return (
        '<details class="toc"><summary>Sommaire</summary>'
        f'<nav aria-label="Sommaire"><ol>{"".join(lis)}</ol></nav>'
        '<p class="ver" data-app-version></p></details>'
    )


def _eu_full_content(name: str, eu: str, holder: str, overlay_html: str) -> str:
    """Body of a full /eu/ page: a short EU-authorization lead + EU number/holder,
    then the converted EMA document. The drug/presentation name header is emitted by
    the page template ({{TITLE}}), shared with RCP + stub pages, so it is not
    repeated here. The EMA source buttons + freshness banner are placed in the
    {{ASOF}} slot by build_stubs (same as RCP pages)."""
    bits = []
    if eu:
        bits.append(f"N° d'AMM européenne : <strong>{_esc(eu)}</strong>")
    if holder:
        bits.append(f"Titulaire : {_esc(holder)}")
    meta = ('<p class="stub-meta">' + "<br>".join(bits) + "</p>") if bits else ""
    lead = (
        '<p class="stub-lead">Ce médicament bénéficie d\'une autorisation de mise '
        "sur le marché <strong>européenne centralisée</strong>. Le texte ci-dessous "
        "est le résumé des caractéristiques du produit (et sa notice) publié par "
        "l'Agence européenne des médicaments (EMA), converti par justelesRCP depuis "
        "le PDF officiel. En cas de doute, reportez-vous au PDF ci-dessus.</p>"
    )
    # When the PDF was recovered from the Internet Archive (the live EMA was down at
    # capture time), tell the reader. We surface the fact, not the archive URL.
    archive_note = (
        '<p class="stub-lead rcp-archive-note">Ce texte a été récupéré via une '
        "archive web (Internet Archive) car le PDF n'était pas accessible sur le "
        "site de l'EMA au moment de la capture. Reportez-vous au PDF officiel de "
        "l'EMA ci-dessus pour la version en vigueur.</p>"
    ) if _eu_via_archive(overlay_html) else ""
    return f'<div class="rcp-eu">{lead}{meta}{archive_note}</div>{overlay_html}'


def render_eu_page(cis: str, overlay_html: str, meta: tuple[str, str, str] | None,
                   page_tpl: str, pdf_fallback: str = "") -> dict | None:
    """Render ONE full /eu/ page from its converted EMA overlay + cap meta.

    The single-page counterpart of render_record for the /eu/ side, reused by BOTH
    build_stubs (batch) and the refresh service (on-demand, after re-fetching one
    EMA PDF). Writes dist/eu/<slug>.html (+ .gz/.br) and returns the index row, or
    None if the overlay is empty / the CIS has no cap meta. The freshness banner
    mirrors an ANSM page: the PDF's ModDate is the 'à jour au' headline
    (data-rcp-ansm) and our fetch date the 'vérifiée le' line + refresh key
    (data-rcp-asof, so the on-demand button lights up and detects the update)."""
    if not meta or not overlay_html.strip():
        return None
    name, eu, holder = meta
    slug = f"{cis}-{slugify(name)}"
    pdf_url = _eu_pdf_url(overlay_html, name, pdf_fallback)
    asof = _asof_html(_eu_date(overlay_html), _eu_fetched(overlay_html) or _eu_date(overlay_html)) \
        + _official_source_html(
            _source_button(pdf_url, "Consulter le RCP officiel (PDF) sur le site de l'EMA →"),
            _source_button(_ema_search_url(name), "Recherche EMA →"),
        ) \
        + _ref_links_html(cis, name, include_ema=False)
    page = (
        page_tpl.replace("{{TITLE}}", _esc(name))
        .replace("{{HEADEXTRA}}", '<meta name="robots" content="noindex">')
        .replace("{{CIS}}", _esc(cis))
        .replace("{{TOC}}", _eu_toc_html(_eu_toc(overlay_html)))
        .replace("{{ASOF}}", asof)
        .replace("{{CONTENT}}", _eu_full_content(name, eu, holder, overlay_html))
        .replace("{{XREF}}", "")
    )
    out = DIST / "eu" / f"{slug}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    compress(out)
    return {"cis": cis, "name": name, "slug": slug, "eu": 1}


def build_stubs(
    real_index: list[dict[str, str]], page_tpl: str, prev_records: dict
) -> tuple[list[dict], dict, int, int]:
    """Render /eu/ landing pages for centrally-authorized, page-less drugs.

    Returns (stub_index, stub_records, reused, rendered). stub_index rows carry
    eu=1 so search.js routes them to /eu/ and write_browse can exclude them;
    stub_records feed the SAME incremental manifest as real pages (stub CIS never
    collide with RCP CIS: one has an empty RCP, the other does not). Reuses a
    cached output when its content hash is unchanged, exactly like render_record.
    The content hash omits the template/code, which the global key already
    guards."""
    cap = load_cap_meta()
    # Direct EMA PDF links harvested by the scraper (CIS -> url); a presentation
    # whose CIS (or a sibling's, see below) is present links straight at the doc,
    # others fall back to the EMA search.
    ema_links = _load_ema_links()
    # Authorization groups: every strength/pack of one centrally-authorized product
    # shares ONE EMA PDF, so a presentation with no overlay/link of its own borrows a
    # sibling's (resolve_eu). This is what makes e.g. ABILIFY MAINTENA 300 mg render
    # the same full converted page as 400 mg the moment either sibling is scraped,
    # instead of staying a bare stub. The overlay-read memo is shared across the loop.
    groups = auth_groups(cap)
    overlay_memo: dict[str, str] = {}
    eu_dir = DIST / "eu"

    def _prune(keep: set[str]) -> None:
        if not eu_dir.is_dir():
            return
        for page in eu_dir.glob("*.html"):
            if page.stem not in keep:
                page.unlink()
                page.with_suffix(".html.gz").unlink(missing_ok=True)
                page.with_suffix(".html.br").unlink(missing_ok=True)
                # Orphaned semantic-search sidecar (written by the embedder) too.
                for suf in (".vec.json", ".vec.json.gz", ".vec.json.br"):
                    (page.parent / (page.stem + suf)).unlink(missing_ok=True)

    if not cap:  # no BDPM file: build nothing, sweep away any prior stubs
        _prune(set())
        return [], {}, 0, 0

    rendered_cis = {e["cis"] for e in real_index}
    stub_cis = [c for c in cap if c not in rendered_cis]
    # A generics link is offered ONLY when a same-substance drug actually renders
    # here: intersect each stub's active-substance tokens (COMPO col 3, reusing
    # bdpm) with tokens present in real page names, dropping salts/short words.
    page_tokens: set[str] = set()
    for e in real_index:
        page_tokens |= bdpm.tokens(e["name"])
    compo = bdpm.column_tokens(COMPO_PATH, 0, 3, keep=set(stub_cis))

    eu_dir.mkdir(parents=True, exist_ok=True)
    stub_index: list[dict] = []
    stub_records: dict = {}
    reused = 0
    for cis in stub_cis:
        name, eu, holder = cap[cis]
        slug = f"{cis}-{slugify(name)}"
        out = eu_dir / f"{slug}.html"
        # If this drug's EMA PDF has been fetched + converted (its own overlay OR a
        # sibling presentation's, since they share one PDF), render the full
        # converted SmPC/notice on-site (render_eu_page); otherwise a lightweight
        # stub that only points out. resolve_eu also yields the best PDF link (own,
        # else a sibling's) so even a stub points at the real doc, not a search.
        # Overlays live in EU_OVERLAY_DIR (never RCP's); a zero-byte one decodes "".
        overlay, link = resolve_eu(cis, cap, groups, ema_links, overlay_memo)
        full = bool(overlay.strip())
        if full:
            # The whole converted doc drives the output, so the cache key is the
            # overlay itself (a re-scrape changes it) plus the metadata rendered
            # around it. The "full" tag busts the cache if a page flips stub<->full.
            # pdf_url is resolved the SAME way render_eu_page does, so the key and
            # the rendered page stay in step.
            pdf_url = _eu_pdf_url(overlay, name, link)
            h = hashlib.sha256(
                "\x00".join(("full", overlay, name, eu, holder, pdf_url)).encode("utf-8")
            ).hexdigest()
        else:
            # Lightweight stub: no EMA text on-site yet, just a pointer out, plus a
            # same-substance generic link when one actually renders here. Intersect
            # the stub's active-substance tokens (COMPO col 3) with tokens present
            # in real page names, dropping salts/short words.
            hits = {
                t for t in compo.get(cis, set())
                if len(t) >= _XREF_MIN_LEN and t not in _XREF_STOP
            } & page_tokens
            # Longest token, breaking ties alphabetically. The alphabetical tiebreak
            # is load-bearing: `hits` is a set, whose iteration order varies between
            # runs (hash randomization), so `max(..., key=len)` alone would pick a
            # different term on ties each build, churning the incremental cache.
            generic = max(hits, key=lambda t: (len(t), t)).lower() if hits else None
            content = _stub_content(name, eu, holder, generic, link)
            h = hashlib.sha256(("stub\x00" + content).encode("utf-8")).hexdigest()

        prev = prev_records.get(cis)
        if (
            prev and prev.get("h") == h and out.exists()
            and out.with_suffix(".html.gz").exists()
            and out.with_suffix(".html.br").exists()
        ):
            reused += 1
        elif full:
            render_eu_page(cis, overlay, (name, eu, holder), page_tpl, link)
        else:
            page = (
                page_tpl.replace("{{TITLE}}", _esc(name))
                .replace("{{HEADEXTRA}}", '<meta name="robots" content="noindex">')
                .replace("{{CIS}}", _esc(cis))
                .replace("{{TOC}}", "")
                .replace("{{ASOF}}", _ref_links_html(cis, name, include_ema=False))
                .replace("{{CONTENT}}", content)
                .replace("{{XREF}}", "")
            )
            out.write_text(page, encoding="utf-8")
            compress(out)
        stub_index.append({"cis": cis, "name": name, "slug": slug, "eu": 1})
        stub_records[cis] = {"h": h, "name": name, "slug": slug, "eu": 1}

    _prune({e["slug"] for e in stub_index})
    return stub_index, stub_records, reused, len(stub_cis) - reused


def main() -> None:
    # Either input source suffices: the 2022 baseline CSV or a scraped overlay
    # dir (scrape-rcp.py can run standalone without the bulk dump present).
    if not CSV_PATH.exists() and not RCP_OVERLAY_DIR.is_dir():
        sys.exit(f"missing {CSV_PATH} and {RCP_OVERLAY_DIR} (see README / download-data.sh)")

    print(f"build justelesRCP v{__version__}")
    names = load_names()
    # CIS that actually render a page (non-empty baseline cell or overlay). Link
    # targets are restricted to this set so a backlink never points at a pageless
    # CIS. Baseline presence is cached (frozen CSV), so this is cheap after the
    # first build; overlays are layered on each build. See _present_cis.
    present = _present_cis()
    # Cross-drug backlink index (term -> canonical target page). Built once and
    # shared read-only with every worker; empty when the BDPM inputs are absent.
    xref = build_xref_index(names, present)
    print(f"  cross-drug backlink terms: {len(xref)}")
    # Active-substance search strings for the external-reference pill row (HAS, EMA,
    # Vidal). Set as a process-wide global so build_stubs / render_eu_page, which run
    # in THIS process, can read it; the pool workers get their own copy via the
    # _init_worker initargs below.
    global _SUBSTANCES
    _SUBSTANCES = load_substances()
    print(f"  active-substance links: {len(_SUBSTANCES)}")
    # Per-CIS scrape dates stamp overlay pages with a real "as of" date; baseline
    # pages fall back to BASELINE_DATE. Loaded once, read inside records().
    scrape_dates = _load_scrape_dates()

    (DIST / "rcp").mkdir(parents=True, exist_ok=True)  # kept: incremental reuse

    page_tpl = (SRC / "rcp.html").read_text(encoding="utf-8")

    # Incremental cache: reuse a record's page when its inputs are unchanged and
    # its output files still exist. A build-code or template change flips the
    # global key and forces a full rebuild; a version-only bump does not.
    global_key = _global_key(page_tpl, xref, _SUBSTANCES)
    prev = _load_manifest()
    prev_records = prev.get("records", {}) if prev.get("global") == global_key else {}

    index: list[dict[str, str]] = []
    new_records: dict[str, dict[str, str]] = {}
    miss_hashes: dict[str, str] = {}  # cis -> record hash for pages we (re)render
    skipped_empty = 0
    reused = 0

    def records():
        """Yield (cis, raw, asof) for non-empty RCPs; count empties as a side effect.

        Thin wrapper over the module-level iter_rcp_raw() (shared with embed-rcp.py);
        the empties it skips are tallied into skipped_empty for the final report.
        """
        nonlocal skipped_empty
        stats = {"empty": 0}
        yield from iter_rcp_raw(scrape_dates, stats)
        skipped_empty += stats["empty"]

    def output_ok(slug: str) -> bool:
        """True when a slug's page and both precompressed siblings still exist."""
        page = DIST / "rcp" / f"{slug}.html"
        return (
            page.exists()
            and page.with_suffix(".html.gz").exists()
            and page.with_suffix(".html.br").exists()
        )

    def misses():
        """Yield (cis, raw, asof) only for records that must be (re)rendered.

        Cache hits are appended straight to the index here (no parsing, no
        compression) as the pool pulls this generator, keeping memory streaming.
        """
        nonlocal reused
        for cis, raw, asof in records():
            rec_hash = _record_hash(raw, names.get(cis, ""), asof)
            hit = prev_records.get(cis)
            if hit and hit.get("h") == rec_hash and output_ok(hit["slug"]):
                index.append({"cis": cis, "name": hit["name"], "slug": hit["slug"]})
                new_records[cis] = {"h": rec_hash, "name": hit["name"], "slug": hit["slug"]}
                reused += 1
                continue
            miss_hashes[cis] = rec_hash
            yield cis, raw, asof

    # Parsing + brotli are CPU-bound and independent per record -> fan out.
    workers = max(1, (os.cpu_count() or 2) - 1)
    print(f"  rendering with {workers} workers ({len(prev_records)} cached)...")
    with Pool(
        workers, initializer=_init_worker, initargs=(names, page_tpl, xref, _SUBSTANCES)
    ) as pool:
        for i, entry in enumerate(
            pool.imap_unordered(render_record, misses(), chunksize=8)
        ):
            if entry is not None:
                index.append(entry)
                cis = entry["cis"]
                new_records[cis] = {
                    "h": miss_hashes[cis],
                    "name": entry["name"],
                    "slug": entry["slug"],
                }
            if (i + 1) % 2000 == 0:
                print(f"  {i + 1} rendered...")

    # EU-authorization stubs: findable landing pages for centrally-authorized
    # drugs whose RCP lives at the EMA (empty ANSM cell -> no normal RCP page).
    # Built after the real index so we know which CIS already render a page. Their
    # records share the same manifest; their pages live under dist/eu (own URL
    # space, so they stay out of the RCP cross-link graph). See build_stubs.
    stub_index, stub_records, stub_reused, stub_rendered = build_stubs(
        index, page_tpl, prev_records
    )
    new_records.update(stub_records)

    # Prune outputs left behind by renamed slugs or CIS dropped from the source.
    keep = {e["slug"] for e in index}
    pruned = 0
    for page in (DIST / "rcp").glob("*.html"):
        if page.stem not in keep:
            page.unlink()
            page.with_suffix(".html.gz").unlink(missing_ok=True)
            page.with_suffix(".html.br").unlink(missing_ok=True)
            # Drop the orphaned semantic-search sidecar too (written by the embedder).
            for suf in (".vec.json", ".vec.json.gz", ".vec.json.br"):
                (page.parent / (page.stem + suf)).unlink(missing_ok=True)
            pruned += 1

    MANIFEST_PATH.write_text(
        json.dumps({"global": global_key, "records": new_records}), encoding="utf-8"
    )

    # Homepage + assets. search-index.json = real RCP pages + EU stubs, sorted by
    # CIS so the file is stable across runs (imap_unordered returns pages in
    # arbitrary order); a stable file avoids needless recompression churn and rsync
    # transfers on unchanged data. Browse (below) gets the real RCP pages only.
    search_rows = sorted(index + stub_index, key=lambda e: e["cis"])
    idx_json = json.dumps(search_rows, ensure_ascii=False, separators=(",", ":"))
    (DIST / "search-index.json").write_text(idx_json, encoding="utf-8")
    # The version is served at runtime (window.__APP_VERSION__) and injected into
    # the page by src/app-init.js, so it is NOT baked into page HTML. This keeps
    # rendered pages independent of the version, so a version bump alone does not
    # invalidate the incremental cache above.
    (DIST / "app-version.js").write_text(
        f'window.__APP_VERSION__ = "{__version__}";\n', encoding="utf-8"
    )
    # app-config.js is the local-dev fallback (empty config); in the container
    # docker/Caddyfile serves a per-startup rendered copy from a tmpfs instead.
    # app-init.js (umami + version) + dev-banner.js consume window.__APP_CONFIG__.
    static_assets = (
        "index.html",
        "a-propos.html",
        "style.css",
        "search.js",
        "app-config.js",
        "app-init.js",
        "dev-banner.js",
        "toc.js",
        "rcp-semsearch.js",
    )
    for asset in static_assets:
        shutil.copy(SRC / asset, DIST / asset)
    for f in (*static_assets, "app-version.js", "search-index.json"):
        compress(DIST / f)

    # Per-drug semantic search: the section vectors (dist/<slug>.vec.json) are now
    # written server-side by the embed service (or embed-rcp.py offline), NOT baked
    # here; build.py only prunes orphaned .vec.json above when a slug is dropped.

    browse_pages = write_browse(index)

    print(
        f"done: {len(index)} RCP pages ({reused} reused, {pruned} pruned) "
        f"+ {len(stub_index)} EU stubs ({stub_reused} reused, {stub_rendered} built) "
        f"+ {browse_pages} browse pages ({skipped_empty} empty CIS skipped) -> {DIST}"
    )


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    main()

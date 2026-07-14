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

import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime
from multiprocessing import Pool
from pathlib import Path

import brotli
from lxml import html as lxml_html

import bdpm  # shared, pure-stdlib BDPM tokenising + frequency scoring

__version__ = "0.14.1"  # single source of truth; bump patch/minor per change

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


def _global_key(page_tpl: str, xref: dict[str, tuple[str, str, str]]) -> str:
    """Cache key that invalidates every record when the build code, the template,
    OR the cross-drug backlink index change.

    The backlink dictionary must be folded in because a page's injected links
    depend on the WHOLE index, not just that record's own inputs (which
    _record_hash covers): adding a drug can introduce a new term that changes an
    unrelated page's body, so a changed index has to bust the whole cache."""
    h = hashlib.sha256()
    h.update(_code_fingerprint())
    h.update(b"\0")
    h.update(page_tpl.encode("utf-8"))
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


def _overlay_path(cis: str) -> Path | None:
    """Return the overlay file for a CIS, or None if none exists.

    scrape-rcp.py stores each overlay either plain (``<cis>.html``) or gzipped
    (``<cis>.html.gz``), depending on RCP_OVERLAY_GZIP at scrape time, and keeps
    only one of the two. We read whichever is present transparently; if both
    somehow coexist (format flipped mid-cache), the newest by mtime wins.
    """
    cands = [
        p for p in (RCP_OVERLAY_DIR / f"{cis}.html.gz", RCP_OVERLAY_DIR / f"{cis}.html")
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

    denom = _denomination(doc)

    body = doc.xpath("//div[@id='textDocument']")
    inner = body[0] if body else doc
    toc = _build_toc(inner)
    # Inject cross-drug backlinks after the toc is built (so section ids/titles
    # are captured from the untouched headings) but before serialisation.
    xref_links = _linkify(inner, xref or {}, cis)
    cleaned = "".join(
        lxml_html.tostring(c, encoding="unicode") for c in inner.iterchildren()
    )
    return denom, cleaned, toc, xref_links


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


# --- per-record rendering, run in a worker process pool ---------------------
_NAMES: dict[str, str] = {}
_TPL = ""
_XREF: dict[str, tuple[str, str, str]] = {}


def _init_worker(
    names: dict[str, str], tpl: str, xref: dict[str, tuple[str, str, str]] | None = None
) -> None:
    global _NAMES, _TPL, _XREF
    _NAMES, _TPL, _XREF = names, tpl, xref or {}


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
        .replace("{{CIS}}", _esc(cis))
        .replace("{{TOC}}", _toc_html(toc))
        .replace("{{ASOF}}", _asof_html(_ansm_date(cleaned), asof))
        .replace("{{CONTENT}}", cleaned)
        .replace("{{XREF}}", _xref_html(xref_links))
    )
    out = DIST / "rcp" / f"{slug}.html"
    out.write_text(page, encoding="utf-8")
    compress(out)
    return {"cis": cis, "name": name, "slug": slug}


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
    # Per-CIS scrape dates stamp overlay pages with a real "as of" date; baseline
    # pages fall back to BASELINE_DATE. Loaded once, read inside records().
    scrape_dates = _load_scrape_dates()

    (DIST / "rcp").mkdir(parents=True, exist_ok=True)  # kept: incremental reuse

    page_tpl = (SRC / "rcp.html").read_text(encoding="utf-8")

    # Incremental cache: reuse a record's page when its inputs are unchanged and
    # its output files still exist. A build-code or template change flips the
    # global key and forces a full rebuild; a version-only bump does not.
    global_key = _global_key(page_tpl, xref)
    prev = _load_manifest()
    prev_records = prev.get("records", {}) if prev.get("global") == global_key else {}

    index: list[dict[str, str]] = []
    new_records: dict[str, dict[str, str]] = {}
    miss_hashes: dict[str, str] = {}  # cis -> record hash for pages we (re)render
    skipped_empty = 0
    reused = 0

    def _overlay(cis: str) -> str | None:
        """Return the scraped overlay HTML for a CIS, or None if no overlay file.

        A present overlay file always supersedes the CSV baseline cell (fresher
        data). An empty file is a real value ("scraped, no RCP") and is returned
        as "" so the caller skips it rather than falling back to the stale cell.
        """
        if not RCP_OVERLAY_DIR.is_dir():
            return None
        path = _overlay_path(cis)
        return _read_overlay(path) if path is not None else None

    def records():
        """Yield (cis, raw, asof) for non-empty RCPs; count empties as a side effect.

        Sources are merged with the overlay winning: for each CIS the scraped
        data/rcp/<cis>.html is used when present, else the 2022 CSV cell. Overlay
        files whose CIS is absent from the baseline CSV are yielded afterwards.
        asof is the page's freshness date: the scrape date for overlay data
        (manifest, else the overlay file's mtime), or BASELINE_DATE for a CSV cell.
        """
        nonlocal skipped_empty
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
                        skipped_empty += 1  # no published RCP (or empty overlay)
                        continue
                    yield cis, raw, asof
        # Overlay-only drugs: scraped CIS that never existed in the 2022 baseline.
        # Collect CIS from both formats (<cis>.html and <cis>.html.gz); the CIS is
        # the filename up to the first dot (8 digits, never dotted). Read through
        # _overlay so gz/plain and the newest-wins rule stay in one place.
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
                    skipped_empty += 1
                    continue
                yield cis, raw, scrape_dates.get(cis) or _overlay_date(cis)

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
    with Pool(workers, initializer=_init_worker, initargs=(names, page_tpl, xref)) as pool:
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

    # Prune outputs left behind by renamed slugs or CIS dropped from the source.
    keep = {e["slug"] for e in index}
    pruned = 0
    for page in (DIST / "rcp").glob("*.html"):
        if page.stem not in keep:
            page.unlink()
            page.with_suffix(".html.gz").unlink(missing_ok=True)
            page.with_suffix(".html.br").unlink(missing_ok=True)
            pruned += 1

    MANIFEST_PATH.write_text(
        json.dumps({"global": global_key, "records": new_records}), encoding="utf-8"
    )

    # Homepage + assets. Sorted by CIS so search-index.json is stable across runs
    # (imap_unordered returns pages in arbitrary order); a stable file avoids
    # needless recompression churn and rsync transfers on unchanged data.
    index.sort(key=lambda e: e["cis"])
    idx_json = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
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
    )
    for asset in static_assets:
        shutil.copy(SRC / asset, DIST / asset)
    for f in (*static_assets, "app-version.js", "search-index.json"):
        compress(DIST / f)

    browse_pages = write_browse(index)

    print(
        f"done: {len(index)} RCP pages ({reused} reused, {pruned} pruned) "
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

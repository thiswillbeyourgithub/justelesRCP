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

__version__ = "0.7.0"  # single source of truth; bump patch/minor per change

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = ROOT / "src"
DIST = ROOT / "dist"

CSV_PATH = DATA / "CIS_RCP.csv"
BDPM_PATH = DATA / "CIS_bdpm.txt"
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


def _global_key(page_tpl: str) -> str:
    """Cache key that invalidates every record when build code/template change."""
    h = hashlib.sha256()
    h.update(_code_fingerprint())
    h.update(b"\0")
    h.update(page_tpl.encode("utf-8"))
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


def _overlay_date(cis: str) -> str:
    """Fallback 'as of' date for an overlay with no manifest entry: the overlay
    file's own modification date. '' if it cannot be read."""
    try:
        ts = (RCP_OVERLAY_DIR / f"{cis}.html").stat().st_mtime
        return date.fromtimestamp(ts).isoformat()
    except OSError:
        return ""


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


def clean_rcp(raw: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (denomination, cleaned_inner_html, toc) for one RCP document.

    Keeps the semantic structure (section anchors, headings, paragraphs) but
    removes ANSM decoration and inline font styling so style.css can reskin it.
    The toc is the list of top-level sections for the sidebar navigation.
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
    cleaned = "".join(
        lxml_html.tostring(c, encoding="unicode") for c in inner.iterchildren()
    )
    return denom, cleaned, toc


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


def _init_worker(names: dict[str, str], tpl: str) -> None:
    global _NAMES, _TPL
    _NAMES, _TPL = names, tpl


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


def _asof_html(asof: str) -> str:
    """Top-of-page freshness banner: the absolute 'as of' date, baked so that
    no-JS readers still see it. app-init.js turns it into a relative age
    ('il y a X') and flags data older than a year, client-side, so the page stays
    cacheable and the age stays correct without a rebuild. Empty string when the
    date is unknown (nothing to show)."""
    if not asof:
        return ""
    return (
        f'<p class="rcp-asof" data-rcp-asof="{_esc(asof)}">'
        f"Informations à jour au {_esc(_fr_date(asof))}.</p>"
    )


def render_record(item: tuple[str, str, str]) -> dict[str, str] | None:
    """Clean one RCP, write its page + precompressed siblings, return index row."""
    cis, raw, asof = item
    try:
        denom, cleaned, toc = clean_rcp(raw)
    except Exception:  # a few dumps have malformed markup
        return None
    name = _NAMES.get(cis) or denom or f"RCP {cis}"
    slug = f"{cis}-{slugify(name)}"
    page = (
        _TPL.replace("{{TITLE}}", _esc(name))
        .replace("{{CIS}}", _esc(cis))
        .replace("{{TOC}}", _toc_html(toc))
        .replace("{{ASOF}}", _asof_html(asof))
        .replace("{{CONTENT}}", cleaned)
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
    # Per-CIS scrape dates stamp overlay pages with a real "as of" date; baseline
    # pages fall back to BASELINE_DATE. Loaded once, read inside records().
    scrape_dates = _load_scrape_dates()

    (DIST / "rcp").mkdir(parents=True, exist_ok=True)  # kept: incremental reuse

    page_tpl = (SRC / "rcp.html").read_text(encoding="utf-8")

    # Incremental cache: reuse a record's page when its inputs are unchanged and
    # its output files still exist. A build-code or template change flips the
    # global key and forces a full rebuild; a version-only bump does not.
    global_key = _global_key(page_tpl)
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
        path = RCP_OVERLAY_DIR / f"{cis}.html"
        return path.read_text(encoding="utf-8") if path.exists() else None

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
        if RCP_OVERLAY_DIR.is_dir():
            for path in sorted(RCP_OVERLAY_DIR.glob("*.html")):
                cis = path.stem
                if cis in seen:
                    continue
                raw = path.read_text(encoding="utf-8")
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
    with Pool(workers, initializer=_init_worker, initargs=(names, page_tpl)) as pool:
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

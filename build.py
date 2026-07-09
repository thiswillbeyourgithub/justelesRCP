# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
#   "brotli>=1.1",
# ]
# ///
"""Build the static justelesRCP site from the ANSM RCP dump.

Pipeline (id + raw ANSM html) -> (id + cleaned, reskinned static page):

  data/CIS_RCP.csv   TSV: Code_CIS <TAB> RCP_html (CSV-quoted, multi-line)
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
import json
import os
import re
import shutil
import sys
import unicodedata
from multiprocessing import Pool
from pathlib import Path

import brotli
from lxml import html as lxml_html

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = ROOT / "src"
DIST = ROOT / "dist"

CSV_PATH = DATA / "CIS_RCP.csv"
BDPM_PATH = DATA / "CIS_bdpm.txt"

# The RCP HTML field can be very large; lift the csv field-size ceiling.
csv.field_size_limit(sys.maxsize)


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


def clean_rcp(raw: str) -> tuple[str, str]:
    """Return (denomination, cleaned_inner_html) for one RCP document.

    Keeps the semantic structure (section anchors, headings, paragraphs) but
    removes ANSM decoration and inline font styling so style.css can reskin it.
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
    cleaned = "".join(
        lxml_html.tostring(c, encoding="unicode") for c in inner.iterchildren()
    )
    return denom, cleaned


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


def render_record(item: tuple[str, str]) -> dict[str, str] | None:
    """Clean one RCP, write its page + precompressed siblings, return index row."""
    cis, raw = item
    try:
        denom, cleaned = clean_rcp(raw)
    except Exception:  # a few dumps have malformed markup
        return None
    name = _NAMES.get(cis) or denom or f"RCP {cis}"
    slug = f"{cis}-{slugify(name)}"
    page = (
        _TPL.replace("{{TITLE}}", _esc(name))
        .replace("{{CIS}}", _esc(cis))
        .replace("{{CONTENT}}", cleaned)
    )
    out = DIST / "rcp" / f"{slug}.html"
    out.write_text(page, encoding="utf-8")
    compress(out)
    return {"cis": cis, "name": name, "slug": slug}


def main() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"missing {CSV_PATH} (see README / download-data.sh)")

    print("build justelesRCP")
    names = load_names()

    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "rcp").mkdir(parents=True)

    page_tpl = (SRC / "rcp.html").read_text(encoding="utf-8")
    index: list[dict[str, str]] = []
    skipped_empty = 0

    def records():
        """Yield (cis, raw) for non-empty RCPs; count empties as a side effect."""
        nonlocal skipped_empty
        with CSV_PATH.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t")
            next(reader, None)  # header: Code_CIS / RCP_html
            for row in reader:
                if len(row) < 2:
                    continue
                cis, raw = row[0].strip(), row[1]
                if not raw.strip():
                    skipped_empty += 1  # some CIS have no published RCP
                    continue
                yield cis, raw

    # Parsing + brotli are CPU-bound and independent per record -> fan out.
    workers = max(1, (os.cpu_count() or 2) - 1)
    print(f"  rendering with {workers} workers...")
    with Pool(workers, initializer=_init_worker, initargs=(names, page_tpl)) as pool:
        for i, entry in enumerate(
            pool.imap_unordered(render_record, records(), chunksize=8)
        ):
            if entry is not None:
                index.append(entry)
            if (i + 1) % 2000 == 0:
                print(f"  {i + 1} pages...")

    # Homepage + assets
    idx_json = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    (DIST / "search-index.json").write_text(idx_json, encoding="utf-8")
    for asset in ("index.html", "style.css", "search.js"):
        shutil.copy(SRC / asset, DIST / asset)
    for f in ("index.html", "style.css", "search.js", "search-index.json"):
        compress(DIST / f)

    print(f"done: {len(index)} RCP pages ({skipped_empty} empty CIS skipped) -> {DIST}")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    main()

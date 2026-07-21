# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.24",
# ]
# ///
"""Convert an EMA product-information PDF into a single clean HTML document.

Centrally-authorized (EMA "procédure centralisée") drugs have an empty ANSM RCP;
their real SmPC/notice text is published by the EMA as a French PDF
(``..._fr.pdf``, the exact href scrape-rcp.py already harvests, see build.py's
/eu/ stubs). This module turns that PDF into ONE self-contained HTML blob (text
reflowed, tables rebuilt as real ``<table>``, meaningful figures embedded as
base64) that build.py can render on the drug's ``/eu/`` page, so the reader gets
the actual text on-site instead of only a link out.

Design goals mirroring the ANSM overlay pipeline:

- **Output shape**: the body HTML is wrapped in ``<div id="textDocument">…</div>``,
  the SAME envelope the ANSM overlays use, so build.py reads/handles it uniformly.
- **Structure from the QRD template**: EMA SmPCs follow the same numbered
  "résumé des caractéristiques du produit" template as the ANSM RCP (``1.
  DÉNOMINATION``, ``4.1 Indications``, …). Headings are detected from that
  numbering + the PDF's bold flag and emitted as ``<h2 id="sec-N">`` / ``<h3>``
  so the /eu/ page can grow the same sidebar table of contents.
- **Real tables**: PyMuPDF's ``find_tables`` recovers tabular regions as cells;
  we emit semantic ``<table>`` and skip the raw text of those regions so it is
  not duplicated.
- **Figures only**: images are embedded (base64) but tiny repeated pictograms
  (warning triangles, the black-triangle additional-monitoring symbol) are
  dropped via a size floor; big figures are transcoded to web formats (JPEG for
  photos, PNG for line art) and downscaled, since the source may be JPEG2000
  (not browser-renderable) or huge.
- **Chrome stripped**: the per-page running page-number line is removed.

Pure conversion, no I/O and no network: ``convert(pdf_bytes)`` takes the raw PDF
bytes and returns a dict. The fetcher (scrape-ema.py) and the refresh service own
the download + caching; keeping this import-safe and side-effect-free lets both
reuse it. PyMuPDF (fitz) is AGPL-3.0, matching this project's licence.
"""

from __future__ import annotations

import base64
import html
import re

import fitz  # PyMuPDF


# A source image smaller than this on BOTH sides is chrome (pictogram/icon), not
# a figure worth showing; dropped. Real SmPC figures (charts, structures) are
# comfortably larger.
_IMG_MIN_SIDE = 200
# Cap embedded figure width so a 3000px source doesn't bloat the page; CSS scales
# it down anyway (max-width:100%). Height scales proportionally.
_IMG_MAX_WIDTH = 1100
# Above this source pixel area an rgb, alpha-free image is treated as a photo and
# stored as JPEG (far smaller than PNG); smaller / line-art images stay PNG.
_IMG_JPEG_AREA = 400_000

# Section-marker tokens that open a heading in the QRD template. A top-level
# marker ("1.", "10.", a lone "ANNEXE") -> h2; a sub-marker ("4.1", "6.2") -> h3.
_MARKER_TOP = re.compile(r"^(\d{1,2})\.$")
_MARKER_SUB = re.compile(r"^(\d{1,2})\.\d{1,2}$")
_MARKER_ALPHA = re.compile(r"^[A-E]\.$")  # Annexe II conditions: A. B. C. D.
_ANNEXE = re.compile(r"^ANNEXE\s+[IVX]+", re.I)
# The few structural annex-title lines that are top-level headings without a
# number marker. Deliberately a tight whitelist: a generic "all-caps bold line ->
# heading" rule wrongly promotes the mock-up box text in the labelling annex and
# the many uppercase run-ins, exploding the heading count.
_ANNEX_TITLE = re.compile(
    r"^(RÉSUMÉ DES CARACTÉRISTIQUES|ÉTIQUETAGE ET NOTICE|ÉTIQUETAGE|NOTICE)\b", re.I
)
# A heading that opens a new collapsible <details> group (macro structure of the
# doc). The labelling annex repeats its 1-18 block per pack and the notice recurs
# per presentation, so grouping by these dividers gives a handful of foldable
# blocks instead of one flat 300-heading wall. Matched against the full heading
# label, so "A. ÉTIQUETAGE"/"B. NOTICE" (Annexe III halves) group while
# "A. FABRICANT…" (an Annexe II section) does not.
_GROUP_BOUNDARY = re.compile(r"(ANNEXE\s+[IVX]+|RÉSUMÉ DES CARACTÉRISTIQUES|ÉTIQUETAGE|NOTICE)", re.I)
_SMPC_TITLE = re.compile(r"RÉSUMÉ DES CARACTÉRISTIQUES", re.I)  # the group shown open


def _pdf_date(raw: str | None) -> str:
    """PDF ``D:YYYYMMDDHHmmSS±ZZ'zz'`` date -> ``YYYY-MM-DD`` (or "" if unparsable)."""
    if not raw:
        return ""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def capture_date(doc: fitz.Document) -> str:
    """Our 'as of' date for an EMA doc: its ModDate (last revision), else the
    CreationDate. Both come from the PDF metadata (see pdfinfo)."""
    md = doc.metadata or {}
    return _pdf_date(md.get("modDate")) or _pdf_date(md.get("creationDate"))


def _img_data_uri(doc: fitz.Document, xref: int) -> tuple[str, int] | None:
    """Return ``(data_uri, display_width)`` for one image xref, or None if it is
    too small to be a real figure. Transcodes to a browser format and downscales.

    Transparency is PRESERVED: a figure drawn on a transparent background (e.g. a
    Kaplan-Meier curve) is emitted as a real RGBA PNG so the page's per-theme figure
    backing shows through (CSS --fig-img-bg is white in dark mode + the lightbox pads
    white). Dropping the soft mask instead baked those areas opaque black, unreadable on
    the dark theme. NOTE: this changes the converted overlay, so existing data/eu pages
    keep the old flattened image until re-converted (scrape-ema.py / the refresh EMA lane
    re-crawl, e.g. deploy.sh --rebuild)."""
    try:
        info = doc.extract_image(xref)
    except Exception:
        return None
    w, h = info.get("width", 0), info.get("height", 0)
    if w < _IMG_MIN_SIDE and h < _IMG_MIN_SIDE:
        return None  # pictogram / icon -> drop
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.colorspace and pix.colorspace.name not in ("DeviceRGB", "DeviceGray"):
            pix = fitz.Pixmap(fitz.csRGB, pix)  # normalise CMYK/other to browser RGB
        # Re-attach the soft mask (a separate grayscale xref) so transparent regions stay
        # transparent in the emitted PNG. Same-dimension single-channel mask only; on any
        # size mismatch / read error keep the opaque base (the previous behaviour).
        smask_xref = info.get("smask", 0)
        if smask_xref and pix.alpha == 0:
            try:
                pix = fitz.Pixmap(pix, fitz.Pixmap(doc, smask_xref))
            except Exception:
                pass
        # Halve until within the width cap (shrink(n) reduces each side by 2**n).
        n = 0
        while pix.width > _IMG_MAX_WIDTH * (2 ** n):
            n += 1
        if n:
            pix.shrink(n)
        photo = pix.alpha == 0 and pix.n >= 3 and (pix.width * pix.height) >= _IMG_JPEG_AREA
        if photo:
            data, mime = pix.tobytes("jpeg", jpg_quality=78), "image/jpeg"
        else:
            data, mime = pix.tobytes("png"), "image/png"
    except Exception:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", pix.width


def _heading(level: int, label: str, sid: str = "") -> str:
    """Section heading reusing the ANSM RCP's class hooks so the /eu/ page looks
    identical to a normal RCP page (style.css restyles ``.AmmAnnexeTitre*`` and
    keys the sidebar-ToC scroll offset off the ``id``). level 1 -> Titre1 (h2,
    a ToC target), level 2 -> Titre2 (h3)."""
    cls = "AmmAnnexeTitre1" if level == 1 else "AmmAnnexeTitre2"
    tag = "h2" if level == 1 else "h3"
    idattr = f' id="{sid}"' if sid else ""
    return f'<{tag} class="{cls}"{idattr}>{html.escape(label)}</{tag}>'


def _table_html(tab) -> str:
    """One detected table -> semantic <table>. First row is treated as header."""
    rows = tab.extract()
    if not rows:
        return ""
    out = ["<table>"]
    for i, row in enumerate(rows):
        cells = [(c or "").strip() for c in row]
        tag = "th" if i == 0 else "td"
        tds = "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in cells)
        out.append(f"<tr>{tds}</tr>")
    out.append("</table>")
    return "".join(out)


def _inside(inner, outer, tol: float = 2.0) -> bool:
    """True when rect ``inner`` lies (mostly) within rect ``outer``."""
    return (
        inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol
    )


def _line_text(line) -> tuple[str, bool]:
    """Joined text of a dict-line and whether it is (all) bold."""
    txt = "".join(s["text"] for s in line["spans"]).strip()
    bold = bool(line["spans"]) and all(s["flags"] & 16 for s in line["spans"] if s["text"].strip())
    return txt, bold


def convert(pdf_bytes: bytes) -> dict:
    """Convert an EMA product-information PDF to clean, structured HTML.

    Returns ``{"html": <div id=textDocument>…</div>, "date": "YYYY-MM-DD",
    "title": <pdf title>, "toc": [{"sid", "label", "subs": [(sid, label), …]}, …]}``.

    The body groups the doc's macro dividers (each ANNEXE, the SmPC, the
    labelling halves, each notice variant) into collapsible ``<details
    class="ema-annexe">`` blocks so the reader gets a handful of foldable
    sections instead of a flat wall (the labelling annex repeats its 1-18 block
    per pack; the notice recurs per presentation). The SmPC group opens by
    default. ``toc`` is two levels: the groups, with the SmPC group's numbered
    sections as children. Figures are de-duplicated by xref (the same chart is
    embedded once). ``html`` is "" for an empty/broken PDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    date = capture_date(doc)
    title = (doc.metadata or {}).get("title", "").strip()
    groups: list[dict] = []
    seen_xrefs: set[int] = set()
    sec_n = 0
    pending_marker = ""  # a bold section marker awaiting its title line

    def new_group(gtitle: str) -> dict:
        # Merge consecutive boundaries with nothing between them (e.g. the
        # "ANNEXE I" cover line immediately followed by "RÉSUMÉ DES
        # CARACTÉRISTIQUES DU PRODUIT") into one descriptive title.
        if groups and not groups[-1]["parts"]:
            g = groups[-1]
            g["title"] = f'{g["title"]} - {gtitle}'.strip(" -")
            g["open"] = g["open"] or bool(_SMPC_TITLE.search(gtitle))
            return g
        g = {
            "sid": f"grp-{len(groups) + 1}", "title": gtitle,
            "open": bool(_SMPC_TITLE.search(gtitle)), "parts": [], "subs": [],
        }
        groups.append(g)
        return g

    def add_html(frag: str):
        (groups or [new_group("Document")])[-1]["parts"].append(frag)

    def flush_para(buf: list[str]):
        if buf:
            add_html(f"<p>{html.escape(' '.join(buf))}</p>")
            buf.clear()

    for pno, page in enumerate(doc, 1):
        tables = page.find_tables().tables
        tbboxes = [t.bbox for t in tables]
        # Elements to place in reading order: (y0, kind, payload)
        elems: list[tuple[float, str, object]] = []
        for t in tables:
            elems.append((t.bbox[1], "table", t))
        for im in page.get_image_info(xrefs=True):
            xref = im.get("xref", 0)
            if xref:
                elems.append((im["bbox"][1], "image", xref))
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            if any(_inside(b["bbox"], tb) for tb in tbboxes):
                continue  # text belongs to a table, emitted as <table>
            elems.append((b["bbox"][1], "block", b))
        elems.sort(key=lambda e: e[0])

        para: list[str] = []
        for _, kind, payload in elems:
            if kind == "table":
                flush_para(para)
                pending_marker = ""
                add_html(_table_html(payload))
            elif kind == "image":
                xref = payload
                if xref in seen_xrefs:
                    continue  # same figure already embedded on an earlier page
                seen_xrefs.add(xref)
                got = _img_data_uri(doc, xref)
                if got:
                    flush_para(para)
                    add_html(f'<figure><img src="{got[0]}" alt=""></figure>')
            else:  # text block
                for line in payload["lines"]:
                    txt, bold = _line_text(line)
                    if not txt:
                        continue
                    # Strip the running page-number line (a lone integer at top).
                    if txt == str(pno) and line["bbox"][1] < 90:
                        continue
                    # A lone bold section marker: hold it, the next line is its title.
                    if bold and (_MARKER_TOP.match(txt) or _MARKER_SUB.match(txt) or _MARKER_ALPHA.match(txt)):
                        flush_para(para)
                        pending_marker = txt
                        continue
                    if pending_marker:
                        label = f"{pending_marker} {txt}"
                        is_sub = bool(_MARKER_SUB.match(pending_marker))
                        if _GROUP_BOUNDARY.search(label) and not is_sub:
                            flush_para(para)
                            new_group(label)
                        elif is_sub:
                            add_html(_heading(2, label))
                        else:
                            sec_n += 1
                            sid = f"sec-{sec_n}"
                            add_html(_heading(1, label, sid))
                            if groups and groups[-1]["open"]:
                                groups[-1]["subs"].append((sid, label))
                        pending_marker = ""
                        continue
                    # A bold ANNEXE divider or a whitelisted annex title opens a group.
                    if bold and (_ANNEXE.match(txt) or _ANNEX_TITLE.match(txt)):
                        flush_para(para)
                        new_group(txt)
                        continue
                    para.append(txt)
                flush_para(para)
        flush_para(para)

    body_parts, toc = [], []
    for g in groups:
        inner = "".join(g["parts"])
        if not inner.strip():
            continue
        label = g["title"] or "Document"
        openattr = " open" if g["open"] else ""
        body_parts.append(
            f'<details class="ema-annexe"{openattr}>'
            f'<summary id="{g["sid"]}">{html.escape(label)}</summary>{inner}</details>'
        )
        toc.append({"sid": g["sid"], "label": label, "subs": g["subs"]})
    body = "".join(body_parts)
    return {
        "html": f'<div id="textDocument">{body}</div>' if body else "",
        "date": date,
        "title": title,
        "toc": toc,
    }

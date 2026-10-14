"""The site as plain text, for an AI assistant a person handed /llms.txt to.

Served by refresh-service.py, the one container that already reads ``dist/rcp`` and
``dist/eu``, under ``/api/md/...`` and ``/api/llms.txt``; docker/Caddyfile maps the
public ``/llms.txt`` and ``/md/...`` onto them. Nothing here is written to disk: a
Markdown page is converted, on request, FROM THE SERVED HTML PAGE, so it cannot say
anything the page does not, and no new kind of file joins the br-only storage rules
(``write_served``, ``served_pages``, ``_prune_pages``, the Caddy routes). It also keeps
build.py untouched, whose every edit re-renders the whole site (``_code_fingerprint``).

Pure lxml plus build.py's own helpers (by import, never copied).

Written by Claude Code (Opus 5.5).
"""

from __future__ import annotations

import re
from datetime import date

from lxml import html as lxml_html

import build

# The page's chrome, around the RCP text: breadcrumb, title, CIS line, dates, the
# official link, the reference pills (twice), the table of contents, and the
# automatic "Médicaments liés" block, which is justelesRCP's and not the ANSM's.
_CHROME = (
    ".//nav | .//h1 | .//script | .//style"
    " | .//p[contains(@class,'cis') or contains(@class,'rcp-asof') or contains(@class,'rcp-source')]"
    " | .//section[contains(@class,'rcp-more')]"
    " | .//details[contains(@class,'toc') or contains(@class,'drug-xref')]"
)
_TITLE_CLASS = re.compile(r"AmmAnnexeTitre(\d?)")
_BULLET = re.compile(r"^[·•▪•\-–]\s*")
_HEADING_TAGS = {"h2": 2, "h3": 3, "h4": 4, "h5": 4, "h6": 4}
_BLOCK_TAGS = {"p", "li", "dt", "dd", "blockquote", "pre", *_HEADING_TAGS}

# What /api/md/ answers, kept as strict as build._PLAIN_RE since it maps a
# client-derived path onto the filesystem: a lane, then a slug, nothing else.
MD_PAGE_RE = re.compile(r"/api/md/(rcp|eu)/(\d{8}-[a-z0-9-]+)")
MD_LETTER_RE = re.compile(r"/api/md/browse/([a-z]|num)")


def md_url(path: str) -> str:
    """The public Markdown address of a site path (``/rcp/<slug>`` -> ``/md/rcp/<slug>``)."""
    return build._abs_url(f"/md{path}")


def _inline(el) -> str:
    """An element's text, with its links as Markdown links.

    A link to another drug page points at that page's Markdown, so an assistant
    following it stays in text; an in-page anchor keeps only its words."""
    parts = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str):
            text = _inline(child)
            href = child.get("href", "") if child.tag == "a" else ""
            if href.startswith(("/rcp/", "/eu/")):
                text = f"[{build._norm_ws(text)}]({md_url(href)})"
            elif href.startswith(("http://", "https://")):
                text = f"[{build._norm_ws(text)}]({href})"
            parts.append(f" {text} " if child.tag in ("br", "td", "th") else text)
        parts.append(child.tail or "")
    return "".join(parts)


def _block(el) -> str:
    """One paragraph, heading or list item as a Markdown line, or "" when empty."""
    text = build._norm_ws(_inline(el))
    if not text:
        return ""
    title = _TITLE_CLASS.search(el.get("class", ""))
    if title:
        # AmmAnnexeTitre1 is a numbered section (4.), 2 a subsection (4.1), 3 and
        # deeper the unnumbered headings inside one; the page's h1 is the drug.
        return "#" * min(int(title[1] or 1) + 1, 4) + " " + text
    if el.tag in _HEADING_TAGS:
        return "#" * _HEADING_TAGS[el.tag] + " " + text
    if el.tag == "li" or "ListePuces" in el.get("class", ""):
        return "- " + _BULLET.sub("", text)
    return text


def _cell(td) -> str:
    """A table cell's text, its paragraphs (one adverse effect each, often) kept apart."""
    paras = [build._norm_ws(_inline(p)) for p in td.xpath(".//p")]
    return "; ".join(p for p in paras if p) if paras else build._norm_ws(_inline(td))


def _table(tbl) -> list[str]:
    """Every row, its non-empty cells joined by ' | '.

    Not build._linearize_table, which pairs each cell with its column header but
    stops at 60 rows and gives up on a table whose first row is a title: right for
    an embedding chunk, wrong for a page read whole."""
    lines = []
    for tr in tbl.xpath(".//tr"):
        cells = [_cell(c) for c in tr.xpath("./th | ./td")]
        cells = [c for c in cells if c]
        if cells:
            lines.append("| " + " | ".join(cells) + " |")
    return lines


def _walk(el, out: list[str]) -> None:
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if child.tag == "table":
            out.append("\n".join(_table(child)))
        elif child.tag in _BLOCK_TAGS:
            out.append(_block(child))
        else:
            if child.text and child.text.strip():
                out.append(build._norm_ws(child.text))
            _walk(child, out)
            if child.tail and child.tail.strip():
                out.append(build._norm_ws(child.tail))


def page_markdown(page: str, path: str) -> str:
    """A served drug page (``/rcp/`` or ``/eu/``) as Markdown.

    Parameters
    ----------
    page
        The page's HTML, as ``build.read_served`` returns it.
    path
        Its site path (``/rcp/<slug>``), for the links back to it.
    """
    doc = lxml_html.fromstring(page)
    main = doc.xpath("//main")[0]
    title = build._norm_ws(" ".join(main.xpath(".//h1//text()")))
    cis = main.get("data-cis", "")
    dates = [build._norm_ws(s.text_content()) for s in main.xpath(".//p[contains(@class,'rcp-asof')]/span")]
    source = main.xpath(".//p[contains(@class,'rcp-source')]//a/@href")
    for node in main.xpath(_CHROME):
        node.drop_tree()
    body: list[str] = []
    _walk(main, body)
    head = [f"# {title}", "", f"CIS {cis}" + "".join(f" · {d}" for d in dates), "",
            f"Page for a person: {build._abs_url(path)}"]
    if source:
        head.append(f"Official source: {source[0]}")
    head += ["", "Official text of the summary of product characteristics (ANSM / EMA), reformatted "
             "by justelesRCP. Links to other drugs were added automatically. Not medical advice.", ""]
    return "\n".join(head) + "\n" + "\n\n".join(b for b in body if b) + "\n"


def served_markdown(url_path: str) -> str | None:
    """``/api/md/<lane>/<slug>`` as Markdown, or None when no such page is served."""
    m = MD_PAGE_RE.fullmatch(url_path)
    if not m:
        return None
    data = build.read_served(build.DIST / m[1] / f"{m[2]}.html")
    return None if data is None else page_markdown(data.decode("utf-8"), f"/{m[1]}/{m[2]}")


def drug_index(names: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    """Served drug pages, /rcp/ and /eu/, grouped by first letter as the A-Z pages
    group them: letter -> [(name, site path)].

    Read from the directories rather than from the build's index, so a page the
    refresh service has just written is listed and a pruned one is not. A drug that
    has left the BDPM list keeps its page (an archived one) but has no name in
    ``names``; its slug's words stand in, which is cheaper than parsing ~1.6k pages."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for lane in ("rcp", "eu"):
        for page in build.served_pages(build.DIST / lane):
            slug = page.name[: -len(".html")]
            name = names.get(slug[:8]) or slug[9:].replace("-", " ")
            groups.setdefault(build._first_letter(name), []).append((name, f"/{lane}/{slug}"))
    for entries in groups.values():
        entries.sort(key=lambda e: build._sort_key(e[0]))
    return groups


_index_cache: tuple[tuple | None, dict] = (None, {})


def cached_drug_index() -> dict[str, list[tuple[str, str]]]:
    """drug_index over build._NAMES, recomputed only when dist/rcp changes.

    A directory's mtime moves whenever a file in it is created, renamed or removed,
    which is every page write (write_served renames a temp file into place)."""
    global _index_cache
    stamp = tuple((build.DIST / lane).stat().st_mtime_ns for lane in ("rcp", "eu")
                  if (build.DIST / lane).is_dir())
    if _index_cache[0] != stamp:
        _index_cache = (stamp, drug_index(build._NAMES))
    return _index_cache[1]


def letters_markdown(groups: dict[str, list[tuple[str, str]]]) -> str:
    """``/md/browse``: one line per letter, with its count."""
    lines = ["# justelesRCP: drugs A-Z", "",
             "One list per first letter of the drug's name (accents folded; `num` for names "
             "starting with anything else).", ""]
    for label in build._LETTERS:
        if label in groups:
            lines.append(f"- {label}: {md_url('/browse/' + build._letter_key(label))} "
                         f"({len(groups[label])} drugs)")
    return "\n".join(lines) + "\n"


def letter_markdown(groups: dict[str, list[tuple[str, str]]], key: str) -> str | None:
    """``/md/browse/<key>``: every drug under one letter, linked to its Markdown page."""
    label = "#" if key == "num" else key.upper()
    if label not in groups:
        return None
    lines = [f"# justelesRCP: drugs under {label}", ""]
    lines += [f"- [{name}]({md_url(path)})" for name, path in groups[label]]
    return "\n".join(lines) + "\n"


def llms_txt(groups: dict[str, list[tuple[str, str]]]) -> str:
    """The guide an assistant reads first (https://llmstxt.org)."""
    count = sum(len(v) for v in groups.values())
    return f"""# justelesRCP

> The official French summaries of product characteristics (RCP, résumés des caractéristiques du produit) of {count} drugs, from the ANSM's public drug database (BDPM) and, for centrally authorised drugs, from the EMA, as fast, ad-free pages. Each page is the official text, reformatted; nothing is written by an AI.

Please do not crawl: fetch the pages a question needs.

## Find a drug

- {md_url('/browse')}: the letters, with how many drugs each holds.
- `{md_url('/browse/<letter>')}`: every drug whose name starts with that letter (`a` to `z`, `num` for the rest), with a link to its page. Names are the official ones, brand and generic, with strength and form, e.g. "DOLIPRANE 1000 mg, comprimé".

## Read a drug's RCP

- `{md_url('/rcp/<CIS>-<slug>')}`: one RCP as Markdown, in French, with the official section numbers (4.1 Indications, 4.2 Posologie, 4.3 Contre-indications, 4.4 Mises en garde, 4.5 Interactions, 4.6 Grossesse et allaitement, 4.8 Effets indésirables, 5. Propriétés pharmacologiques...). Tables are one row per line, cells separated by `|`.
- Its first lines give the CIS code, the date of the official text, the page a person can open, and the official source.
- `{md_url('/eu/<CIS>-<slug>')}`: the same for a centrally authorised drug whose text comes from the EMA.

## Citing

Give a person the page link (`{build._abs_url('/rcp/<CIS>-<slug>')}`, the Markdown address without `/md`), and say which section the answer comes from. The text is the official one at the date the page states; it is not medical advice and does not replace the prescriber's judgement or the latest official text.

## About

- Source: ANSM, Base de données publique des médicaments, reused under the Licence Ouverte / Etalab 2.0. Not affiliated with the ANSM.
- Code (AGPL-3.0): https://github.com/thiswillbeyourgithub/justelesRCP
- Sister site, psychiatry practice guidelines with semantic search: https://psychiatheque.olicorne.org/llms.txt
- Generated {date.today().isoformat()}.
"""


MARKDOWN = "text/markdown; charset=utf-8"
NOT_FOUND = ("# Not found\n\nNo page at this address. The drug list is at "
             f"{md_url('/browse')}, and how to use this site at {build._abs_url('/llms.txt')}.\n")


def route(path: str) -> tuple[int, str, str] | None:
    """Answer a GET under ``/api/md/`` or ``/api/llms.txt``: (status, body, content type).

    None when the path is neither, so the service keeps its other routes."""
    if path == "/api/llms.txt":
        return 200, llms_txt(cached_drug_index()), "text/plain; charset=utf-8"
    if not path.startswith("/api/md/"):
        return None
    if path in ("/api/md/browse", "/api/md/browse/"):
        return 200, letters_markdown(cached_drug_index()), MARKDOWN
    m = MD_LETTER_RE.fullmatch(path)
    body = letter_markdown(cached_drug_index(), m[1]) if m else served_markdown(path)
    return (200, body, MARKDOWN) if body is not None else (404, NOT_FOUND, MARKDOWN)

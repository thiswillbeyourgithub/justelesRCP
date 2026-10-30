# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "html5lib",
# ]
# ///
"""Check that every semantic-search chunk can be found on its rendered page.

A search hit is shown by highlighting the blocks its chunk was cut from: the chunk's
location (``b``/``r`` in the .vec.json, see build._BlockIndex) numbers the blocks of
its section, and rcp-semsearch.js's locateByBlocks() takes those blocks from the live
page and verifies that they hold the snippet before trusting them. This tool replays
that on the SERVED pages, parsed with html5lib (the HTML5 algorithm browsers use, so
a <p> auto-closed around a <table> numbers the same way here as in the reader's
browser), for chunks re-segmented from the current overlays (no vectors needed).

A chunk FAILS when it has no location, its section heading is missing from the page,
or the claimed blocks do not hold its snippet's words: the reader would land on the
section heading instead of the passage. Exits 1 on any failure, so deploy.sh can run
``--sample N`` as a pre-flight gate.

    uv run src/audit-locate.py --sample 200     # random pages
    uv run src/audit-locate.py --all            # the whole crawled catalog
    uv run src/audit-locate.py --only 60234100  # one drug, every failing chunk listed
"""
from __future__ import annotations

import random
import re
import sys
from collections import Counter
from pathlib import Path

import warnings

import click
import html5lib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

# Mirrors WORD_RE / VERIFY_WORDS in rcp-semsearch.js (keep in sync).
_WORD = re.compile(r"[^\W_]{4,}")
_VERIFY_WORDS = 8


def verify_words(snippet: str) -> list[str]:
    """The words a located run must contain: the first _VERIFY_WORDS words of 4+
    letters/digits, minus the snippet's last token (the snippet is the body cut at
    _SEC_SNIPPET_CHARS, so its last word may be truncated)."""
    head = snippet.rsplit(None, 1)[0] if len(snippet.split()) > 1 else snippet
    return [w.lower() for w in _WORD.findall(head)][:_VERIFY_WORDS]


def _text(el) -> str:
    """Lowercased text of one block; a row's cells are joined with a space (a <tr>'s
    own string glues "Dose" + "50 %" into "dose50 %")."""
    parts = el.xpath("./th | ./td") if el.tag == "tr" else [el]
    return " ".join(" ".join(p.xpath("string()").split()) for p in parts).lower()


def _run_text(blocks, first: int, last: int, row: bool) -> str | None:
    """Space-free lowercased text of blocks[first..last] (for a row: its <tr>s plus
    the table's header row, which the linearised line prefixes onto every cell)."""
    run = blocks[first:last + 1]
    if row:
        trs = []
        for el in run:
            tr = el if el.tag == "tr" else next(el.iterancestors("tr"), None)
            if tr is not None and tr not in trs:
                trs.append(tr)
        if not trs:
            return None
        table = next(trs[0].iterancestors("table"), None)
        run = table.xpath(".//tr")[:1] + trs if table is not None else trs
    return "".join(" ".join(_text(el) for el in run).split())


def check(doc, sec: str, snippet: str, b, row: bool) -> str:
    """"ok", "shifted" or the failure reason for one located chunk on one parsed page.
    Mirrors locateByBlocks() in rcp-semsearch.js: the claimed run first, then the same
    length shifted to the nearest offset whose text verifies (a browser-repaired
    <p><table> adds or drops blocks, moving every later index of the section)."""
    if not b:
        return "no-location"
    found = doc.xpath(f'//*[@id="{sec}"]')
    if not found:
        return "no-heading"
    blocks = build._BlockIndex(found[0]).blocks
    words = verify_words(snippet)
    for d in sorted(range(-b[0], len(blocks) - b[1]), key=abs):
        text = _run_text(blocks, b[0] + d, b[1] + d, row)
        # Substring of the space-free text, not word equality: the chunk's
        # text_content() glues adjacent cells ("lymphatiqueTrès") where the page has
        # two blocks.
        if text is not None and all(w in text for w in words):
            return "ok" if d == 0 else "shifted"
    return "text-mismatch"


def _parse(html: bytes | str):
    # html5lib warns about every attribute name lxml cannot store; noise here.
    warnings.filterwarnings("ignore", module="html5lib")
    return html5lib.parse(html, treebuilder="lxml", namespaceHTMLElements=False)


@click.command()
@click.option("--sample", type=int, default=None, help="Check N random crawled pages.")
@click.option("--all", "do_all", is_flag=True, help="Check every crawled page.")
@click.option("--only", multiple=True, help="Check only these CIS (repeatable).")
@click.option("--seed", type=int, default=None, help="Seed for --sample.")
@click.option("--show", type=int, default=30, help="How many failures to print.")
def main(sample, do_all, only, seed, show):
    pages = {sub: build.dist_pages_index(sub) for sub, _ in build.OVERLAY_LANES}
    items = [(cis, ov, sub) for cis, ov, sub in build.iter_overlay_paths()
             if cis in pages.get(sub, {}) and (not only or cis in only)]
    if not (sample or do_all or only):
        raise click.UsageError("pass --sample N, --all or --only CIS")
    if sample and not do_all:
        random.Random(seed).shuffle(items)
        items = items[:sample]
    total = Counter()
    bad_pages = 0
    failures = []
    for cis, ov, sub in items:
        html = build.read_served(pages[sub][cis])
        if html is None:
            continue
        doc = _parse(html)
        bad = 0
        for sec, snippet, _text_, loc in build.section_chunks(
                build._read_overlay(ov), cis, located=True):
            reason = check(doc, sec, snippet, loc and loc[:2], bool(loc and loc[2]))
            total[reason] += 1
            if reason not in ("ok", "shifted"):
                bad += 1
                failures.append((sub, cis, sec, reason, snippet[:90]))
        bad_pages += bool(bad)
    chunks = sum(total.values())
    print(f"{len(items)} page(s), {chunks} chunk(s): {dict(total)}")
    for f in failures[:show]:
        print("  FAIL", *f)
    if failures:
        print(f"{len(failures)} chunk(s) on {bad_pages} page(s) would land on their "
              "section heading instead of the passage")
        sys.exit(1)


if __name__ == "__main__":
    main()

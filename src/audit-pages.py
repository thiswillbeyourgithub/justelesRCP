# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "lxml>=5.0",
#   "brotli>=1.1",
# ]
# ///
"""Flag crawled pages whose parse looks broken: too few Sommaire entries, too little
text or too few semantic-search chunks.

A real RCP (ANSM) or SmPC (EMA) follows the QRD template: a dozen numbered sections,
thousands of characters, dozens of chunks. A page far below that almost always means
the parser lost the structure (e.g. 2026-11-05: 311 /eu/ pages whose SmPC title was
spelled without accents, "RESUME DES CARACTERISTIQUES", got a Sommaire made of the
annex names only, see build._eu_open_smpc). So a page under a threshold is a parsing
bug to fix, not a page to whitelist.

Works from the overlays (data/rcp, data/eu) through the SAME code the build and the
embed service run (build.clean_rcp / build._eu_prepare / build.section_chunks), so no
dist/ is needed.

    uv run src/audit-pages.py --all              # whole crawled catalog
    uv run src/audit-pages.py --sample 300       # random pages
    uv run src/audit-pages.py --only 61409314    # one drug, its metrics printed
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import click
from lxml import html as lxml_html

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

# Floors a sane page clears by a wide margin (the smallest real RCPs, e.g. a simple
# solvent, still have ~10 sections, a few thousand characters and ~10 chunks). Tune
# them only after checking that the pages under them are really fine.
MIN_TOC = 6
MIN_TEXT = 2000
MIN_CHUNKS = 5


def _toc_sections(toc, sub: str) -> int:
    """Sommaire entries pointing into the document: every nested entry, but on an /eu/
    page not the annex group rows themselves (a Sommaire listing only "ANNEXE I..IV"
    is exactly the bug this audit exists for)."""
    def count(entries):
        return sum(1 + count(kids) for _, _, kids in entries)
    if sub == "eu":
        return sum(count(kids) for _, _, kids in toc)
    return count(toc)


def page_metrics(cis: str, raw: str, sub: str) -> dict:
    """{toc, text, chunks} of one overlay, as the build renders and chunks it."""
    if sub == "eu":
        body, toc = build._eu_prepare(raw)
    else:
        _name, body, toc, _links = build.clean_rcp(raw, cis)
    text = " ".join(lxml_html.fragment_fromstring(body, create_parent="div")
                    .text_content().split()) if body else ""
    return {"toc": _toc_sections(toc, sub), "text": len(text),
            "chunks": len(build.section_chunks(raw, cis))}


def _work(item):
    cis, path, sub = item
    raw = build._read_overlay(path)
    if not raw.strip():
        return None
    try:
        return cis, sub, page_metrics(cis, raw, sub)
    except Exception as e:  # a page the build cannot parse at all is a failure too
        return cis, sub, {"toc": 0, "text": 0, "chunks": 0, "error": repr(e)}


def failures_of(m: dict, min_toc: int, min_text: int, min_chunks: int) -> list[str]:
    out = []
    if "error" in m:
        out.append(m["error"])
    if m["toc"] < min_toc:
        out.append(f"toc={m['toc']}")
    if m["text"] < min_text:
        out.append(f"text={m['text']}")
    if m["chunks"] < min_chunks:
        out.append(f"chunks={m['chunks']}")
    return out


@click.command()
@click.option("--sample", type=int, default=None, help="Check N random crawled pages.")
@click.option("--all", "do_all", is_flag=True, help="Check every crawled page.")
@click.option("--only", multiple=True, help="Check only these CIS (repeatable).")
@click.option("--seed", type=int, default=None, help="Seed for --sample.")
@click.option("--lane", type=click.Choice(["rcp", "eu"]), default=None,
              help="Check only the ANSM (rcp) or EMA (eu) pages.")
@click.option("--min-toc", type=int, default=MIN_TOC, show_default=True)
@click.option("--min-text", type=int, default=MIN_TEXT, show_default=True)
@click.option("--min-chunks", type=int, default=MIN_CHUNKS, show_default=True)
@click.option("--show", type=int, default=40, help="How many failures to print.")
def main(sample, do_all, only, seed, lane, min_toc, min_text, min_chunks, show):
    if not (sample or do_all or only):
        raise click.UsageError("pass --sample N, --all or --only CIS")
    items = [it for it in build.iter_overlay_paths()
             if (not only or it[0] in only) and (not lane or it[2] == lane)]
    if sample and not do_all:
        random.Random(seed).shuffle(items)
        items = items[:sample]
    with Pool() as pool:
        results = [r for r in pool.imap_unordered(_work, items, chunksize=16) if r]
    failures = []
    reasons = Counter()
    for cis, sub, m in sorted(results, key=lambda r: (r[1], r[0])):
        if only:
            print(sub, cis, m)
        bad = failures_of(m, min_toc, min_text, min_chunks)
        if bad:
            failures.append((sub, cis, " ".join(bad)))
            reasons.update(b.split("=")[0] for b in bad)
    print(f"{len(results)} page(s) checked, {len(failures)} under a threshold: {dict(reasons)}")
    for f in failures[:show]:
        print("  FAIL", *f)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

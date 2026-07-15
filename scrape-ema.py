# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "loguru",
#   "httpx",
#   "lxml",
#   "pymupdf>=1.24",
# ]
# ///
"""Fetch EMA product-information PDFs and convert them into ``/eu/`` overlays.

This is the EMA counterpart of ``scrape-rcp.py``. Centrally-authorized drugs have
an empty ANSM RCP; their real SmPC/notice text lives in a French PDF at the EMA.
``scrape-rcp.py`` already harvests the exact PDF URL per CIS into the ANSM scrape
manifest (the ``ema_pdf`` field, see build.py's /eu/ stubs). This script reads
those links, downloads each PDF politely, converts it to clean HTML with
``ema_pdf.convert``, and writes one overlay per drug at
``data/eu/<cis>.html[.gz]``. ``build.py``'s ``build_stubs`` renders that overlay
as the drug's full ``/eu/`` page (text on-site instead of only a link out).

Kept DRY with the ANSM scraper: manifest read/write (including the atomic-write
+ EROFS fallback needed on the read-only refresh container), the frequency queue
ordering and the ``is_due`` TTL, and the overlay writer are all imported from
``scrape-rcp.py`` (parameterised by path/dir), not re-implemented. The converter
is ``ema_pdf.py``. The refresh service imports ``process_one`` for its on-demand
+ crawler EMA lane, so keep it import-safe (the ``__main__`` guard below).

Storage mirrors the ANSM overlays: gzip by default (``RCP_OVERLAY_GZIP``), plain
with ``--no-gzip``; build.py reads either. The manifest is a SEPARATE file
(``data/.scrape-ema-manifest.json``) so the EMA TTL/crawl state never collides
with the ANSM one. Politeness knobs (env): ``EMA_SCRAPE_RATE_SECONDS`` (base gap,
EMA is strict about scraping so keep it slow), ``EMA_TTL_DAYS`` (re-fetch window;
SmPC PDFs change rarely, so this is long).

Internet Archive fallback: when the live EMA PDF URL fails to yield a PDF (network
error, 404, or an HTML error/maintenance page), ``process_one`` transparently
falls back to the Wayback Machine and converts the archived copy instead. The
reader never sees the archive URL (the overlay still bakes the real EMA URL as its
source button); we only stamp that the Internet Archive was used, on the overlay
(``data-ema-archive``, so the page shows a note) and in the manifest
(``via_archive``, so ``--retry-archived`` can re-try just those against the live
EMA once it recovers).
"""

from __future__ import annotations

import hashlib
import html
import importlib.util
import os
import random
import time
import urllib.parse
from pathlib import Path

import click
import httpx
from loguru import logger

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EU_OVERLAY_DIR = DATA / "eu"
EMA_MANIFEST_PATH = DATA / ".scrape-ema-manifest.json"
ANSM_MANIFEST_PATH = DATA / ".scrape-manifest.json"  # source of the ema_pdf links

# Internet Archive Wayback Machine, used ONLY as a fallback when the live EMA PDF
# URL fails to yield a PDF (network error, 404, or an HTML error/maintenance page).
# The availability endpoint returns the closest snapshot; we then fetch the raw
# archived bytes with the `id_` modifier so no Wayback HTML toolbar is injected.
# We NEVER expose the archive URL to the reader: the overlay still bakes the real
# EMA PDF URL as the source button; the archive is only how WE obtained the bytes.
WAYBACK_API = "https://archive.org/wayback/available?url="


def _load_module(filename: str, name: str):
    """Import a sibling ``foo-bar.py`` script by path (its ``-`` name isn't a
    valid import). Both are import-safe (``__main__``-guarded)."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scrape = _load_module("scrape-rcp.py", "scrape_rcp")  # manifest/queue/overlay helpers
ema = _load_module("ema_pdf.py", "ema_pdf")           # the PDF -> HTML converter

# EMA is strict about automated access, so default to a slow trickle and a long
# re-fetch window (an SmPC PDF changes rarely). Both overridable via env / flags.
DEFAULT_RATE = float(os.environ.get("EMA_SCRAPE_RATE_SECONDS", "30"))
DEFAULT_TTL_DAYS = int(os.environ.get("EMA_TTL_DAYS", "90"))
GZIP_DEFAULT = os.environ.get("RCP_OVERLAY_GZIP", "1").strip().lower() not in ("0", "false", "no", "")
USER_AGENT = os.environ.get(
    "EMA_SCRAPE_USER_AGENT",
    "justelesRCP-ema-scraper/1.0 (SmPC freshness bot; contact hedv10g9@mailer.me)",
)


def ema_links(ansm_manifest: dict) -> dict[str, str]:
    """CIS -> EMA product-information PDF URL, from the ANSM manifest's ema_pdf field."""
    out: dict[str, str] = {}
    for cis, entry in ansm_manifest.items():
        url = (entry or {}).get("ema_pdf") if isinstance(entry, dict) else None
        if url:
            out[cis] = url
    return out


def _overlay_html(conv: dict, src_url: str = "", fetched_date: str = "",
                  via_archive: bool = False) -> str:
    """The converter's HTML with the data-* facts baked onto the wrapper, so
    build.py's /eu/ renderer is self-contained (it reads all three off the overlay,
    no EMA-manifest read at build):

    * ``data-ema-date`` = the PDF's ModDate, i.e. when the EMA last revised the
      official text. build headlines this as the page's 'à jour au …' date (the
      analog of an ANSM RCP's revision date).
    * ``data-ema-fetched`` = when WE captured/converted it (this scrape's date).
      build bakes it as ``data-rcp-asof`` (the 'vérifiée par justelesRCP le …'
      line) AND, crucially, the on-demand refresh keys off it: it advances every
      time we re-fetch, so a refresh is detectable even when the ModDate is
      unchanged (the common case, since EMA PDFs change rarely).
    * ``data-ema-pdf`` = the exact source PDF URL, baked as the 'consulter le PDF
      officiel' button. This is ALWAYS the real EMA URL, even when the bytes came
      from the Internet Archive fallback (we never expose the archive URL).
    * ``data-ema-archive="1"`` = present ONLY when the bytes were recovered from the
      Internet Archive because the live EMA URL failed. build shows a note on the
      page; the manifest also records it (``via_archive``) so these can be re-tried
      later against the live EMA.
    """
    doc_html = conv.get("html") or ""
    if not doc_html:
        return ""
    date = conv.get("date") or ""
    fetched = f' data-ema-fetched="{html.escape(fetched_date, quote=True)}"' if fetched_date else ""
    src = f' data-ema-pdf="{html.escape(src_url, quote=True)}"' if src_url else ""
    archived = ' data-ema-archive="1"' if via_archive else ""
    return doc_html.replace(
        '<div id="textDocument">',
        f'<div id="textDocument" data-ema-date="{date}"{fetched}{src}{archived}>', 1,
    )


def _looks_like_pdf(data: bytes) -> bool:
    """A downloaded body is a real PDF only if it starts with the %PDF- signature.
    A 200 OK that is actually an HTML error/maintenance page is a failed download,
    and must fall through to the Internet Archive just like a 404 would."""
    return data[:5] == b"%PDF-"


def _fetch_pdf(client: httpx.Client, url: str) -> bytes:
    """GET one URL and return its bytes, raising unless it is a real PDF."""
    resp = client.get(url)
    resp.raise_for_status()
    if not _looks_like_pdf(resp.content):
        ct = resp.headers.get("content-type", "?")
        raise RuntimeError(f"not a PDF (content-type={ct}, {len(resp.content)}B)")
    return resp.content


def _wayback_pdf(client: httpx.Client, url: str) -> bytes:
    """Fetch ``url``'s PDF bytes from the Internet Archive Wayback Machine, the
    fallback when the live EMA URL fails. Queries the availability API for the
    closest snapshot, then fetches the RAW archived bytes (the ``id_`` modifier, so
    no Wayback HTML chrome is injected). Raises if there is no usable snapshot."""
    meta = client.get(WAYBACK_API + urllib.parse.quote(url, safe=""))
    meta.raise_for_status()
    snap = (meta.json().get("archived_snapshots") or {}).get("closest") or {}
    ts = snap.get("timestamp")
    if not snap.get("available") or not ts:
        raise RuntimeError("no Internet Archive snapshot")
    # https://web.archive.org/web/<timestamp>id_/<original-url> -> the raw file.
    resp = client.get(f"https://web.archive.org/web/{ts}id_/{url}")
    resp.raise_for_status()
    if not _looks_like_pdf(resp.content):
        raise RuntimeError("Internet Archive snapshot is not a PDF")
    return resp.content


def convert_and_write(cis: str, pdf_bytes: bytes, gzip_overlay: bool,
                      src_url: str = "", via_archive: bool = False) -> dict:
    """Convert one PDF's bytes and write its /eu/ overlay. Returns a manifest
    entry dict ({last_fetch, hash, status, date, bytes[, via_archive]}); status
    'empty' means the PDF yielded no usable HTML (no overlay written). When
    ``via_archive`` the bytes came from the Internet Archive, which is stamped on
    both the overlay (data-ema-archive) and the entry (via_archive)."""
    conv = ema.convert(pdf_bytes)
    now = scrape._now_iso()
    # Bake the fetch DATE (not the full timestamp) so a same-day re-fetch of an
    # unchanged PDF yields a byte-identical overlay (no needless rewrite/rsync).
    html_doc = _overlay_html(conv, src_url, fetched_date=now[:10], via_archive=via_archive)
    digest = hashlib.sha256(html_doc.encode("utf-8")).hexdigest()
    entry = {
        "last_fetch": now, "hash": digest,
        "date": conv.get("date", ""), "status": "ok" if html_doc else "empty",
    }
    if via_archive:
        entry["via_archive"] = True
    if not html_doc:
        return entry
    dest = scrape.write_overlay(cis, html_doc, gzip_overlay, overlay_dir=EU_OVERLAY_DIR)
    entry["bytes"] = dest.stat().st_size
    return entry


def process_one(client: httpx.Client, cis: str, url: str, gzip_overlay: bool) -> dict:
    """Fetch one EMA PDF and write its overlay. Tries the live EMA URL first; on
    ANY failure to obtain a PDF (network error, 404, or a non-PDF body) it falls
    back to the Internet Archive. Returns the manifest entry (with ``status='error'``
    and no overlay only when BOTH fail). Reused by the refresh service's on-demand +
    crawler EMA lane, so the fallback covers those too."""
    via_archive = False
    try:
        try:
            pdf = _fetch_pdf(client, url)
        except Exception as exc:  # live EMA failed: try the Internet Archive
            logger.warning("{} live EMA fetch failed ({}); trying Internet Archive",
                           cis, str(exc)[:120])
            pdf = _wayback_pdf(client, url)
            via_archive = True
        entry = convert_and_write(cis, pdf, gzip_overlay, src_url=url, via_archive=via_archive)
        entry["ema_pdf"] = url  # always the real EMA URL, never the archive one
        if via_archive:
            logger.info("{} recovered from Internet Archive (stamped for later retry)", cis)
        return entry
    except Exception as exc:  # both live + archive failed: record and move on
        return {"last_fetch": scrape._now_iso(), "status": "error",
                "error": str(exc)[:200], "ema_pdf": url}


def build_order(ttl_days: int, *, force: bool, frequency: Path | None,
                links: dict[str, str], ema_manifest: dict) -> list[str]:
    """CIS with an EMA PDF that are due, frequency-ordered (reusing the ANSM
    scraper's queue for ordering, then filtering by the EMA manifest's TTL)."""
    ordered = scrape.build_queue(0, force=True, restrict=set(links), frequency=frequency)
    if force:
        return ordered
    return [c for c in ordered if scrape.is_due(ema_manifest.get(c), ttl_days)]


@click.command()
@click.option("--limit", default=40, show_default=True, help="Max PDFs to fetch this run.")
@click.option("--all", "fetch_all", is_flag=True, help="Fetch every due CIS (ignore --limit).")
@click.option("--only", multiple=True, help="Fetch these explicit CIS (repeatable).")
@click.option("--file", "local_file", type=click.Path(exists=True, dir_okay=False),
              help="Offline: convert this local PDF instead of fetching (needs --cis).")
@click.option("--cis", "local_cis", default="", help="CIS for --file mode.")
@click.option("--src-url", "local_src", default="",
              help="Source PDF URL to bake into the overlay (--file mode).")
@click.option("--via-archive", "local_via_archive", is_flag=True,
              help="Mark the --file overlay as Internet-Archive-sourced (testing).")
@click.option("--ttl-days", default=DEFAULT_TTL_DAYS, show_default=True, help="Re-fetch window.")
@click.option("--rate", default=DEFAULT_RATE, show_default=True, help="Base seconds between fetches.")
@click.option("--no-gzip", "no_gzip", is_flag=True, help="Store plain .html overlays.")
@click.option("--force", is_flag=True, help="Ignore the TTL; refetch all candidates.")
@click.option("--retry-archived", is_flag=True,
              help="Only re-fetch CIS previously recovered from the Internet Archive "
                   "(manifest via_archive), to try the live EMA PDF again. Implies --force.")
@click.option("--frequency", type=click.Path(dir_okay=False), default=None,
              help="Frequency JSONL for ordering (defaults to data/drugs_frequency.jsonl).")
def main(limit, fetch_all, only, local_file, local_cis, local_src, local_via_archive,
         ttl_days, rate, no_gzip, force, retry_archived, frequency):
    """Refresh EMA /eu/ overlays from the live EMA site (see module docstring)."""
    gzip_overlay = GZIP_DEFAULT and not no_gzip
    freq = Path(frequency) if frequency else None

    # Offline conversion of a supplied PDF (testing / manual seed): no network.
    if local_file:
        if not local_cis:
            raise SystemExit("--file requires --cis")
        entry = convert_and_write(local_cis, Path(local_file).read_bytes(),
                                  gzip_overlay, src_url=local_src,
                                  via_archive=local_via_archive)
        if local_src:
            entry["ema_pdf"] = local_src
        man = scrape.load_manifest(EMA_MANIFEST_PATH)
        man[local_cis] = entry
        scrape.save_manifest(man, EMA_MANIFEST_PATH)
        logger.info("{} {} -> {} ({} bytes, date {})", local_cis, Path(local_file).name,
                    entry["status"], entry.get("bytes", 0), entry.get("date", "?"))
        return

    ansm = scrape.load_manifest(ANSM_MANIFEST_PATH)
    links = ema_links(ansm)
    if not links:
        logger.info("no ema_pdf links in {} yet; run scrape-rcp.py first", ANSM_MANIFEST_PATH.name)
        return
    ema_manifest = scrape.load_manifest(EMA_MANIFEST_PATH)

    if retry_archived:
        # Re-try only the CIS we had to recover from the Internet Archive, hoping the
        # live EMA PDF is reachable now. Frequency-ordered like a normal sweep, but
        # forced (TTL ignored) and restricted to the archive-stamped set.
        archived = {c for c in links if (ema_manifest.get(c) or {}).get("via_archive")}
        if not archived:
            logger.info("no Internet-Archive-sourced overlays to retry; done")
            return
        due = build_order(ttl_days, force=True, frequency=freq,
                          links={c: links[c] for c in archived}, ema_manifest=ema_manifest)
        targets = due if fetch_all else due[:limit]
        logger.info("retrying {} Internet-Archive-sourced CIS against the live EMA", len(targets))
    elif only:
        targets = [c for c in dict.fromkeys(only) if c in links]
        missing = [c for c in only if c not in links]
        if missing:
            logger.warning("{} requested CIS have no ema_pdf link (skipped): {}", len(missing), missing[:10])
    else:
        due = build_order(ttl_days, force=force, frequency=freq, links=links, ema_manifest=ema_manifest)
        targets = due if fetch_all else due[:limit]

    if not targets:
        logger.info("nothing due; done")
        return

    total = len(targets)
    logger.info("fetching {} EMA PDFs, overlay={}, base rate {}s (+jitter)",
                total, "gzip" if gzip_overlay else "plain", rate)
    n_ok = n_empty = n_err = 0
    start = time.monotonic()
    with httpx.Client(follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT}) as client:
        for i, cis in enumerate(targets, 1):
            entry = process_one(client, cis, links[cis], gzip_overlay)
            ema_manifest[cis] = entry
            status = entry["status"]
            if status == "ok":
                n_ok += 1
                result = f'{entry.get("bytes", 0)} bytes, date {entry.get("date", "?")}'
            elif status == "empty":
                n_empty += 1
                result = "no usable HTML (skipped)"
            else:
                n_err += 1
                result = f'ERROR {entry.get("error", "")}'
            logger.info("{} | {} -> {}", scrape._progress(i, total, start), cis, result)
            if i % 10 == 0:
                scrape.save_manifest(ema_manifest, EMA_MANIFEST_PATH)
            if i < total and rate > 0:
                time.sleep(rate + random.uniform(0.0, min(rate, 10.0)))

    scrape.save_manifest(ema_manifest, EMA_MANIFEST_PATH)
    logger.info("done: {} overlays, {} empty, {} errors in {}",
                n_ok, n_empty, n_err, scrape._fmt_dur(time.monotonic() - start))
    logger.info("now rebuild: uv run build.py")


if __name__ == "__main__":
    main()

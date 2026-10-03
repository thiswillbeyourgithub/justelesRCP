# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "loguru",
#   "httpx",
#   "lxml",
#   "brotli>=1.1",
#   "pymupdf>=1.24",
# ]
# ///
"""Unit tests for the EMA-JSON seeding helpers in scrape-ema.py.

Run: ``uv run test_ema_seed.py``. Covers the fragile PURE pieces of
``--seed-from-ema-json`` (no network, no data files; the full seed loop over real
CIS_bdpm data is exercised by a live ``--dry-run``):

1. ``_parse_ema_documents`` tolerates the dump's run-on records: records carrying
   a ``translations`` object are NOT comma-separated from the next one, so a plain
   ``json.loads`` raises. This is the exact shape the real EMA feed ships.
2. ``_ema_pi_index`` keeps only ``product-information`` docs, French PDF preferred.
3. ``_match_brand`` joins on whole-word boundaries only, so a shared leading word
   alone never mislinks a wrong drug's SmPC onto a whole authorization group.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "scrape_ema", Path(__file__).parent / "scrape-ema.py")
sema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sema)


# A miniature dump mixing a comma-separated record and a run-on one: the
# translations-bearing record 2 is followed by whitespace, NOT a comma, before
# record 3 -- exactly like the real feed, and exactly what breaks json.loads.
MINI = (
    '{\n"meta": {"total_records": 3},\n"data": [\n'
    '    {"id":"1","type":"overview","medicine_name":"Zerene",'
    '"document_url":"https://www.ema.europa.eu/en/zerene-overview_en.pdf"},\n'
    '    {"id":"2","type":"product-information","medicine_name":"Abilify",'
    '"document_url":"https://www.ema.europa.eu/en/abilify-maintena-epar-product-information_en.pdf",'
    '"translations":{"fr":"https://www.ema.europa.eu/fr/abilify-maintena-epar-product-information_fr.pdf",'
    '"de":"https://www.ema.europa.eu/de/abilify_de.pdf"}}    '
    '{"id":"3","type":"product-information","medicine_name":"Onlyen",'
    '"document_url":"https://www.ema.europa.eu/en/onlyen-epar-product-information_en.pdf"}\n]}'
)


def test_parse_tolerates_runon_records():
    import json
    # the raw feed shape genuinely defeats a plain parse (guard against a future
    # refactor that "simplifies" _parse_ema_documents back to json.loads):
    try:
        json.loads(MINI)
        assert False, "MINI should be malformed (run-on records); test is stale"
    except json.JSONDecodeError:
        pass
    recs = sema._parse_ema_documents(MINI)
    assert [r["id"] for r in recs] == ["1", "2", "3"], recs
    assert sema._parse_ema_documents("not json at all") == []
    print("ok  test_parse_tolerates_runon_records")


def test_pi_index_prefers_french_and_filters_type():
    idx = sema._ema_pi_index(sema._parse_ema_documents(MINI))
    # 'overview' (Zerene) excluded; only product-information kept.
    assert set(idx) == {"abilify", "onlyen"}, idx
    # French preferred when present, English document_url as the fallback.
    assert idx["abilify"].endswith("_fr.pdf"), idx["abilify"]
    assert idx["onlyen"].endswith("onlyen-epar-product-information_en.pdf"), idx["onlyen"]
    print("ok  test_pi_index_prefers_french_and_filters_type")


def test_match_brand_word_boundary():
    idx = {"abilify": "U1", "ozempic wegovy": "U2"}
    assert sema._match_brand("abilify", idx) == "U1"             # exact
    assert sema._match_brand("abilify maintena", idx) == "U1"    # ANSM brand longer, word-prefix
    assert sema._match_brand("ozempic wegovy 1 mg", idx) == "U2"
    assert sema._match_brand("ozempic", {"ozempic wegovy": "U2"}) == "U2"  # EMA name longer, word-prefix
    assert sema._match_brand("abilifyx", idx) is None            # no word boundary -> no mislink
    assert sema._match_brand("doliprane", idx) is None           # unrelated
    assert sema._match_brand("", idx) is None
    print("ok  test_match_brand_word_boundary")


def test_overlay_pdf_url_reads_baked_link():
    # --only must be able to re-fetch a CIS that borrows a sibling's link: it falls
    # back to the data-ema-pdf URL baked into the CIS's own converted overlay. Cover
    # both storage formats (plain + gzip) and the miss cases.
    import gzip as _gz
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as d:
        orig = sema.EU_OVERLAY_DIR
        sema.EU_OVERLAY_DIR = _P(d)
        try:
            url = "https://www.ema.europa.eu/fr/documents/product-information/x_fr.pdf"
            (_P(d) / "111.html").write_text(
                f'<div id="textDocument" data-ema-pdf="{url}">body</div>', encoding="utf-8")
            (_P(d) / "222.html.gz").write_bytes(_gz.compress(
                f'<div data-ema-pdf="{url}">g</div>'.encode("utf-8")))
            (_P(d) / "333.html").write_text("<div>no attr here</div>", encoding="utf-8")
            assert sema._overlay_pdf_url("111") == url          # plain
            assert sema._overlay_pdf_url("222") == url          # gzip
            assert sema._overlay_pdf_url("333") is None         # overlay without the attr
            assert sema._overlay_pdf_url("999") is None         # no overlay at all
        finally:
            sema.EU_OVERLAY_DIR = orig
    print("ok  test_overlay_pdf_url_reads_baked_link")


def test_convert_splits_paragraphs_bullets_and_drops_page_numbers():
    """ema_pdf.convert on a tiny synthetic 2-page PDF (regression, ABILIFY
    MAINTENA 4.2): the bottom page number must not leak in as a paragraph, a
    blank-line gap and each "•" item must open a new paragraph (they were all
    merged into one <p>), and a sentence running across the page break must stay
    one paragraph (it used to be cut at every page end)."""
    import fitz
    ema_pdf = sema.ema
    doc = fitz.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((71, 100), "Premier paragraphe, ligne un")
    p1.insert_text((71, 112.6), "et sa suite.")
    p1.insert_text((71, 138), "Deux schémas :")
    p1.insert_text((71, 163), "•")
    p1.insert_text((99, 163), "Une injection initiale ;")
    p1.insert_text((71, 176), "•")
    p1.insert_text((99, 176), "Deux injections initiales.")
    p1.insert_text((71, 201), "Les données")
    p1.insert_text((295, 805), "2")
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((71, 60), "disponibles sont insuffisantes.")
    p2.insert_text((295, 805), "3")
    out = ema_pdf.convert(doc.tobytes())["html"]
    paras = [m.strip() for m in out.split("<p>")[1:]]
    paras = [x[: x.index("</p>")] for x in paras]
    assert paras == [
        "Premier paragraphe, ligne un et sa suite.",
        "Deux schémas :",
        "· Une injection initiale ;",
        "· Deux injections initiales.",
        "Les données disponibles sont insuffisantes.",
    ], paras


def test_table_merged_title_row_spans_columns():
    """A merged title row (pymupdf gives ``None`` for the spanned cells) renders
    as one colspan header, and the next row is the column header, instead of a
    title cell next to a spurious empty header cell."""

    class _Tab:
        def extract(self):
            return [
                ["Oubli de doses", None],
                ["Moment de l’oubli", "Mesure à prendre"],
                ["> 5\nsemaines", ""],
            ]

    got = sema.ema._table_html(_Tab())
    assert got == (
        '<table><tr><th colspan="2">Oubli de doses</th></tr>'
        "<tr><th>Moment de l’oubli</th><th>Mesure à prendre</th></tr>"
        "<tr><td>&gt; 5 semaines</td><td></td></tr></table>"
    ), got


def test_error_entry_keeps_last_success_and_backs_off():
    """A failed fetch must not pass for a fresh capture, nor be retried at once.

    It used to overwrite the entry with ``{last_fetch: <now>, status: error}``:
    the page's "vérifiée le" date and the anti-hammer floor then read the FAILED
    attempt as a successful fetch, and ``is_due`` re-queued it on every rotation.
    """
    from datetime import datetime, timedelta, timezone
    scrape = sema.scrape
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    manifest = {"1": {"last_fetch": old, "hash": "h", "status": "ok",
                      "ema_pdf": "https://www.ema.europa.eu/x_fr.pdf"}}
    scrape.record_error(manifest, "1", "HTTP 503")
    e = manifest["1"]
    assert e["last_fetch"] == old and e["hash"] == "h", e
    assert e["ema_pdf"].endswith("x_fr.pdf") and e["status"] == "error", e
    assert "last_error" in e
    # Fresh failure: not due until the retry window passes, whatever the TTL.
    assert not scrape.is_due(e, 365)
    e["last_error"] = (datetime.now(timezone.utc)
                       - timedelta(seconds=scrape.ERROR_RETRY_SECONDS + 1)).isoformat()
    assert scrape.is_due(e, 365)
    # A never-succeeded CIS gets no fake last_fetch.
    scrape.store_entry(manifest, "2", {"status": "error", "error": "boom"})
    assert "last_fetch" not in manifest["2"] and not scrape.is_due(manifest["2"], 1)
    # Old-format error entry: its last_fetch was the failure time, so it is dropped.
    manifest["3"] = {"last_fetch": old, "status": "error", "error": "x"}
    assert scrape.is_due(manifest["3"], 365)
    scrape.record_error(manifest, "3", "y")
    assert "last_fetch" not in manifest["3"]
    # A success replaces the error entry wholesale.
    scrape.store_entry(manifest, "1", {"last_fetch": "2026-01-01T00:00:00+00:00", "status": "ok"})
    assert manifest["1"] == {"last_fetch": "2026-01-01T00:00:00+00:00", "status": "ok"}


def test_refresh_refuses_unknown_cis():
    """Any 8-digit number used to be accepted, queued and fetched from the ANSM
    (counting against the hourly budget). A CIS with no page now answers
    ``unknown`` before any queue/budget accounting; a page rendered after startup
    is still recognised from disk."""
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "refresh_service", Path(__file__).parent / "refresh-service.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    r = rs.Refresher.__new__(rs.Refresher)  # no I/O: only the sets _known reads
    r._page_cis, r._eu_cis = {"11111111"}, frozenset({"22222222"})
    saved = rs.build.DIST
    with tempfile.TemporaryDirectory() as d:
        rs.build.DIST = Path(d)
        try:
            assert r.request("99999999", "user") == {"status": "unknown"}
            assert r.status_of("99999999") == {"status": "unknown"}
            assert r._known("11111111") and r._known("22222222")
            (Path(d) / "rcp").mkdir()
            rs.build.write_served(Path(d) / "rcp" / "33333333-new.html", b"<html/>")
            assert r._known("33333333")
        finally:
            rs.build.DIST = saved


def test_ema_url_host_check_blocks_spoofs():
    # SSRF guard: the EMA links come from scraped third-party HTML and are fetched
    # server-side, so only https on the EMA host (parsed, not a substring) passes.
    ok = sema.scrape.bdpm.is_ema_pdf_url
    real = [  # shapes actually found in data/.scrape-manifest.json
        "https://www.ema.europa.eu/fr/documents/product-information/abilify-epar-product-information_fr.pdf",
        "https://www.ema.europa.eu/fr/documents/product-information/x-epar-product-information_fr.pdf-0",
        "https://www.ema.europa.eu/fr/media/55066",
        "https://ema.europa.eu/a.pdf",
    ]
    for u in real:
        assert ok(u), u
    for u in ("https://ema.europa.eu.evil.example/a.pdf",       # suffix spoof
              "https://evil.example/ema.europa.eu/a.pdf",       # path spoof
              "https://www.ema.europa.eu@169.254.169.254/a.pdf",  # userinfo spoof
              "https://notema.europa.eu/a.pdf",                 # no dot boundary
              "http://www.ema.europa.eu/a.pdf",                 # plain http
              "https://www.ema.europa.eu:8080/a.pdf",           # odd port
              "file:///etc/passwd", "", "https://[::1/"):
        assert not ok(u), u
    # The JSON-dump index drops an off-host PI link instead of seeding it.
    recs = [{"type": "product-information", "medicine_name": "Evil",
             "document_url": "https://ema.europa.eu.evil.example/evil.pdf"}]
    assert sema._ema_pi_index(recs) == {}
    print("ok  test_ema_url_host_check_blocks_spoofs")


def test_pdf_fetch_refuses_offhost_redirect():
    # A redirect hop to an internal host must be refused, not followed: the old
    # client followed redirects blindly after checking nothing at all.
    import httpx
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "www.ema.europa.eu":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8461/api/sem/stats"})
        return httpx.Response(200, content=b"%PDF-1.7 internal")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            sema._fetch_pdf(client, "https://www.ema.europa.eu/fr/a.pdf")
            assert False, "off-host redirect was followed"
        except RuntimeError as exc:
            assert "off-host" in str(exc), exc
        assert seen == ["https://www.ema.europa.eu/fr/a.pdf"], seen
        try:
            sema._fetch_pdf(client, "https://169.254.169.254/latest/meta-data")
            assert False, "off-host URL was fetched"
        except RuntimeError:
            pass
        assert len(seen) == 1, seen
    # An on-host redirect is followed and the PDF comes back.
    def ok_handler(req):
        if req.url.path == "/old.pdf":
            return httpx.Response(301, headers={"location": "/new.pdf"})
        return httpx.Response(200, content=b"%PDF-1.7 ok")
    with httpx.Client(transport=httpx.MockTransport(ok_handler)) as client:
        assert sema._fetch_pdf(client, "https://www.ema.europa.eu/old.pdf") == b"%PDF-1.7 ok"
    print("ok  test_pdf_fetch_refuses_offhost_redirect")


if __name__ == "__main__":
    test_parse_tolerates_runon_records()
    test_pi_index_prefers_french_and_filters_type()
    test_match_brand_word_boundary()
    test_overlay_pdf_url_reads_baked_link()
    test_convert_splits_paragraphs_bullets_and_drops_page_numbers()
    test_table_merged_title_row_spans_columns()
    test_error_entry_keeps_last_success_and_backs_off()
    test_refresh_refuses_unknown_cis()
    test_ema_url_host_check_blocks_spoofs()
    test_pdf_fetch_refuses_offhost_redirect()
    print("\nAll tests passed.")

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
    '"document_url":"https://x/en/zerene-overview_en.pdf"},\n'
    '    {"id":"2","type":"product-information","medicine_name":"Abilify",'
    '"document_url":"https://x/en/abilify-maintena-epar-product-information_en.pdf",'
    '"translations":{"fr":"https://x/fr/abilify-maintena-epar-product-information_fr.pdf",'
    '"de":"https://x/de/abilify_de.pdf"}}    '
    '{"id":"3","type":"product-information","medicine_name":"Onlyen",'
    '"document_url":"https://x/en/onlyen-epar-product-information_en.pdf"}\n]}'
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


if __name__ == "__main__":
    test_parse_tolerates_runon_records()
    test_pi_index_prefers_french_and_filters_type()
    test_match_brand_word_boundary()
    test_overlay_pdf_url_reads_baked_link()
    print("\nAll tests passed.")

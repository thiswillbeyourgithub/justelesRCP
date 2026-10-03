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


def test_pdf_fetch_and_convert_are_size_capped():
    # An untrusted PDF must not exhaust the refresh container: the download is
    # streamed and abandoned past the byte cap, and convert() refuses a PDF over the
    # page cap before parsing it (previously both were unbounded).
    import httpx
    import fitz
    big = b"%PDF-1.7 " + b"x" * 5000

    def handler(req):
        return httpx.Response(200, content=big)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            sema._get_on_hosts(client, "https://www.ema.europa.eu/a.pdf",
                               sema.scrape.bdpm.EMA_HOSTS, max_bytes=1000)
            assert False, "oversized body was accepted"
        except RuntimeError as exc:
            assert "over 1000 bytes" in str(exc), exc
        assert sema._fetch_pdf(client, "https://www.ema.europa.eu/a.pdf") == big
    with fitz.open() as doc:
        for _ in range(3):
            doc.new_page()
        pdf = doc.tobytes()
    saved = sema.ema.MAX_PAGES
    sema.ema.MAX_PAGES = 2
    try:
        try:
            sema.ema.convert(pdf)
            assert False, "over-page-cap PDF was converted"
        except ValueError as exc:
            assert "3 pages" in str(exc), exc
    finally:
        sema.ema.MAX_PAGES = saved
    assert sema.ema.convert(pdf)["html"] == ""  # under the cap: converts (empty doc)
    print("ok  test_pdf_fetch_and_convert_are_size_capped")


def test_persist_snapshots_inside_write_lock():
    """Two workers persisting the same manifest must not lose an update.

    ``_persist`` used to copy the manifest BEFORE taking ``_persist_lock``, so a
    worker holding an older copy could write it after another worker had written a
    newer one, dropping that entry. Here a worker blocks on the write lock while the
    manifest gains an entry: what it finally writes must include that entry."""
    import threading
    import time
    spec = importlib.util.spec_from_file_location(
        "refresh_service", Path(__file__).parent / "refresh-service.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    r = rs.Refresher.__new__(rs.Refresher)  # no I/O: only the two locks
    r._lock, r._persist_lock = threading.Lock(), threading.Lock()
    manifest, written = {"1": {"status": "ok"}}, []
    saved = rs.scrape.save_manifest
    rs.scrape.save_manifest = lambda snap, path=None: written.append(dict(snap))
    try:
        with r._persist_lock:  # another worker is mid-write
            t = threading.Thread(target=r._persist, args=(manifest,))
            t.start()
            time.sleep(0.2)  # let it reach the lock (it used to snapshot first)
            with r._lock:
                manifest["2"] = {"status": "ok"}
        t.join(5)
    finally:
        rs.scrape.save_manifest = saved
    assert written and set(written[-1]) == {"1", "2"}, written
    print("ok  test_persist_snapshots_inside_write_lock")


def test_crawl_order_is_rebuilt_after_a_rotation_and_a_live_harvest():
    """A lane's crawl order used to be built ONCE at startup, so a /eu/ group whose
    EMA link was harvested later (or a page added since) never joined the rotation.
    It is now rebuilt when the cursor wraps, and _harvest_ema_url marks the EMA
    lane stale (and wakes it) after caching a new link."""
    import threading
    spec = importlib.util.spec_from_file_location(
        "refresh_service", Path(__file__).parent / "refresh-service.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    r = rs.Refresher.__new__(rs.Refresher)  # no I/O: just the crawl state
    r._lock, r._pending = threading.Lock(), {}
    catalog = ["11111111", "22222222"]
    lane = rs._CrawlLane("eu", True, 180, 0, {}, lambda: list(catalog))
    r._build_crawl_order(lane)
    claimed = []
    for _ in range(2):
        claimed.append(r._claim_next_crawl(lane))
        r._pending.clear()
    assert claimed == catalog and lane.stale, (claimed, lane.stale)
    catalog.append("33333333")  # e.g. a newly seeded group's presentation
    r._build_crawl_order(lane)  # what _crawl_run does when lane.stale
    assert not lane.stale and lane.order == catalog
    seen = set()
    for _ in range(3):
        seen.add(r._claim_next_crawl(lane))
        r._pending.clear()
    assert "33333333" in seen, seen

    # A live harvest marks the EMA lane stale and wakes its worker.
    import tempfile
    r._eu_lane, r._manifest, r._ema_links = lane, {}, {}
    r._persist_manifest, r.gzip_overlay = (lambda: None), True
    lane.wake.clear()
    saved = rs.scrape.fetch_one, rs.scrape.extract_ema_pdf, rs.scrape.RCP_OVERLAY_DIR
    rs.scrape.fetch_one = lambda client, cis: ("<html/>", 200)
    rs.scrape.extract_ema_pdf = lambda page: "https://www.ema.europa.eu/x_fr.pdf"
    try:
        with tempfile.TemporaryDirectory() as d:
            rs.scrape.RCP_OVERLAY_DIR = Path(d)  # the harvest writes the empty overlay
            assert r._harvest_ema_url(None, "44444444")
    finally:
        rs.scrape.fetch_one, rs.scrape.extract_ema_pdf, rs.scrape.RCP_OVERLAY_DIR = saved
    assert lane.stale and lane.wake.is_set()
    print("ok  test_crawl_order_is_rebuilt_after_a_rotation_and_a_live_harvest")


def test_single_cis_scrape_records_empty_status_and_keeps_ema_link():
    """The refresh service's ANSM lane used to record every scrape as ``ok``, even a
    page with no RCP (a delisted drug), and to rebuild the entry from scratch, losing
    its ``ema_pdf``; its live EMA-link harvest wrote the link but no fetch result.
    All three paths now go through scrape.scrape_one, which records ``empty`` for an
    RCP-less page, harvests the EMA link off it, and keeps a previous link when the
    page links none."""
    import tempfile
    import threading
    spec = importlib.util.spec_from_file_location(
        "refresh_service", Path(__file__).parent / "refresh-service.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    sc = rs.scrape
    link = "https://www.ema.europa.eu/fr/documents/product-information/x-epar-product-information_fr.pdf"
    empty_page = f'<html><body><a href="{link}">RCP</a></body></html>'
    rcp_page = ('<html><body><div id="tabpanel-rcp-panel"><div id="contenu">'
                '<p class="AmmAnnexeTitre1">1. DENOMINATION</p><p>X</p></div></div>'
                '</body></html>')
    pages = {"11111111": empty_page, "22222222": rcp_page}
    saved = sc.fetch_one, sc.RCP_OVERLAY_DIR
    sc.fetch_one = lambda client, cis: (pages[cis], 200)
    try:
        with tempfile.TemporaryDirectory() as d:
            sc.RCP_OVERLAY_DIR = Path(d)
            # The shared core: empty page -> "empty" + harvested link, zero-byte overlay.
            rcp, entry = sc.scrape_one(None, "11111111", True)
            assert rcp == "" and entry["status"] == "empty", entry
            assert entry["ema_pdf"] == link, entry
            assert (Path(d) / "11111111.html.gz").stat().st_size == 0
            # A page with an RCP and no link keeps the previous entry's link.
            rcp, entry = sc.scrape_one(None, "22222222", True, {"ema_pdf": link})
            assert rcp and entry["status"] == "ok" and entry["ema_pdf"] == link, entry

            # The refresh service's ANSM lane records the same thing.
            r = rs.Refresher.__new__(rs.Refresher)  # no I/O: only what _process_ansm uses
            r._lock, r.gzip_overlay = threading.Lock(), True
            r._manifest = {"11111111": {"status": "ok", "ema_pdf": link}}
            r._persist_manifest = lambda: None
            outcomes = []
            r._record = lambda cis, source, result, msg: outcomes.append(result)
            r._process_ansm(None, "11111111", "user")
            assert r._manifest["11111111"]["status"] == "empty", r._manifest
            assert r._manifest["11111111"]["ema_pdf"] == link, r._manifest
            assert outcomes == ["empty"], outcomes
    finally:
        sc.fetch_one, sc.RCP_OVERLAY_DIR = saved
    print("ok  test_single_cis_scrape_records_empty_status_and_keeps_ema_link")


def test_summary_reports_crawler_refresh_count_not_the_lane_gauge():
    """stats() reuses the "crawl" key for the ANSM lane gauge, which used to shadow
    the crawler's refresh COUNTER, so /api/summary's refreshes.crawl (shown on
    /status as "Déclenchés par l'explorateur") was a dict instead of a number."""
    import queue
    import threading
    import time
    spec = importlib.util.spec_from_file_location(
        "refresh_service", Path(__file__).parent / "refresh-service.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    r = rs.Refresher.__new__(rs.Refresher)  # no I/O: only what stats() reads
    r._lock, r._pending, r._demand = threading.Lock(), {}, queue.Queue()
    r._started, r.demand_rate = time.monotonic(), 5
    r._stats = {"ok": 3, "empty": 0, "error": 0, "user": 1, "auto": 0, "crawl": 2,
                "fresh": 0, "busy": 0, "budget": 0}
    r._ansm_lane = rs._CrawlLane("rcp", True, 365, 120, {}, lambda: [])
    r._eu_lane = rs._CrawlLane("eu", True, 180, 300, {}, lambda: [])
    summary = r.public_summary()
    assert summary["refreshes"]["crawl"] == 2, summary["refreshes"]
    assert isinstance(summary["crawl"], dict)  # the lane gauge keeps its key
    print("ok  test_summary_reports_crawler_refresh_count_not_the_lane_gauge")


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
    test_pdf_fetch_and_convert_are_size_capped()
    test_persist_snapshots_inside_write_lock()
    test_crawl_order_is_rebuilt_after_a_rotation_and_a_live_harvest()
    test_single_cis_scrape_records_empty_status_and_keeps_ema_link()
    test_summary_reports_crawler_refresh_count_not_the_lane_gauge()
    print("\nAll tests passed.")

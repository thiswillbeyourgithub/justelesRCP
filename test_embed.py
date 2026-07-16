# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
#   "brotli>=1.1",
# ]
# ///
"""Unit tests for the pure semantic-search helpers in build.py.

Run: ``uv run test_embed.py`` (no ML dependency; the model-side embedding lives in
onnx_embed.py / embed-service.py). Covers the fiddly pure pieces the feature relies
on:

1. int8 quantise/dequantise round-trip stays within the ~1/127 error bound, so a
   query's cosine ranking over the baked section vectors is faithful.
2. section_chunks() stays aligned with clean_rcp()'s sec-N anchors, so a search hit
   scrolls to a heading that actually exists in the rendered page.
3. vec_payload/write_vec_json/read_vec_meta round-trip: the .vec.json the embed
   service and embed-rcp.py write is decodable by the browser, and its baked
   src_hash + model (the content-hash staleness key, no manifest) survives a
   write+read via either the plain file or its .gz sibling.
"""

import base64
import math
import os
import struct
import tempfile
from pathlib import Path

import build

SAMPLE = """<html><body><div id="textDocument">
  <p class="AmmAnnexeTitre1">1. DENOMINATION DU MEDICAMENT</p>
  <p>DOLIPRANE 1000 mg, comprime. Chaque comprime contient 1000 mg de paracetamol.</p>
  <p class="AmmAnnexeTitre1">4.6 Fertilite, grossesse et allaitement</p>
  <p>Le paracetamol peut etre utilise pendant la grossesse si besoin.</p>
  <p>En cas d'allaitement, le paracetamol passe en faible quantite dans le lait.</p>
  <p class="AmmAnnexeTitre1"></p>
  <p>Cette section a un titre vide et ne doit pas produire de chunk.</p>
</div></body></html>"""


def _l2_normalise(vec):
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def test_quantize_roundtrip():
    # A spread of normalised vectors, including boundary-ish values.
    vecs = [
        _l2_normalise([0.1 * (i - 5) for i in range(12)]),
        _l2_normalise([1.0] + [0.0] * 11),
        _l2_normalise([(-1) ** i for i in range(12)]),
    ]
    for vec in vecs:
        deq = build.dequantize_int8(build.quantize_int8(vec))
        assert len(deq) == len(vec)
        for original, restored in zip(vec, deq):
            err = abs(original - restored)
            assert err <= 1.0 / 127 + 1e-9, f"{original} vs {restored} (err {err})"
        # clamp guarantee
        for q in build.quantize_int8(vec):
            assert -127 <= q <= 127
    print("ok  test_quantize_roundtrip")


def test_section_chunks_align_with_toc():
    _, _, toc, _ = build.clean_rcp(SAMPLE)
    toc_ids = [sid for sid, _, _ in toc]
    # Two titled sections -> sec-0, sec-1; the empty-title heading gets no id.
    assert toc_ids == ["sec-0", "sec-1"], toc_ids

    chunks = build.section_chunks(SAMPLE)
    assert chunks, "expected chunks"
    chunk_ids = {sid for sid, _, _ in chunks}
    # Every chunk targets a real toc anchor; the empty-title section is excluded.
    assert chunk_ids <= set(toc_ids), chunk_ids
    assert chunk_ids == {"sec-0", "sec-1"}, chunk_ids

    joined = {sid: " ".join(c for s, _, c in chunks if s == sid) for sid in chunk_ids}
    assert "paracetamol" in joined["sec-0"].lower()
    assert "grossesse" in joined["sec-1"].lower()
    # chunk_text is title-prefixed (context for the encoder).
    assert joined["sec-1"].lower().startswith("4.6 fertilite")
    # snippets are non-empty and bounded.
    for _, snippet, _ in chunks:
        assert 0 < len(snippet) <= build._SEC_SNIPPET_CHARS
    print("ok  test_section_chunks_align_with_toc")


def test_empty_and_untitled():
    assert build.section_chunks("<div id='textDocument'></div>") == []
    assert build.section_chunks("not even html") == [] or True  # must not raise
    print("ok  test_empty_and_untitled")


def test_demojibake_restores_lost_apostrophes():
    # The frozen 2022 CSV stores Windows-1252 punctuation mis-decoded as Latin-1, so
    # the apostrophe byte 0x92 survives as the invisible C1 control U+0092 and the
    # apostrophe vanishes on the page ("d'élimination" -> "d élimination"). _parse_clean
    # must repair it on BOTH the rendered body and the search chunks.
    moji = (
        '<div id="textDocument">'
        '<p class="AmmAnnexeTitre1">4.2 Posologie</p>'
        # 0x92 apostrophes (mojibake), a 0x96 en-dash and a 0x85 ellipsis.
        "<p>Précautions délimination et modalités demploi "
        "– dose 110 mg</p>"
        "</div>"
    )
    _, cleaned, _, _ = build.clean_rcp(moji)
    # No C1 control survives, and the real punctuation is back.
    assert not any(0x80 <= ord(ch) <= 0x9F for ch in cleaned), "C1 control leaked"
    assert "d’élimination" in cleaned, cleaned      # 0x92 -> ’
    assert "d’emploi" in cleaned, cleaned
    assert "1–10 mg" in cleaned, cleaned                 # 0x96 -> –
    assert "mg…" in cleaned, cleaned                     # 0x85 -> …

    # The same repair reaches the semantic-search chunk text (parsed via _parse_clean).
    chunk_text = " ".join(c for _, _, c in build.section_chunks(moji))
    assert "d’élimination" in chunk_text, chunk_text
    assert not any(0x80 <= ord(ch) <= 0x9F for ch in chunk_text)

    # Already-clean UTF-8 (a fresh scrape) is untouched: proper ’ and straight ' pass through.
    clean = "<div id='textDocument'><p>l’efficacité et l'emploi</p></div>"
    assert build._demojibake(clean) == clean
    print("ok  test_demojibake_restores_lost_apostrophes")


# A page with two top-level sections, the second carrying two numbered subsections
# (AmmAnnexeTitre2) and one deeper heading (AmmAnnexeTitre3, excluded at depth 2).
SAMPLE_NESTED = """<html><body><div id="textDocument">
  <p class="AmmAnnexeTitre1">1. DENOMINATION DU MEDICAMENT</p>
  <p>DOLIPRANE 1000 mg.</p>
  <p class="AmmAnnexeTitre1">4. DONNEES CLINIQUES</p>
  <p class="AmmAnnexeTitre2">4.1 Indications therapeutiques</p>
  <p>Traitement de la douleur.</p>
  <p class="AmmAnnexeTitre2">4.2 Posologie et mode d'administration</p>
  <p class="AmmAnnexeTitre3">Posologie</p>
  <p>1 comprime par prise.</p>
</div></body></html>"""


def test_nested_toc_keeps_sec_namespace_stable():
    # The sidebar ToC now nests subsections, but the top-level sec-N ids (shared with
    # the semantic-search .vec.json anchors) MUST NOT shift: sub-headings live in a
    # separate sub-N namespace, so already-embedded vectors stay aligned.
    _, cleaned, toc, _ = build.clean_rcp(SAMPLE_NESTED)

    # Two roots, both top-level, numbered sec-0 / sec-1 exactly as depth-1 would.
    assert [sid for sid, _, _ in toc] == ["sec-0", "sec-1"], toc
    denom, clinique = toc
    assert denom[1].startswith("1. DENOMINATION") and denom[2] == []
    # The two AmmAnnexeTitre2 headings nest UNDER "4. DONNEES CLINIQUES" as sub-0/sub-1
    # (depth 2 stops before the AmmAnnexeTitre3 "Posologie" fragment).
    children = clinique[2]
    assert [sid for sid, _, _ in children] == ["sub-0", "sub-1"], children
    assert "4.1 Indications" in children[0][1]
    assert all(grandkids == [] for _, _, grandkids in children)

    # The rendered anchors exist for both namespaces, and the ToC HTML nests them.
    assert 'id="sec-0"' in cleaned and 'id="sec-1"' in cleaned
    assert 'id="sub-0"' in cleaned and 'id="sub-1"' in cleaned
    html = build._toc_html(toc)
    assert '<a href="#sub-0">' in html and "<ol><li><a" in html.replace("\n", "")

    # section_chunks is depth-1: it anchors chunks ONLY to top-level sec-N, never the
    # sub-N subsections, so the semantic-search contract is untouched by the ToC depth.
    chunk_ids = {sid for sid, _, _ in build.section_chunks(SAMPLE_NESTED)}
    assert chunk_ids <= {"sec-0", "sec-1"}, chunk_ids
    assert not any(sid.startswith("sub-") for sid in chunk_ids), chunk_ids
    print("ok  test_nested_toc_keeps_sec_namespace_stable")


SAMPLE_TABLE = """<html><body><div id="textDocument">
  <p class="AmmAnnexeTitre1">1. DENOMINATION DU MEDICAMENT</p>
  <p>DOLIPRANE 1000 mg, comprime.</p>
  <p class="AmmAnnexeTitre1">4.2 Posologie et mode d'administration</p>
  <p>La posologie depend du poids.</p>
  <table>
    <tr><th>Population</th><th>Dose</th><th>Frequence</th></tr>
    <tr><td>Adulte</td><td>500 mg</td><td>3 fois par jour</td></tr>
    <tr><td>Enfant</td><td>250 mg</td><td>2 fois par jour</td></tr>
  </table>
</div></body></html>"""


def test_table_rows_stay_intact():
    chunks = build.section_chunks(SAMPLE_TABLE)
    assert chunks, "expected chunks"
    texts = [c.lower() for _, _, c in chunks]

    # The posologie section id (sec-1: denomination is sec-0).
    poso = {sid for sid, _, c in chunks if "posologie" in c.lower()}
    assert poso == {"sec-1"}, poso

    # Each table row is ONE chunk carrying its column headers, so the population
    # and its full dose/frequency stay together (not flattened + split mid-row).
    adulte = [t for t in texts if "adulte" in t]
    assert len(adulte) == 1, adulte
    row = adulte[0]
    assert "500 mg" in row and "3 fois par jour" in row, row
    assert "population:" in row and "dose:" in row and "frequence:" in row, row
    # ...and rows do NOT bleed into each other.
    assert "enfant" not in row, row
    assert all(not ("adulte" in t and "enfant" in t) for t in texts)

    # Every table-row chunk is anchored to the real posologie section.
    for sid, _, c in chunks:
        if "adulte" in c.lower() or "enfant" in c.lower():
            assert sid == "sec-1", (sid, c)

    # The narrative paragraph of the same section is still its own chunk.
    assert any("la posologie depend du poids" in t for t in texts)
    print("ok  test_table_rows_stay_intact")


def test_layout_table_falls_back_flat():
    # A single-column / header-less "table" (layout markup) must NOT be linearised;
    # it falls back to flat text and must not raise.
    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">6. DONNEES</p>
      <table><tr><td>juste une cellule de mise en page</td></tr></table>
    </div>"""
    chunks = build.section_chunks(sample)
    assert any("mise en page" in c.lower() for _, _, c in chunks), chunks
    print("ok  test_layout_table_falls_back_flat")


# A converted /eu/ (EMA) overlay: sec-N anchors are already baked by ema_pdf (and
# QRD-numbered, so NON-sequential), nested inside a collapsible <details> group.
SAMPLE_EU = """<div id="textDocument">
  <details class="ema-annexe" open>
    <summary id="grp-0">ANNEXE I : RESUME DES CARACTERISTIQUES DU PRODUIT</summary>
    <h2 id="sec-4" class="AmmAnnexeTitre1">4. INFORMATIONS CLINIQUES</h2>
    <p>Indications therapeutiques dans le traitement de la schizophrenie.</p>
    <h2 id="sec-6" class="AmmAnnexeTitre1">6. INFORMATIONS PHARMACEUTIQUES</h2>
    <p>Liste des excipients: lactose monohydrate.</p>
  </details>
</div>"""


def test_eu_overlay_preserves_existing_ids():
    # section_chunks must KEEP the overlay's sec-4 / sec-6 ids (render_eu_page emits
    # exactly those anchors), not renumber them to sec-0 / sec-1 via _build_toc, else
    # a hit would scroll to a section that doesn't exist on the /eu/ page.
    chunks = build.section_chunks(SAMPLE_EU)
    assert chunks, "expected chunks"
    ids = {sid for sid, _, _ in chunks}
    assert ids == {"sec-4", "sec-6"}, ids
    by = {sid: c.lower() for sid, _, c in chunks}
    assert "schizophrenie" in by["sec-4"], by
    assert "excipients" in by["sec-6"], by
    print("ok  test_eu_overlay_preserves_existing_ids")


def _decode_q(b64):
    """Decode a base64 signed-int8 vector back to floats, exactly as the browser's
    decodeVec does (mirror of build.quantize_int8: v = q / 127)."""
    raw = base64.b64decode(b64)
    q = struct.unpack(f"{len(raw)}b", raw)
    return build.dequantize_int8(list(q))


def test_vec_payload_roundtrip():
    # vec_payload builds the exact wire format the browser consumes: {model, dim,
    # query_prefix, src_hash, chunks:[{sec, snippet, q}]}, q = base64(int8).
    chunks = [
        ("sec-0", "prise pendant les repas", "4.2 posologie: a prendre au cours des repas"),
        ("sec-3", "grossesse et allaitement", "4.6 grossesse: deconseille au 3e trimestre"),
    ]
    vecs = [
        _l2_normalise([0.1 * (i - 3) for i in range(8)]),
        _l2_normalise([(-1) ** i * 0.2 for i in range(8)]),
    ]
    payload = build.vec_payload(chunks, vecs, "Xenova/multilingual-e5-small",
                                "query: ", "abc123def456")
    assert payload["model"] == "Xenova/multilingual-e5-small"
    assert payload["query_prefix"] == "query: "
    assert payload["src_hash"] == "abc123def456"
    assert payload["dim"] == 8, payload["dim"]
    assert len(payload["chunks"]) == 2
    c0 = payload["chunks"][0]
    assert c0["sec"] == "sec-0" and c0["snippet"] == "prise pendant les repas"
    # q decodes back to ~ the original vector, within the int8 error bound.
    deq = _decode_q(c0["q"])
    assert len(deq) == 8
    for original, restored in zip(vecs[0], deq):
        assert abs(original - restored) <= 1.0 / 127 + 1e-9, (original, restored)
    print("ok  test_vec_payload_roundtrip")


def test_write_read_vec_meta_roundtrip():
    # write_vec_json writes the plain .vec.json + a .gz/.br sibling; read_vec_meta
    # recovers the baked {src_hash, model} (the content-hash staleness key, no
    # manifest) from EITHER the plain file or its .gz alone.
    payload = build.vec_payload(
        [("sec-0", "snip", "un texte")],
        [_l2_normalise([0.3] * 6)],
        "Xenova/multilingual-e5-small", "query: ", "feedface1234",
    )
    with tempfile.TemporaryDirectory() as d:
        vec = Path(d) / "12345678-doliprane.vec.json"
        build.write_vec_json(vec, payload)
        assert vec.exists()
        gz = vec.with_name(vec.name + ".gz")
        assert gz.exists(), "expected a .gz sibling from compress()"
        meta = build.read_vec_meta(vec)
        assert meta == {"src_hash": "feedface1234",
                        "model": "Xenova/multilingual-e5-small"}, meta
        # With the plain file gone (as Caddy might serve only the .gz), the meta must
        # still be readable from the compressed sibling.
        vec.unlink()
        meta_gz = build.read_vec_meta(vec)
        assert meta_gz["src_hash"] == "feedface1234", meta_gz
        # Missing entirely -> None (a not-yet-embedded page).
        assert build.read_vec_meta(Path(d) / "00000000-none.vec.json") is None
    print("ok  test_write_read_vec_meta_roundtrip")


def test_raw_hash_is_the_staleness_key():
    # The staleness gate (build.embed_page_to_vec) re-embeds iff raw_hash(raw) differs
    # from the .vec.json's baked src_hash: deterministic, short, and flips on ANY
    # change. So a no-op re-crawl that rewrote identical bytes hashes the same -> skip.
    a = build.raw_hash("<div id='textDocument'>bonjour</div>")
    assert a == build.raw_hash("<div id='textDocument'>bonjour</div>")  # deterministic
    assert a != build.raw_hash("<div id='textDocument'>bonjour!</div>")  # any edit flips
    assert len(a) == 16 and all(ch in "0123456789abcdef" for ch in a)
    print("ok  test_raw_hash_is_the_staleness_key")


def test_vec_is_fresh_gate():
    # The reconcile sweep's cheap enqueue gate. Its subtle case: a MODEL swap leaves an
    # already-embedded vec newer than its unchanged overlay, so the mtime-only gate must
    # NOT report it fresh on the check_model pass (else new-model query vectors would
    # rank against old-model passage vectors, silently wrong).
    payload_old = build.vec_payload([("sec-0", "snip", "texte")],
                                    [_l2_normalise([0.3] * 6)],
                                    "old-model", "query: ", "cafe1234cafe1234")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        overlay = d / "12345678.html"
        overlay.write_text("<div id='textDocument'>x</div>")
        vec = d / "12345678-doliprane.vec.json"

        # 1. No vec yet -> not fresh (embed it).
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        build.write_vec_json(vec, payload_old)
        # 2. Vec OLDER than overlay -> not fresh (a re-crawl bumped the overlay).
        os.utime(overlay, (overlay.stat().st_atime, vec.stat().st_mtime + 10))
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        # 3. Vec NEWER than overlay, mtime-only pass -> fresh (the common skip).
        os.utime(overlay, (overlay.stat().st_atime, vec.stat().st_mtime - 10))
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        # 4. Same, check_model pass, baked model MATCHES current -> still fresh.
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=True)

        # 5. Same, check_model pass, current model DIFFERS from the baked one -> stale,
        #    so the startup pass re-embeds it despite the newer mtime (the #1 fix).
        assert not build.vec_is_fresh(vec, overlay, "new-model", check_model=True)
    print("ok  test_vec_is_fresh_gate")


def test_overlay_iterators_agree():
    # iter_overlay_paths (path-level, shared by the embed sweep) and iter_overlay_raw
    # (content-level, shared by embed-rcp) must agree: raw yields exactly the non-empty
    # subset of the valid-CIS overlays paths enumerates, from the same lanes. This is
    # the single "which overlays exist" definition both the runtime and offline embed
    # paths build on.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        eu = d / "eu"; eu.mkdir()
        (rcp / "11111111.html").write_text("<div id='textDocument'>a</div>")
        (rcp / "22222222.html").write_text("")            # zero-byte body -> not in raw
        (rcp / "notacis.html").write_text("<div>x</div>")  # non-CIS name -> skipped
        (eu / "33333333.html").write_text("<div id='textDocument'>b</div>")
        saved = build.OVERLAY_LANES
        build.OVERLAY_LANES = (("rcp", rcp), ("eu", eu))
        try:
            paths = list(build.iter_overlay_paths())
            raws = list(build.iter_overlay_raw())
        finally:
            build.OVERLAY_LANES = saved
        # Every valid-CIS overlay file is enumerated (incl. the empty one); the non-CIS
        # name is skipped.
        assert {c for c, _, _ in paths} == {"11111111", "22222222", "33333333"}
        # raw keeps only the NON-empty overlays, tagged with the right subdir.
        assert {(c, s) for c, _, s in raws} == {("11111111", "rcp"), ("33333333", "eu")}
        # ...and raw is a strict subset of paths (never a page paths didn't list).
        assert {c for c, _, _ in raws} <= {c for c, _, _ in paths}
    print("ok  test_overlay_iterators_agree")


if __name__ == "__main__":
    test_quantize_roundtrip()
    test_section_chunks_align_with_toc()
    test_empty_and_untitled()
    test_demojibake_restores_lost_apostrophes()
    test_nested_toc_keeps_sec_namespace_stable()
    test_table_rows_stay_intact()
    test_layout_table_falls_back_flat()
    test_eu_overlay_preserves_existing_ids()
    test_vec_payload_roundtrip()
    test_write_read_vec_meta_roundtrip()
    test_raw_hash_is_the_staleness_key()
    test_vec_is_fresh_gate()
    test_overlay_iterators_agree()
    print("\nAll tests passed.")

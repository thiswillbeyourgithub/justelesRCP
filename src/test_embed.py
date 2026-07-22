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
import json
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
    # The heading PATH is now prefixed onto chunk_text (v6): each chunk starts with its
    # section title, then the body. So the heading text IS embedded (as context).
    assert joined["sec-1"].lower().startswith("4.6 fertilite")
    assert "fertilite" in joined["sec-1"].lower()
    assert "le paracetamol" in joined["sec-1"].lower()  # body follows the path
    # ...but the SNIPPET stays body-only, so the client's locate() still matches it
    # against the DOM paragraph (the heading text is never in a rendered <p>).
    for sec_id, snippet, chunk_text in chunks:
        assert 0 < len(snippet) <= build._SEC_SNIPPET_CHARS
        assert "fertilite" not in snippet.lower()
        assert "denomination" not in snippet.lower()
        # chunk_text = "<path>\n<body>"; the snippet is exactly the body's head.
        assert chunk_text.split("\n", 1)[-1].startswith(snippet)
    print("ok  test_section_chunks_align_with_toc")


def test_heading_only_section_yields_no_chunk():
    # A section that is a heading with no body must not emit a title-only chunk
    # (the heading is navigation, not content). Only the two bodied sections survive.
    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">5.1 Proprietes pharmacodynamiques</p>
      <p class="AmmAnnexeTitre1">5.2 Proprietes pharmacocinetiques</p>
      <p>Le medicament est absorbe rapidement apres administration orale.</p>
    </div>"""
    chunks = build.section_chunks(sample)
    ids = {sid for sid, _, _ in chunks}
    # sec-0 (heading only) drops out; sec-1 (has a body) stays.
    assert ids == {"sec-1"}, ids
    text = " ".join(c for _, _, c in chunks).lower()
    # 5.1 has no body -> no chunk -> its heading is never even a context prefix.
    assert "pharmacodynamiques" not in text, text
    # 5.2 DOES have a body, so its heading is now the prefix of that chunk (v6), and the
    # body follows it.
    assert "pharmacocinetiques" in text, text
    assert "absorbe rapidement" in text, text
    print("ok  test_heading_only_section_yields_no_chunk")


def test_heading_path_context_prefix():
    # v6: each chunk_text is prefixed with its section-heading PATH (top heading, then
    # the numbered subheadings it sits under), so a passage carries its topic even when
    # the sentence never names it. The subheading text is the PREFIX, not body, and the
    # anchor stays the TOP-level sec-N (a subheading never shifts the anchor).
    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">4. DONNEES CLINIQUES</p>
      <p class="AmmAnnexeTitre2">4.2 Posologie et mode d'administration</p>
      <p>La dose habituelle est de cinq cents milligrammes deux fois par jour chez l'adulte.</p>
      <p class="AmmAnnexeTitre2">4.6 Grossesse et allaitement</p>
      <p>Ce medicament est deconseille pendant toute la duree de la grossesse envisagee.</p>
    </div>"""
    chunks = build.section_chunks(sample)
    # Both bodies live under the ONE top-level section, so both anchor to sec-0.
    assert {sid for sid, _, _ in chunks} == {"sec-0"}, chunks
    by_body = {snip[:12]: text for _, snip, text in chunks}
    poso = next(t for t in by_body.values() if "cinq cents" in t)
    gross = next(t for t in by_body.values() if "deconseille" in t)
    # The path is "top > subheading", deepest last, ahead of the body (after a newline).
    assert poso.startswith("4. DONNEES CLINIQUES > 4.2 Posologie et mode d'administration\n"), poso
    assert gross.startswith("4. DONNEES CLINIQUES > 4.6 Grossesse et allaitement\n"), gross
    # The snippet is body-only (no path), so the client's locate() still matches the DOM.
    for _, snippet, _ in chunks:
        assert not snippet.startswith("4."), snippet
    print("ok  test_heading_path_context_prefix")


def test_merge_small_chunks():
    # A tiny tail / stray fragment is folded into a neighbour, never emitted alone, but a
    # lone short chunk (a genuinely short section) is kept; large chunks are left intact.
    assert build._merge_small(["x" * 400, "y" * 20], 160, 1000) == ["x" * 400 + " " + "y" * 20]
    assert build._merge_small(["short"], 160, 1000) == ["short"]  # lone small kept
    big = ["a" * 400, "b" * 400]
    assert build._merge_small(big, 160, 1000) == big  # both big -> untouched
    # The ceiling bounds a merge: a small piece that would overflow is NOT merged.
    assert build._merge_small(["a" * 900, "b" * 150], 160, 1000) == ["a" * 900, "b" * 150]
    # Consecutive small pieces coalesce up to the ceiling.
    assert build._merge_small(["a" * 50, "b" * 50, "c" * 50], 160, 1000) == \
        ["a" * 50 + " " + "b" * 50 + " " + "c" * 50]
    print("ok  test_merge_small_chunks")


def test_dsfr_backtotop_chrome_stripped():
    # Fresh ANSM scrapes carry DSFR back-to-top links + tooltip spans whose visible
    # text is "Redirection vers le haut de page". They must not pollute the chunks.
    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">4.1 Indications therapeutiques</p>
      <a class="fr-link lien-retour-hautdepage" aria-label="Redirection vers le haut de page" href="#top"></a>
      <span class="fr-tooltip fr-placement" role="tooltip">Redirection vers le haut de page</span>
      <p>Traitement symptomatique des douleurs d'intensite legere a moderee.</p>
    </div>"""
    text = " ".join(c for _, _, c in build.section_chunks(sample)).lower()
    assert "redirection vers le haut" not in text, text
    assert "traitement symptomatique" in text, text
    print("ok  test_dsfr_backtotop_chrome_stripped")


def test_filler_paragraphs_dropped():
    # RCP boilerplate paragraphs ("Sans objet.", a one-word "Oui", a 2-word fragment)
    # carry no searchable meaning and must not dilute the section vector; a real body
    # paragraph in the same section survives. Table rows keep their own path.
    assert build._is_filler_paragraph("Sans objet")            # exact phrase
    assert build._is_filler_paragraph("Sans objet.")           # with trailing period
    assert build._is_filler_paragraph("Oui")                   # 1 word / 3 chars
    assert build._is_filler_paragraph("Voie orale")            # 2 words
    assert build._is_filler_paragraph("   ")                   # empty after norm
    # QRD editorial placeholder, in its many accent/bracket/case variants.
    assert build._is_filler_paragraph("[à compléter ultérieurement par le titulaire]")
    assert build._is_filler_paragraph("[A compléter ultérieurement par le titulaire]")
    assert build._is_filler_paragraph("Fax, e-Mail : à compléter ultérieurement par le titulaire]")
    assert not build._is_filler_paragraph("Traitement de la douleur legere.")

    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">4.3 Contre-indications</p>
      <p>Sans objet.</p>
      <p class="AmmAnnexeTitre1">4.4 Mises en garde</p>
      <p>Ne pas depasser la dose recommandee chez les patients ages.</p>
      <p class="AmmAnnexeTitre1">4.9 Surdosage</p>
      <p>[&#224; compl&#233;ter ult&#233;rieurement par le titulaire]</p>
    </div>"""
    chunks = build.section_chunks(sample)
    ids = {sid for sid, _, _ in chunks}
    # "Sans objet." empties 4.3 and the placeholder empties 4.9 -> no chunk for either;
    # only 4.4 (real body) survives.
    assert ids == {"sec-1"}, ids
    joined = " ".join(c for _, _, c in chunks).lower()
    assert "sans objet" not in joined, joined
    assert "compléter" not in joined and "completer" not in joined, joined
    assert "ne pas depasser" in joined, joined
    print("ok  test_filler_paragraphs_dropped")


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


def test_iter_overlay_raw_honors_caller_order():
    # embed-rcp.py shuffles iter_overlay_paths() and feeds it back so tqdm's ETA is
    # representative; iter_overlay_raw(paths) must yield in the caller's order (skipping
    # only empty overlays), not re-enumerate its own.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        eu = d / "eu"; eu.mkdir()
        (rcp / "11111111.html").write_text("<div id='textDocument'>a</div>")
        (rcp / "22222222.html").write_text("")            # empty -> dropped, order intact
        (eu / "33333333.html").write_text("<div id='textDocument'>b</div>")
        saved = build.OVERLAY_LANES
        build.OVERLAY_LANES = (("rcp", rcp), ("eu", eu))
        try:
            paths = list(build.iter_overlay_paths())
            ordered = sorted(paths, key=lambda t: t[0], reverse=True)  # 33.., 22.., 11..
            got = [c for c, _, _ in build.iter_overlay_raw(ordered)]
        finally:
            build.OVERLAY_LANES = saved
        # The empty 22222222 is dropped; the two non-empty ones keep the caller's order.
        assert got == ["33333333", "11111111"]
    print("ok  test_iter_overlay_raw_honors_caller_order")


def test_sentence_chunks_group_and_fallbacks():
    # Prose is grouped on whole sentences up to the limit; a chunk ends on a sentence
    # boundary, never mid-sentence (the coherence win over the old word-windows).
    para = ("Phrase une. Phrase deux. Phrase trois un peu plus longue mais qui "
            "reste tout a fait raisonnable pour ce test.")
    out = build._sentence_chunks(para, 40)
    assert out and all(len(c) <= 40 for c in out), out
    # Nothing is dropped: rejoining the chunks reproduces the normalised text.
    assert " ".join(out) == build._norm_ws(para)
    # A single sentence longer than the limit falls back to word-windows (still <= limit).
    long_sentence = "mot " * 50  # 200 chars, no sentence punctuation -> one "sentence"
    out2 = build._sentence_chunks(long_sentence, 40)
    assert out2 and all(len(c) <= 40 for c in out2), out2
    # Last resort: a single unbroken token longer than the limit is cut between letters.
    giant = "x" * 130
    out3 = build._sentence_chunks(giant, 40)
    assert out3 and all(len(c) <= 40 for c in out3), out3
    assert "".join(out3) == giant, out3  # letters preserved, just re-windowed
    print("ok  test_sentence_chunks_group_and_fallbacks")


def test_long_section_tail_survives_raised_cap():
    # The bug: the old 160-chunk-per-page cap silently DROPPED the tail of very long
    # drugs, so a passage late in the RCP was never embedded and thus unfindable (the
    # reported real case: quetiapine LP, a fatty-meal pharmacokinetics passage in a doc
    # whose section 4 alone spent all 160 chunks). Build one huge section whose
    # distinctive passage sits well past where 160 chunks would have stopped, and assert
    # it is still emitted (cap raised to a pure failsafe) and kept intact in one chunk.
    # Distinct sentences (numbered) so within-page dedup does NOT collapse them: this
    # test isolates the raised cap, not the dedup path (covered separately below).
    filler = " ".join(
        f"Phrase numero {i} decrivant un aspect pharmacologique precis du produit teste."
        for i in range(4000)
    )
    target = ("Un repas riche en graisses induit une augmentation significative de la "
              "Cmax et de l'ASC de la substance a liberation prolongee.")
    body = filler + " " + target  # ~320k chars -> far more than 160 * _SEC_CHUNK_CHARS
    sample = ('<div id="textDocument">'
              '<p class="AmmAnnexeTitre1">5.2 Proprietes pharmacocinetiques</p>'
              f"<p>{body}</p></div>")
    chunks = build.section_chunks(sample)
    # Old behaviour capped at exactly 160; the tail now survives.
    assert len(chunks) > 160, len(chunks)
    assert len(chunks) <= build._SEC_MAX_CHUNKS, len(chunks)
    joined = " ".join(c for _, _, c in chunks)
    assert "repas riche en graisses" in joined, "tail passage was dropped"
    # The distinctive passage is intact within a SINGLE chunk (sentence grouping did not
    # cut it across two vectors, which would dilute both).
    assert any("repas riche en graisses induit une augmentation" in c
               for _, _, c in chunks), "passage was split across chunks"
    print("ok  test_long_section_tail_survives_raised_cap")


def test_duplicate_chunks_within_page_dropped():
    # An EMA notice repeated verbatim per presentation (or ANSM boilerplate) must embed
    # the identical text ONCE, not once per copy: avoids wasted encodes and duplicate
    # search results. Two top-level sections carry the SAME body; only one chunk survives.
    body = ("Ce medicament contient une substance active bien connue et son profil de "
            "tolerance a ete etabli au cours des essais cliniques menes chez l'adulte.")
    sample = ('<div id="textDocument">'
              '<p class="AmmAnnexeTitre1">1. Denomination</p>'
              f"<p>{body}</p>"
              '<p class="AmmAnnexeTitre1">2. Composition</p>'
              f"<p>{body}</p>"
              '<p class="AmmAnnexeTitre1">3. Forme</p>'
              "<p>Une forme pharmaceutique distincte et clairement differente des autres."
              "</p></div>")
    chunks = build.section_chunks(sample)
    texts = [c for _, _, c in chunks]
    assert len(texts) == len(set(texts)), texts  # no duplicate chunk_text on the page
    # Dedup is on the BODY, so the repeated prose is embedded ONCE (chunk_text now carries
    # a heading-path prefix, so match the body inside it, not a startswith).
    assert sum(1 for t in texts if "Ce medicament contient" in t) == 1, texts
    print("ok  test_duplicate_chunks_within_page_dropped")


def test_sitemap_lists_indexable_pages():
    # write_sitemap must list home + /a-propos + browse + every RCP + the FULL /eu/
    # pages, exclude bare /eu/ stubs (they are noindex), and stamp <lastmod> only when
    # a page's asof is known. It reads full/asof from the manifest, not the index rows.
    index = [
        {"cis": "11111111", "name": "AMOXICILLINE", "slug": "11111111-amoxicilline"},
        {"cis": "22222222", "name": "300", "slug": "22222222-300"},  # no alpha -> '#'
    ]
    stub_index = [
        {"cis": "33333333", "name": "ABILIFY", "slug": "33333333-abilify", "eu": 1},
        {"cis": "44444444", "name": "STUBONLY", "slug": "44444444-stubonly", "eu": 1},
    ]
    records = {
        "11111111": {"asof": "2022-05-02"},
        "22222222": {"asof": ""},                       # no date -> no <lastmod>
        "33333333": {"full": True, "asof": "2026-07-10T08:00:00"},  # full -> indexed
        "44444444": {"full": False, "asof": ""},        # bare stub -> excluded
    }
    with tempfile.TemporaryDirectory() as d:
        saved = build.DIST
        build.DIST = Path(d)
        try:
            n = build.write_sitemap(index, stub_index, records)
            xml = (Path(d) / "sitemap.xml").read_text()
        finally:
            build.DIST = saved
    base = build.SITE_URL
    # Static + browse (letters present: A for amoxicilline, # for the digit name).
    assert f"<loc>{base}/</loc>" in xml
    assert f"<loc>{base}/a-propos</loc>" in xml
    assert f"<loc>{base}/browse/</loc>" in xml
    assert f"<loc>{base}/browse/a</loc>" in xml
    assert f"<loc>{base}/browse/num</loc>" in xml
    # RCP pages, with lastmod only where asof is known.
    assert f"<loc>{base}/rcp/11111111-amoxicilline</loc><lastmod>2022-05-02</lastmod>" in xml
    assert f"<loc>{base}/rcp/22222222-300</loc></url>" in xml  # no lastmod
    # Full /eu/ is indexed (lastmod is the date part only); the bare stub is NOT.
    assert f"<loc>{base}/eu/33333333-abilify</loc><lastmod>2026-07-10</lastmod>" in xml
    assert "44444444-stubonly" not in xml
    # Count: 3 static + 2 browse letters + 2 rcp + 1 full eu = 8.
    assert n == 8, n
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?><urlset'), xml[:60]
    print("ok  test_sitemap_lists_indexable_pages")


_RCP_TEMPLATE_SLOTS = (
    "{{TITLE}}", "{{DESCRIPTION}}", "{{HEADEXTRA}}", "{{HEAD}}", "{{CIS}}",
    "{{BREADCRUMB}}", "{{TOC}}", "{{ASOF}}", "{{CONTENT}}", "{{XREF}}",
)


def _render_one_rcp(d):
    """Render a minimal RCP page in a temp DIST, returning (row, html). Sets the
    module globals render_record reads (normally primed by the pool worker)."""
    # This test now lives in src/ alongside rcp.html (a sibling), same dir as build.py.
    tpl = (Path(__file__).parent / "rcp.html").read_text()
    raw = ('<html><body><div id="textDocument">'
           '<p class="AmmAnnexeTitre1">1. DENOMINATION DU MEDICAMENT</p>'
           '<p>DOLIPRANE 1000 mg, comprime. Contient du paracetamol.</p>'
           '<p class="AmmAnnexeTitre1">4.1 Indications therapeutiques</p>'
           '<p>Traitement symptomatique des douleurs.</p>'
           '<span class="DateNotif">ANSM - Mis a jour le : 03/02/2021</span>'
           '</div></body></html>')
    saved = (build.DIST, build._TPL, build._NAMES, build._XREF, build._SUBSTANCES)
    build.DIST = Path(d)
    (Path(d) / "rcp").mkdir(exist_ok=True)
    build._TPL = tpl
    build._NAMES = {"12345678": "DOLIPRANE 1000 mg, comprime"}
    build._XREF = {}
    build._SUBSTANCES = {"12345678": "PARACETAMOL"}
    try:
        row = build.render_record(("12345678", raw, "2022-05-02"))
        html = (Path(d) / "rcp" / f"{row['slug']}.html").read_text()
    finally:
        (build.DIST, build._TPL, build._NAMES, build._XREF,
         build._SUBSTANCES) = saved
    return row, html


def test_rcp_page_has_canonical_and_no_leftover_slots():
    # Renders a real RCP page through the shared template and asserts the canonical is
    # injected AND no template slot survived unfilled (guards every render path that
    # fills the rcp.html slots). '}}' can legitimately appear inside JSON-LD, so we
    # check the named slot tokens, not a bare '{{'.
    with tempfile.TemporaryDirectory() as d:
        row, html = _render_one_rcp(d)
    for slot in _RCP_TEMPLATE_SLOTS:
        assert slot not in html, f"unfilled template slot {slot}"
    assert f'<link rel="canonical" href="{build.SITE_URL}/rcp/{row["slug"]}">' in html
    assert row["asof"] == "2022-05-02"  # rides back for the sitemap, not into search-index
    # Per-drug description: intent keywords + the active substance (from _SUBSTANCES).
    assert '<meta name="description" content="RCP de DOLIPRANE' in html
    assert "Substance active : paracetamol." in html
    # Open Graph / Twitter unfurl tags, with the canonical absolute og:url + logo card.
    assert '<meta property="og:type" content="article">' in html
    assert '<meta property="og:site_name" content="justelesRCP">' in html
    assert f'<meta property="og:url" content="{build.SITE_URL}/rcp/{row["slug"]}">' in html
    assert f'<meta property="og:image" content="{build.SITE_URL}/og.png">' in html
    assert '<meta name="twitter:card" content="summary">' in html
    # SVG favicon (from the template head).
    assert '<link rel="icon" type="image/svg+xml" href="/logo.svg">' in html
    # Two JSON-LD blocks: the Drug @graph, then the BreadcrumbList.
    blocks = [b.split("</script>")[0]
              for b in html.split('<script type="application/ld+json">')[1:]]
    assert len(blocks) == 2, len(blocks)
    ld = json.loads(blocks[0])
    types = {node["@type"] for node in ld["@graph"]}
    assert types == {"Drug", "MedicalWebPage"}, types
    drug = next(n for n in ld["@graph"] if n["@type"] == "Drug")
    assert drug["activeIngredient"] == "PARACETAMOL", drug
    page = next(n for n in ld["@graph"] if n["@type"] == "MedicalWebPage")
    assert page["lastReviewed"] == "2021-02-03", page  # ANSM revision date (ISO)
    assert page["mainEntity"]["@id"] == drug["@id"]
    # Breadcrumb: visible nav + matching BreadcrumbList (Accueil > A-Z > lettre > drug).
    assert '<nav class="breadcrumb"' in html
    assert 'href="/browse/d">D</a>' in html  # DOLIPRANE -> letter D
    crumb = json.loads(blocks[1])
    assert crumb["@type"] == "BreadcrumbList"
    names = [it["name"] for it in crumb["itemListElement"]]
    assert names[0] == "Accueil" and names[1] == "Médicaments A-Z"
    assert names[-1] == "DOLIPRANE 1000 mg, comprime"
    assert crumb["itemListElement"][-1]["item"] == f"{build.SITE_URL}/rcp/{row['slug']}"
    print("ok  test_rcp_page_has_canonical_and_no_leftover_slots")


def test_jsonld_escapes_script_breakout_and_website_searchaction():
    # A '<' in any value must be escaped so a drug name containing '</script>' can't
    # break out of the JSON-LD block; the escaped form still parses back to the value.
    blob = build._jsonld({"@type": "Drug", "name": "X </script><script>y"})
    inner = blob[len('<script type="application/ld+json">'):-len("</script>")]
    assert "</script>" not in inner and "\\u003c" in inner
    assert json.loads(inner)["name"] == "X </script><script>y"
    # WebSite + SearchAction on the home page points at the client-side search.
    ws = build._website_jsonld()
    assert ws["@type"] == "WebSite"
    assert ws["potentialAction"]["@type"] == "SearchAction"
    assert ws["potentialAction"]["target"]["urlTemplate"] == \
        f"{build.SITE_URL}/?q={{search_term_string}}"
    print("ok  test_jsonld_escapes_script_breakout_and_website_searchaction")


def test_robots_points_at_sitemap():
    with tempfile.TemporaryDirectory() as d:
        saved = build.DIST
        build.DIST = Path(d)
        try:
            build.write_robots()
            txt = (Path(d) / "robots.txt").read_text()
        finally:
            build.DIST = saved
    assert "User-agent: *" in txt
    assert "Disallow: /api/" in txt
    assert f"Sitemap: {build.SITE_URL}/sitemap.xml" in txt
    print("ok  test_robots_points_at_sitemap")


def test_clean_substance_strips_salt_hydrate():
    """A COMPO denomination carries the salt/hydrate INLINE (e.g. "AMOXICILLINE
    TRIHYDRATEE"), which breaks LeCRAT's strict all-terms ?s= search. _clean_substance
    must drop those qualifier words so the external-reference pills land on a real
    page, while keeping multi-word acids and salt-only minerals intact."""
    # the reported bug: the hydrate word made ?s=amoxicilline+trihydratee find nothing
    assert build._clean_substance("AMOXICILLINE TRIHYDRATÉE") == "AMOXICILLINE"
    # accented masculine hydrate + stacked salt words all go
    assert build._clean_substance("PAROXÉTINE CHLORHYDRATE HÉMIHYDRATÉ") == "PAROXÉTINE"
    # multi-word acids must stay whole ("ACIDE" is intentionally not a qualifier)
    assert build._clean_substance("ACIDE CLAVULANIQUE") == "ACIDE CLAVULANIQUE"
    # already-clean names and the legacy parenthetical-salt strip are unchanged
    assert build._clean_substance("ARIPIPRAZOLE") == "ARIPIPRAZOLE"
    assert build._clean_substance("RANITIDINE (CHLORHYDRATE DE)") == "RANITIDINE"
    # a substance that IS entirely salt/connector words is kept, not emptied
    assert build._clean_substance("SULFATE DE MAGNÉSIUM") == "SULFATE DE MAGNÉSIUM"
    print("ok  test_clean_substance_strips_salt_hydrate")


def test_ref_links_include_crat_pill():
    """_ref_links_html emits the 'En savoir plus' box with a CRAT pill pointing at
    lecrat.fr's ?s= search on the cleaned substance, and drops EMA on /eu/ pages."""
    saved = dict(build._SUBSTANCES)
    build._SUBSTANCES["99999999"] = build._clean_substance("AMOXICILLINE TRIHYDRATÉE")
    try:
        html = build._ref_links_html("99999999", "CLAMOXYL 500 mg")
    finally:
        build._SUBSTANCES.clear()
        build._SUBSTANCES.update(saved)
    assert 'class="rcp-more"' in html and "En savoir plus" in html
    assert 'href="https://www.lecrat.fr/?s=amoxicilline"' in html
    # /eu/ pages carry a direct EMA button already, so the EMA pill is dropped there
    eu = build._ref_links_html("99999999", "CLAMOXYL 500 mg", include_ema=False)
    assert "lecrat.fr" in eu and "ema.europa.eu" not in eu
    print("ok  test_ref_links_include_crat_pill")


def test_load_cap_meta_excludes_decentralised():
    """load_cap_meta must classify a row as EMA-centrally-authorized ONLY when it
    carries an EU number or a genuine 'centralisée' procedure. A 'décentralisée'
    (national) row folds to 'decentralisee', which contains the substring
    'centralis', so the old ``"centralis" in c.lower()`` check wrongly swept in
    every decentralised generic (giving it a spurious /eu/ page). This locks the
    fix: décentralisée rows are excluded, centralisée (with or without an EU
    number) are kept."""
    # CIS_bdpm.txt shape: CIS, name, form, route, status, PROCEDURE, marketing,
    # date, (empty), EU-number-or-empty, holder, ... (latin-1, tab-separated).
    def row(cis, name, proc, eu="", holder=""):
        return "\t".join([cis, name, "comprimé", "orale", "Autorisation active",
                          proc, "Commercialisée", "01/01/2020", "", eu, holder, "Non"])
    lines = [
        row("10000001", "ABILIFY 10 mg, comprimé", "Procédure centralisée",
            "EU/1/04/276", " OTSUKA"),                     # centralised + EU -> kept
        row("10000002", "SOMEDRUG 5 mg, comprimé", "Procédure centralisée"),  # centralised, no EU -> kept
        row("10000003", "ABACAVIR ARROW 300 mg, comprimé", "Procédure décentralisée"),  # national -> EXCLUDED
    ]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CIS_bdpm.txt"
        p.write_text("\n".join(lines) + "\n", encoding="latin-1")
        saved = build.BDPM_PATH
        build.BDPM_PATH = p
        try:
            meta = build.load_cap_meta()
        finally:
            build.BDPM_PATH = saved
    assert "10000001" in meta, meta
    assert meta["10000001"] == ("ABILIFY 10 mg, comprimé", "EU/1/04/276", "OTSUKA"), meta["10000001"]
    assert "10000002" in meta and meta["10000002"][1] == "", meta.get("10000002")
    # the decentralised generic must NOT be treated as centrally-authorized
    assert "10000003" not in meta, "décentralisée row wrongly classed as centralised"
    print("ok  test_load_cap_meta_excludes_decentralised")


def test_embed_page_to_vec_reports_stats():
    # embed-service logs throughput (chars/sec) from the optional stats out-dict: it is
    # filled with chunks + chars ONLY on the encode ("ok") path and left untouched when
    # the page is fresh (nothing encoded), so a chars/sec line never counts a skip.
    class _FakeEncoder:
        query_prefix = "query: "
        def encode_passages(self, texts):
            return [_l2_normalise([0.1] * 6) for _ in texts]

    raw = ("<div id='textDocument'><h1 class='AmmAnnexeTitre1'>4. Indications</h1>"
           "<p>Une phrase de contenu suffisamment longue pour tenir dans un chunk "
           "unique et dépasser le seuil de fusion des petits paragraphes.</p></div>")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "rcp").mkdir()
        (d / "rcp" / "12345678-drug.html").write_text("<html></html>")
        saved = build.DIST
        build.DIST = d
        try:
            enc = _FakeEncoder()
            info: dict = {}
            r1 = build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m", stats=info)
            assert r1 == "ok", r1
            assert info.get("chunks", 0) >= 1 and info.get("chars", 0) > 0, info
            info2: dict = {}
            r2 = build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m", stats=info2)
            assert r2 == "fresh" and info2 == {}, (r2, info2)  # skip leaves stats untouched
        finally:
            build.DIST = saved
    print("ok  test_embed_page_to_vec_reports_stats")


def test_rcp_archived_detects_zero_byte_overlay():
    # rcp_archived is the single canonical "is this drug delisted?" test: a zero-byte
    # ANSM overlay ("scraped, no RCP") means the drug left BDPM. A non-empty overlay
    # or no overlay at all is NOT archived. Shared by render_record (the retired
    # banner), the search-index ret tag, and the refresh service, so they never
    # disagree; this pins the mechanical zero-byte contract.
    with tempfile.TemporaryDirectory() as d:
        rcp = Path(d) / "rcp"; rcp.mkdir()
        (rcp / "22222222.html").write_text("")              # zero-byte -> archived
        (rcp / "44444444.html").write_text("<div>x</div>")  # has an RCP -> not archived
        saved = build.RCP_OVERLAY_DIR
        build.RCP_OVERLAY_DIR = rcp
        try:
            assert build.rcp_archived("22222222") is True
            assert build.rcp_archived("44444444") is False
            assert build.rcp_archived("11111111") is False  # no overlay at all
        finally:
            build.RCP_OVERLAY_DIR = saved
    print("ok  test_rcp_archived_detects_zero_byte_overlay")


def test_iter_rcp_raw_serves_delisted_baseline():
    # The delisting bug fix: when a drug is re-scraped and the ANSM no longer
    # publishes its RCP (zero-byte overlay), we must KEEP serving the 2022 baseline
    # text (flagged archived) rather than dropping the page to a 404. Only when the
    # baseline is ALSO empty is the page truly skipped.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        csv_path = d / "CIS_RCP.csv"
        rows = [
            "Code_CIS\tRCP_html",
            "11111111\t<div id=\"textDocument\">baseline one</div>",   # normal (no overlay)
            "22222222\t<div id=\"textDocument\">baseline two</div>",   # delisted -> archived
            "33333333\t",                                              # empty baseline + delisted
            "44444444\t<div id=\"textDocument\">baseline four</div>",  # live overlay wins
        ]
        csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        (rcp / "22222222.html").write_text("")                                 # zero-byte sentinel
        (rcp / "33333333.html").write_text("")                                 # zero-byte sentinel
        (rcp / "44444444.html").write_text("<div id=\"textDocument\">fresh four</div>")
        saved_csv, saved_dir = build.CSV_PATH, build.RCP_OVERLAY_DIR
        build.CSV_PATH = csv_path
        build.RCP_OVERLAY_DIR = rcp
        try:
            stats = {"empty": 0}
            out = {cis: (raw, asof) for cis, raw, asof
                   in build.iter_rcp_raw(scrape_dates={}, stats=stats)}
            archived = {cis: build.rcp_archived(cis) for cis in out}
        finally:
            build.CSV_PATH, build.RCP_OVERLAY_DIR = saved_csv, saved_dir
        # The delisted drug with a surviving baseline is STILL rendered, from the
        # 2022 baseline, at BASELINE_DATE, and flagged archived.
        assert "22222222" in out, "delisted drug with a baseline must not vanish"
        assert "baseline two" in out["22222222"][0]
        assert out["22222222"][1] == build.BASELINE_DATE
        assert archived["22222222"] is True
        # A normal baseline drug: served, not archived.
        assert "baseline one" in out["11111111"][0] and archived["11111111"] is False
        # A live overlay still wins over the baseline (unchanged behaviour).
        assert "fresh four" in out["44444444"][0] and archived["44444444"] is False
        # Delisted AND no baseline text -> genuinely dropped (counted empty).
        assert "33333333" not in out and stats["empty"] == 1
    print("ok  test_iter_rcp_raw_serves_delisted_baseline")


def test_record_hash_changes_when_archived():
    # A drug that gets delisted keeps serving the SAME baseline raw at the SAME
    # BASELINE_DATE asof, so ONLY the archived flag changes. The record hash must
    # fold it in, else the incremental cache would treat the record as unchanged and
    # reuse the old NON-archived page, and the retired banner would never appear.
    raw, name, asof = "<div id='textDocument'>x</div>", "DRUG", build.BASELINE_DATE
    assert build._record_hash(raw, name, asof, False) != \
        build._record_hash(raw, name, asof, True)
    # Default arg keeps the historical (non-archived) hash stable.
    assert build._record_hash(raw, name, asof) == \
        build._record_hash(raw, name, asof, False)
    print("ok  test_record_hash_changes_when_archived")


if __name__ == "__main__":
    test_load_cap_meta_excludes_decentralised()
    test_clean_substance_strips_salt_hydrate()
    test_ref_links_include_crat_pill()
    test_quantize_roundtrip()
    test_section_chunks_align_with_toc()
    test_heading_only_section_yields_no_chunk()
    test_heading_path_context_prefix()
    test_merge_small_chunks()
    test_dsfr_backtotop_chrome_stripped()
    test_filler_paragraphs_dropped()
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
    test_iter_overlay_raw_honors_caller_order()
    test_sentence_chunks_group_and_fallbacks()
    test_long_section_tail_survives_raised_cap()
    test_duplicate_chunks_within_page_dropped()
    test_sitemap_lists_indexable_pages()
    test_robots_points_at_sitemap()
    test_rcp_page_has_canonical_and_no_leftover_slots()
    test_jsonld_escapes_script_breakout_and_website_searchaction()
    test_embed_page_to_vec_reports_stats()
    test_rcp_archived_detects_zero_byte_overlay()
    test_iter_rcp_raw_serves_delisted_baseline()
    test_record_hash_changes_when_archived()
    print("\nAll tests passed.")

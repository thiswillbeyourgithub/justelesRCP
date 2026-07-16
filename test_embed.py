# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
#   "brotli>=1.1",
# ]
# ///
"""Unit tests for the pure semantic-search helpers in build.py.

Run: ``uv run test_embed.py`` (no ML dependency; the model-side embedding lives in
embed-rcp.py). Covers the two fiddly pure pieces the feature relies on:

1. int8 quantise/dequantise round-trip stays within the ~1/127 error bound, so a
   query's cosine ranking over the baked section vectors is faithful.
2. section_chunks() stays aligned with clean_rcp()'s sec-N anchors, so a search hit
   scrolls to a heading that actually exists in the rendered page.
"""

import math

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
    toc_ids = [sid for sid, _ in toc]
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


if __name__ == "__main__":
    test_quantize_roundtrip()
    test_section_chunks_align_with_toc()
    test_empty_and_untitled()
    test_table_rows_stay_intact()
    test_layout_table_falls_back_flat()
    print("\nAll tests passed.")

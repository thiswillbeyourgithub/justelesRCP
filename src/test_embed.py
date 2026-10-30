# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "numpy",
#   "onnxruntime",
#   "tokenizers",
#   "loguru",
#   "click",
#   "html5lib",
# ]
# ///
"""Unit tests for the pure semantic-search helpers in build.py.

Run: ``uv run test_embed.py`` (no model needed: onnxruntime/tokenizers are listed
only so onnx_embed.py imports, for its pure query-cache test, and loguru/click so
embed-service.py imports for its model-free route tests; the model-side embedding
lives in onnx_embed.py / embed-service.py). Covers the fiddly pure pieces the feature relies
on:

1. int8 quantise/dequantise round-trip stays within the ~1/127 error bound, so a
   query's cosine ranking over the baked section vectors is faithful.
2. section_chunks() stays aligned with clean_rcp()'s sec-N anchors, so a search hit
   scrolls to a heading that actually exists in the rendered page.
3. vec_payload/write_vec_json/read_vec_meta round-trip: the .vec.json the embed
   service and embed-rcp.py write is decodable by the browser, and its baked
   src_hash + model (the content-hash staleness key, no manifest) survives a
   write+read.
"""

import base64
import json
import math
import os
import struct
import tempfile
from pathlib import Path

import build
import svc_http

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


def test_unstyled_subheads_promoted():
    # v7: a plain short line with no closing punctuation, followed by prose, is a
    # subheading in disguise (EMA SmPCs set "Absorption", "Élimination"... that way).
    # It must become an AmmAnnexeTitre3 under its numbered parent: listed in the ToC and
    # carried in the chunk's heading path, instead of being dropped as 1-word filler.
    sample = """<div id="textDocument">
      <p class="AmmAnnexeTitre1">5. PROPRIETES PHARMACOLOGIQUES</p>
      <p class="AmmAnnexeTitre2">5.2 Proprietes pharmacocinetiques</p>
      <p>Élimination</p>
      <p>La demi-vie moyenne d'elimination terminale est de vingt-neuf jours apres injection.</p>
      <p>Les effets suivants ont ete rapportes :</p>
      <p>Cephalees frequentes</p>
      <p>Une somnolence a ete observee chez certains patients traites au long cours.</p>
      <p>MARCHÉ</p>
      <p>Un texte de prose suffisamment long pour suivre la ligne en majuscules.</p>
      <p>Tel: +47 32 20 73 00</p>
      <p>Un texte de prose suffisamment long pour suivre la ligne de telephone.</p>
      <p>Poids du</p>
      <p>Un texte de prose suffisamment long pour suivre la ligne coupee.</p>
      <p>Ligne courte</p>
      <p>Autre ligne courte</p>
      <p>Phrase complete et finie.</p>
      <p>Un texte de prose suffisamment long pour suivre la phrase finie.</p>
      <table><tr><td><p>Cellule de tableau</p><p>Une phrase dans la cellule du tableau.</p></td></tr></table>
    </div>"""
    _, cleaned, toc, _ = build.clean_rcp(sample)
    subs = toc[0][2][0][2]  # 5. > 5.2 > [promoted]
    assert [t for _, t, _ in subs] == ["Élimination"], subs
    elim = next(t for _, _, t in build.section_chunks(sample) if "vingt-neuf" in t)
    assert elim.startswith("5. PROPRIETES PHARMACOLOGIQUES > 5.2 Proprietes "
                           "pharmacocinetiques > Élimination\n"), elim
    print("ok  test_unstyled_subheads_promoted")


def test_prose_tagged_as_heading_is_demoted():
    # IMATINIB BGR (66544554) tags whole paragraphs AmmAnnexeTitre3: they listed in the
    # Sommaire, and every chunk below carried ~900 chars of them as heading prefix
    # (twice the tokens per batch, the embed container ran out of RAM). Long lines and
    # finished sentences go back to body text; real subheadings, and Titre1, stay.
    long_p = "Un total de 26 patients pediatriques " + "ont ete recrutes " * 30
    sample = f"""<div id="textDocument">
      <p class="AmmAnnexeTitre1">5. PROPRIETES PHARMACOLOGIQUES</p>
      <p class="AmmAnnexeTitre2">5.1 Proprietes pharmacodynamiques</p>
      <p class="AmmAnnexeTitre3">Population pediatrique</p>
      <p class="AmmAnnexeTitre3">{long_p}</p>
      <p class="AmmAnnexeTitre3">Les etudes de mutagenicite et de carcinogenicite a long terme n'ont pas ete effectuees.</p>
      <p>Une phrase de prose qui suit les deux faux titres dans la section.</p>
    </div>"""
    _, _, toc, _ = build.clean_rcp(sample)
    subs = toc[0][2][0][2]  # 5. > 5.1 > [...]
    assert [t for _, t, _ in subs] == ["Population pediatrique"], subs
    chunks = build.section_chunks(sample)
    assert all(len(t.split("\n")[0]) < 120 for _, _, t in chunks), chunks
    assert any("Un total de 26" in sn for _, sn, _ in chunks), chunks  # now searchable
    print("ok  test_prose_tagged_as_heading_is_demoted")


def test_eu_toc_keeps_ema_ids_and_nests_subheads():
    # A full /eu/ page's ToC nests the open (SmPC) group's numbered subsections and the
    # promoted subheadings, KEEPING ema_pdf's sec-N ids (the chunks anchor to them),
    # while a collapsed annex group (labelling mock-ups, addresses) is never promoted.
    overlay = """<div id="textDocument">
      <details class="ema-annexe" open><summary id="grp-1">ANNEXE I</summary>
        <h2 class="AmmAnnexeTitre1" id="sec-4">4. INFORMATIONS CLINIQUES</h2>
        <h3 class="AmmAnnexeTitre2">4.2 Posologie et mode d'administration</h3>
        <p>Populations particulieres</p>
        <p>Aucun ajustement posologique n'est necessaire chez le sujet age.</p>
      </details>
      <details class="ema-annexe"><summary id="grp-2">ANNEXE III</summary>
        <p>Boite de carton</p>
        <p>Un texte de prose suffisamment long pour suivre la ligne courte.</p>
      </details>
    </div>"""
    body, toc = build._eu_prepare(overlay)
    assert [g[0] for g in toc] == ["grp-1", "grp-2"], toc
    (sec_id, _, kids), = toc[0][2]
    assert sec_id == "sec-4", toc
    assert kids == [("grp-1-sub-0", "4.2 Posologie et mode d'administration",
                     [("grp-1-sub-1", "Populations particulieres", [])])], kids
    assert toc[1][2] == [] and "<p>Boite de carton</p>" in body, body
    assert 'id="grp-1-sub-1"' in body
    print("ok  test_eu_toc_keeps_ema_ids_and_nests_subheads")


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


def test_merged_chunk_snippet_spans_its_paragraph_run():
    """A merged chunk's snippet must resolve to the WHOLE run of source paragraphs.

    Regression for the VERATRAN bug: _merge_small folds a short paragraph into its
    neighbour, so one embedded chunk covers several sibling <p>s. The client's locate()
    used to return the single block matching the snippet's first 40 chars and
    displayText() then showed only THAT block, so searching "Fraction de liaison aux
    protéines" surfaced the right chunk at rank #1 but printed the 42-char paragraph
    merged in front of it ("Le volume de distribution est de 3,5 l/kg."), hiding the
    answer. locate() now walks forward consuming the snippet block by block, which only
    works because the body is a SINGLE-SPACE join of the paragraphs; this test pins both
    halves of that contract.
    """
    paras = [
        "Le volume de distribution est de 3,5 l/kg.",
        "La liaison aux proteines est importante, en moyenne de 98 %.",
        "La demi-vie d'elimination plasmatique du clotiazepam est de 4 heures.",
        "L'etat d'equilibre des concentrations plasmatiques est atteint des la 5eme dose.",
    ]
    sample = (
        '<div id="textDocument">'
        '<p class="AmmAnnexeTitre1">5. PROPRIETES PHARMACOLOGIQUES</p>'
        '<p class="AmmAnnexeTitre3">Distribution</p>'
        + "".join(f'<p class="AmmCorpsTexte">{p}</p>' for p in paras)
        + "</div>"
    )
    chunks = build.section_chunks(sample)
    hit = [c for c in chunks if "liaison aux proteines" in c[2]]
    assert len(hit) == 1, chunks
    sec_id, snippet, _text = hit[0]
    assert sec_id == "sec-0", sec_id

    # The short lead paragraph was merged in, so the snippet reaches past it. Without
    # this the bug cannot occur at all and the rest of the test would be vacuous.
    assert snippet.startswith(paras[0]), snippet
    assert len(snippet) > len(paras[0]), snippet

    # Re-implement locate()'s consume-walk over the rendered paragraphs (see
    # rcp-semsearch.js). It must land on the run that actually holds the answer.
    needle = snippet[:40]
    run, rest = [], ""
    for i, para in enumerate(paras):
        at = para.find(needle)
        if at == -1:
            continue
        run = [para]
        rest = snippet[len(para) - at:].strip()
        for nxt in paras[i + 1:]:
            if not rest:
                break
            if rest.startswith(nxt):        # wholly inside the snippet: keep walking
                run.append(nxt)
                rest = rest[len(nxt):].strip()
            elif nxt.startswith(rest):      # the truncated tail lands here: last block
                run.append(nxt)
                rest = ""
                break
            else:
                break
        break
    assert run[0] == paras[0], run
    assert any("liaison aux proteines" in p for p in run), run
    # displayText() joins the run, so the reader sees the sentence they searched for.
    assert "liaison aux proteines" in " ".join(run)
    print("ok  test_merged_chunk_snippet_spans_its_paragraph_run")


def test_flat_table_snippet_resolves_to_its_rows():
    """A chunk cut from a table kept FLAT (not header-led) must resolve to its rows.

    Regression for AMOXICILLINE VIATRIS: the side-effects table opens with a one-cell
    title row, so _linearize_table refuses it and section_chunks embeds its flat text.
    The snippet ("Langue noire chevelue Fréquence indéterminée ...") then spans several
    <td>s, no single cell holds its first 40 chars, and locate() fell back to the
    section heading, scrolling the reader to "4. DONNEES CLINIQUES" far from the
    passage. locateAcross() (rcp-semsearch.js) matches against the single-space join
    of the leaf cells instead; this re-implements it over the rendered cells.
    """
    # A long side-effects table (no closing periods, so its flat text is word-windowed
    # and a later chunk starts mid-table, exactly like the real page). Cells are
    # newline-separated as in real ANSM HTML.
    socs = ["Affections gastro-intestinales", "Affections hépatobiliaires",
            "Affections de la peau et du tissu sous-cutané", "Affections du rein"]
    effects = ["Diarrhée", "Nausées", "Vomissements", "Candidose cutanéo-muqueuse",
               "Colite associée aux antibiotiques", "Langue noire chevelue",
               "Syndrome d'entérocolite induite par les médicaments", "Hépatite",
               "Ictère cholestatique", "Éruption cutanée", "Urticaire", "Prurit",
               "Néphrite interstitielle", "Cristallurie", "Angio-oedème", "Purpura"]
    rows = [[socs[i % len(socs)], e, "Fréquence indéterminée"] for i, e in enumerate(effects)]
    table = [["Effets indésirables"]] + rows
    sample = (
        '<div id="textDocument">\n'
        '<p class="AmmAnnexeTitre1">4. DONNEES CLINIQUES</p>\n'
        '<table>\n<tr><td colspan="3"><p>Effets indésirables</p></td></tr>\n'
        + "".join("<tr>\n" + "".join(f"<td><p>{c}</p></td>\n" for c in r) + "</tr>\n" for r in rows)
        + "</table></div>"
    )
    cells = [(r, build._norm_ws(c).lower()) for r, row in enumerate(table) for c in row]
    starts, joined = [], ""
    for _, t in cells:
        if joined and t:
            joined += " "
        starts.append(len(joined))
        joined += t

    def resolve(snippet):  # locateAcross() over the rendered cells -> row indices
        at = joined.find(snippet[:40])
        if at == -1:
            return None
        end, run = at + len(snippet), []
        for k, (row, _t) in enumerate(cells):
            nxt = starts[k + 1] if k + 1 < len(cells) else len(joined) + 1
            if nxt <= at:
                continue
            if starts[k] >= end:
                break
            if row not in run:
                run.append(row)
        return run

    chunks = build.section_chunks(sample)
    mid = [build._norm_ws(c[1]).lower() for c in chunks]
    mid = [s for s in mid if not any(s[:40] in t for _, t in cells)]
    # The bug's precondition: some chunk's head spans cells (no single cell holds it).
    assert mid, chunks
    for snippet in mid:
        run = resolve(snippet)
        assert run, (snippet, joined)
        # The run opens on the row holding the snippet's first word, not on the heading.
        assert any(snippet.split()[0] in t for r, t in cells if r == run[0]), (snippet, run)
    print("ok  test_flat_table_snippet_resolves_to_its_rows")


def test_linearised_row_snippet_resolves_to_its_row():
    """A header-led table row's chunk must resolve to that <tr>, not to the heading.

    Regression for ZYPADHERA: _linearize_table embeds each row as "header: value; ..."
    ("Très fréquent: Troubles du métabolisme et de la nutrition; ..."), a string that
    is nowhere on the page, so locate() fell back to the section heading. locateRow()
    (rcp-semsearch.js) rebuilds each rendered row the same way and matches the snippet
    against it; this re-implements that rebuild over the rendered cells and checks
    every row chunk lands on its own row.
    """
    header = ["Très fréquent", "Fréquent", "Peu fréquent"]
    rows = [
        ["Troubles du métabolisme et de la nutrition", "Prise de poids", ""],
        ["Affections du système nerveux", "Somnolence, sensations vertigineuses", "Convulsions"],
        ["Affections vasculaires", "", "Hypotension orthostatique, bradycardie"],
    ]
    sample = (
        '<div id="textDocument">\n'
        '<p class="AmmAnnexeTitre1">4. DONNÉES CLINIQUES</p>\n'
        "<table>\n<tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr>\n"
        + "".join("<tr>" + "".join(f"<td><p>{c}</p></td>" for c in r) + "</tr>\n" for r in rows)
        + "</table></div>"
    )

    def rebuilt(cells):  # locateRow()'s mirror of _linearize_table, per rendered row
        pairs = [f"{h}: {v}" if h else v
                 for h, v in zip(header, (build._norm_ws(c) for c in cells)) if v]
        return build._norm_ws("; ".join(pairs)).lower()

    lines = [rebuilt(r) for r in rows]
    chunks = build.section_chunks(sample)
    row_chunks = [build._norm_ws(c[1]).lower() for c in chunks if ": " in c[1]]
    assert len(row_chunks) == len(rows), chunks
    assert row_chunks[0].startswith("très fréquent: troubles du métabolisme"), row_chunks
    for i, hay in enumerate(row_chunks):
        found = [j for j, line in enumerate(lines)
                 if line.startswith(hay) or hay.startswith(line)]
        assert found == [i], (hay, lines)
    print("ok  test_linearised_row_snippet_resolves_to_its_row")


def _located_page():
    """A page exercising every way a chunk's text stops being contiguous on the page:
    a merged paragraph run with a filler ("2 ans") dropped from its middle, a
    header-led table (linearised rows, also the side-effects table of section 5 whose
    first row reads as a header), and a <p> wrapping
    a <table>, which a browser closes before the table (one extra, empty block that
    shifts every later block of the section)."""
    long = "La stabilité physico-chimique de la solution reconstituée a été démontrée. "
    return (
        '<div id="textDocument">\n'
        '<p class="AmmAnnexeTitre1" id="sec-0">4. DONNÉES CLINIQUES</p>\n'
        "<p>Flacon non ouvert conservé à l'abri de la lumière dans son emballage.</p>\n"
        "<p>2 ans</p>\n"
        f"<p>{long * 3}</p>\n"
        "<table><tr><th>Très fréquent</th><th>Fréquent</th></tr>\n"
        "<tr><td><p>Troubles du métabolisme et de la nutrition</p></td>"
        "<td><p>Prise de poids importante</p></td></tr>\n"
        "<tr><td><p>Affections du système nerveux</p></td>"
        "<td><p>Somnolence, sensations vertigineuses</p></td></tr></table>\n"
        '<p align="center"><span><div><table><tr><td><p>Risque relatif, 0,936 '
        "(IC 95 %, 0,887-0,988)</p></td></tr></table></div></span></p>\n"
        "<p>Paragraphe après le tableau imbriqué, assez long pour faire un passage "
        "à lui seul et vérifier que le décalage est rattrapé.</p>\n"
        '<p class="AmmAnnexeTitre1" id="sec-1">5. PROPRIÉTÉS PHARMACOLOGIQUES</p>\n'
        "<table><tr><td>Langue noire chevelue</td><td>Fréquence indéterminée</td></tr>"
        "<tr><td>Coloration des dents</td><td>Fréquence indéterminée</td></tr></table>\n"
        "</div>"
    )


def test_chunk_locations_land_on_their_blocks():
    """Every chunk records the blocks it was cut from, and those blocks hold its text
    on the page as a browser parses it (html5lib), replaying locateByBlocks().

    Regression for the AMOXICILLINE and ZYPADHERA hits that scrolled to the section
    heading: the browser used to FIND each chunk by its text, which fails whenever that
    text is not contiguous on the page (about 19% of chunks on a 300-page audit). Here
    the filler-dropped run, the linearised rows and the paragraph after
    the browser-repaired <p><table> must all land, the last one only after the shift.
    """
    audit = svc_http.load_sibling("audit-locate.py", "audit_locate")
    raw = _located_page()
    doc = audit._parse(raw)
    chunks = build.section_chunks(raw, located=True)
    assert [c[:3] for c in chunks] == build.section_chunks(raw), \
        "located=True must not change the chunks themselves"
    reasons = {}
    for sec, snippet, _t, loc in chunks:
        assert loc is not None, (sec, snippet)
        reasons[snippet[:30]] = audit.check(doc, sec, snippet, loc[:2], loc[2])
    assert set(reasons.values()) <= {"ok", "shifted"}, reasons
    # The filler sits between the two paragraphs of ONE chunk: its run spans all three.
    head = next(c for c in chunks if c[1].startswith("Flacon non ouvert"))
    assert head[3][1] - head[3][0] == 2 and "2 ans" not in head[1], head
    rows = [c for c in chunks if c[3][2] and c[0] == "sec-0"]
    assert [c[1].split(";")[0] for c in rows] == [
        "Très fréquent: Troubles du métabolisme et de la nutrition",
        "Très fréquent: Affections du système nerveux"], rows
    assert "shifted" in reasons.values(), reasons
    print("ok  test_chunk_locations_land_on_their_blocks")


def test_vec_add_locations_backfills_without_reencoding():
    """A sidecar embedded before chunk locations existed gets them in place (vectors
    untouched), and only when re-segmenting reproduces its chunks one for one."""
    raw = _located_page()
    chunks = build.section_chunks(raw, located=True)
    vecs = [_l2_normalise([0.1 * (i + 1)] * 6) for i in range(len(chunks))]
    src = build.raw_hash(raw)
    fresh = build.vec_payload(chunks, vecs, "m", "q: ", src, quant="int8")
    assert fresh["loc"] == build.VEC_LOC_VERSION
    assert all("b" in c for c in fresh["chunks"]), fresh["chunks"]
    assert sum(c.get("r", 0) for c in fresh["chunks"]) == 3, fresh["chunks"]
    old = build.vec_payload([c[:3] for c in chunks], vecs, "m", "q: ", src,
                            quant="int8")
    assert "loc" not in old and not any("b" in c for c in old["chunks"]), old
    with tempfile.TemporaryDirectory() as d:
        vec = Path(d) / "12345678-x.vec.json"
        build.write_vec_json(vec, old)
        assert build.read_vec_meta(vec)["loc"] is None
        assert build.vec_add_locations(vec, raw, "12345678")
        assert json.loads(build.read_served(vec)) == fresh
        assert not build.vec_add_locations(vec, raw, "12345678"), "already current"
        # Different text (stale src_hash) or different chunks: refuse, never guess.
        build.write_vec_json(vec, old)
        assert not build.vec_add_locations(vec, raw + "<p>x</p>", "12345678")
        tampered = json.loads(json.dumps(old))
        tampered["chunks"][0]["snippet"] = "autre chose"
        build.write_vec_json(vec, tampered)
        assert not build.vec_add_locations(vec, raw, "12345678")
    print("ok  test_vec_add_locations_backfills_without_reencoding")


def test_eu_page_without_heading_ids_gets_the_chunks_anchors():
    """An EMA overlay whose headings ema_pdf could not number (no AmmAnnexeTitre1, no
    ids: _promote_numbered_titles promotes them) inside a CLOSED <details> group must
    still render the sec-N ids section_chunks anchors to. Regression: on CAELYX (and 6
    other /eu/ pages) _eu_toc only ids the OPEN group's headings, so the page had no
    sec-N at all and every search hit pointed at a heading that did not exist."""
    import re
    raw = (
        '<div id="textDocument"><details class="ema-annexe"><summary id="grp-1">'
        "ANNEXE I</summary>\n"
        "<p>1. DÉNOMINATION DU MÉDICAMENT</p>\n"
        "<p>Caelyx pegylated liposomal 2 mg/ml solution à diluer pour perfusion, "
        "un texte assez long pour faire un passage.</p>\n"
        "<p>4. DONNÉES CLINIQUES</p>\n"
        "<p>Caelyx pegylated liposomal est indiqué en monothérapie chez les patients "
        "ayant un cancer du sein métastatique.</p>\n"
        "</details></div>"
    )
    secs = {c[0] for c in build.section_chunks(raw)}
    assert secs == {"sec-0", "sec-1"}, secs
    body, _toc = build._eu_prepare(raw)
    assert secs <= set(re.findall(r'id="(sec-\d+)"', body)), body
    print("ok  test_eu_page_without_heading_ids_gets_the_chunks_anchors")


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
# (AmmAnnexeTitre2) and one deeper heading (AmmAnnexeTitre3, nested at depth 3).
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
    # The two AmmAnnexeTitre2 headings nest UNDER "4. DONNEES CLINIQUES" as sub-0/sub-1,
    # and the AmmAnnexeTitre3 "Posologie" under 4.2 as sub-2 (depth 3).
    children = clinique[2]
    assert [sid for sid, _, _ in children] == ["sub-0", "sub-1"], children
    assert "4.1 Indications" in children[0][1]
    assert children[0][2] == [] and children[1][2] == [("sub-2", "Posologie", [])]

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
                                "query: ", "abc123def456", quant="int8")
    assert payload["model"] == "Xenova/multilingual-e5-small"
    assert payload["query_prefix"] == "query: "
    assert payload["src_hash"] == "abc123def456"
    assert payload["dim"] == 8, payload["dim"]
    assert payload["quant"] == "int8", payload["quant"]  # as asked for, not the default
    assert len(payload["chunks"]) == 2
    c0 = payload["chunks"][0]
    assert c0["sec"] == "sec-0" and c0["snippet"] == "prise pendant les repas"
    # q decodes back to ~ the original vector, within the int8 error bound.
    deq = _decode_q(c0["q"])
    assert len(deq) == 8
    for original, restored in zip(vecs[0], deq):
        assert abs(original - restored) <= 1.0 / 127 + 1e-9, (original, restored)
    print("ok  test_vec_payload_roundtrip")


def _decode_binary_q(b64, dim):
    """Decode a base64 packed-bit vector back to floats, exactly as the browser's
    decodeVec does for quant "binary": a set bit is +1/sqrt(dim), a clear one
    -1/sqrt(dim), MSB first inside each byte. The result is a UNIT vector, so dotting it
    with a unit-norm query yields a genuine cosine."""
    raw = base64.b64decode(b64)
    assert len(raw) == dim // 8, (len(raw), dim)
    scale = 1.0 / math.sqrt(dim)
    return [scale if raw[i >> 3] & (0x80 >> (i & 7)) else -scale for i in range(dim)]


def test_quantize_binary_packs_signs_msb_first():
    # One bit per dimension, MSB first, so the packing matches numpy's packbits default
    # and the browser's unpacking. Byte 0 here is 1,0,1,0,0,0,0,0 = 0b10100000 = 0xA0.
    packed = build.quantize_binary([0.5, -0.5, 0.01, -0.01, -1.0, -1.0, -1.0, -1.0])
    assert packed == b"\xa0", packed
    # Only the SIGN survives: two vectors differing only in magnitude pack identically,
    # which is the point (and why the score shrinks relative to the float cosine).
    assert build.quantize_binary([0.9, -0.1] * 4) == build.quantize_binary([0.1, -0.9] * 4)
    # Exactly zero is NOT positive (matches numpy's `v > 0`), so it packs as a clear bit.
    assert build.quantize_binary([0.0] * 8) == b"\x00"
    # A width that is not a multiple of 8 is refused rather than padded: a padding bit is
    # indistinguishable from a real negative component and would skew every cosine.
    try:
        build.quantize_binary([0.1] * 12)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on a width not divisible by 8")
    print("ok  test_quantize_binary_packs_signs_msb_first")


def test_vec_payload_binary_is_eight_times_narrower_and_ranks_the_same():
    # The binary payload is the same wire shape with quant="binary", dim = the LOGICAL
    # width (not the byte count) and q = base64 of dim/8 bytes.
    chunks = [
        ("sec-0", "posologie", "4.2 posologie: a prendre au cours des repas"),
        ("sec-3", "grossesse", "4.6 grossesse: deconseille au 3e trimestre"),
    ]
    vecs = [
        _l2_normalise([0.1 * (i - 3) for i in range(16)]),
        _l2_normalise([(-1) ** i * 0.2 for i in range(16)]),
    ]
    payload = build.vec_payload(chunks, vecs, "m", "query: ", "abc123def456",
                                quant="binary")
    assert payload["quant"] == "binary"
    assert payload["dim"] == 16, payload["dim"]  # LOGICAL width, not the 2 stored bytes
    assert len(base64.b64decode(payload["chunks"][0]["q"])) == 2
    # Eight times fewer bytes than int8 at the same width, which is the reason to do it.
    int8_payload = build.vec_payload(chunks, vecs, "m", "query: ", "abc123def456",
                                     quant="int8")
    assert len(base64.b64decode(int8_payload["chunks"][0]["q"])) == 16
    # Ranking survives: a query that IS one of the passages must still score that passage
    # highest, and the decoded passage must be a unit vector, so the score the reader
    # displays stays a real cosine.
    decoded = [_decode_binary_q(c["q"], 16) for c in payload["chunks"]]
    for vec in decoded:
        assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-9
    query = vecs[0]
    scores = [sum(a * b for a, b in zip(query, vec)) for vec in decoded]
    assert scores[0] > scores[1], scores
    # An unknown quantisation is a loud failure, never a silent fallback to int8: bytes
    # read under the wrong scheme are noise, not a slightly worse answer.
    try:
        build.vec_payload(chunks, vecs, "m", "query: ", "h", quant="float16")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on an unknown quantisation")
    print("ok  test_vec_payload_binary_is_eight_times_narrower_and_ranks_the_same")


def test_write_read_vec_meta_roundtrip():
    # write_vec_json stores the .vec.json as its .br ONLY (no plain copy, no .gz, and a
    # stale .gz from an older build is removed); read_vec_meta recovers the baked
    # {src_hash, model, dim} (the content-hash staleness key, no manifest).
    payload = build.vec_payload(
        [("sec-0", "snip", "un texte")],
        [_l2_normalise([0.3] * 6)],
        "Xenova/multilingual-e5-small", "query: ", "feedface1234", quant="int8",
    )
    with tempfile.TemporaryDirectory() as d:
        vec = Path(d) / "12345678-doliprane.vec.json"
        gz = vec.with_name(vec.name + ".gz")
        gz.write_bytes(b"stale")  # left over by a pre-brotli-only build
        build.write_vec_json(vec, payload)
        br = vec.with_name(vec.name + ".br")
        assert not vec.exists(), "the .vec.json must be stored br-only"
        assert br.exists()
        assert not gz.exists(), "write_served must drop a stale .gz sibling"
        meta = build.read_vec_meta(vec)
        assert meta == {"src_hash": "feedface1234",
                        "model": "Xenova/multilingual-e5-small", "dim": 6,
                        "quant": "int8", "loc": None}, meta
        # A file baked BEFORE the binary format existed has no "quant" key at all. It
        # must read back as int8 (which is what it is), not as None, or the freshness
        # gate would call the whole legacy catalog stale and re-embed it for nothing.
        # It is also a legacy PLAIN file (written before br-only storage): the VPS
        # keeps those until re-embedded, so they must still read back.
        legacy = json.loads(build.read_served(vec))
        legacy.pop("quant")
        br.unlink()
        vec.write_text(json.dumps(legacy), encoding="utf-8")
        assert build.read_vec_meta(vec)["quant"] == "int8"
        vec.unlink()
        assert build.read_vec_meta(vec) is None
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
                                    "old-model", "query: ", "cafe1234cafe1234",
                                    quant="int8")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        overlay = d / "12345678.html"
        overlay.write_text("<div id='textDocument'>x</div>")
        vec = d / "12345678-doliprane.vec.json"

        # 1. No vec yet -> not fresh (embed it).
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        build.write_vec_json(vec, payload_old)
        # 2. Vec OLDER than overlay -> not fresh (a re-crawl bumped the overlay).
        os.utime(overlay, (overlay.stat().st_atime, build.served_mtime(vec) + 10))
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        # 3. Vec NEWER than overlay, mtime-only pass -> fresh (the common skip).
        os.utime(overlay, (overlay.stat().st_atime, build.served_mtime(vec) - 10))
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=False)

        # 4. Same, check_model pass, baked model MATCHES current -> still fresh.
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=True)

        # 5. Same, check_model pass, current model DIFFERS from the baked one -> stale,
        #    so the startup pass re-embeds it despite the newer mtime (the #1 fix).
        assert not build.vec_is_fresh(vec, overlay, "new-model", check_model=True)

        # 6. check_model pass with a matching served width (dim=6, the payload's) ->
        #    still fresh; a DIFFERING width (EMBED_OUT_DIM change) -> stale, re-embed
        #    (else the reader's new-width query would rank against old-width passages).
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=True, dim=6)
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=True, dim=8)

        # 7. Same for the QUANTISATION (EMBED_VEC_QUANT): the payload above is int8, so
        #    asking for int8 keeps it fresh and asking for binary makes it stale. Without
        #    this the flip to binary would leave every mtime-fresh page untouched and the
        #    reader would unpack int8 bytes as packed bits, i.e. score noise.
        assert build.vec_is_fresh(vec, overlay, "old-model", check_model=True,
                                  dim=6, quant="int8")
        assert not build.vec_is_fresh(vec, overlay, "old-model", check_model=True,
                                      dim=6, quant="binary")

        # 8. A chunkless page bakes dim=0 (dimensionless), so it matches any width, and
        #    having no vectors at all, any quantisation.
        empty = build.vec_payload([], [], "old-model", "query: ", "beadbeadbeadbead",
                                  quant="int8")
        vec2 = d / "87654321-vide.vec.json"
        build.write_vec_json(vec2, empty)
        os.utime(overlay, (overlay.stat().st_atime, build.served_mtime(vec2) - 10))
        assert build.vec_is_fresh(vec2, overlay, "old-model", check_model=True, dim=8)
        assert build.vec_is_fresh(vec2, overlay, "old-model", check_model=True,
                                  dim=8, quant="binary")
    print("ok  test_vec_is_fresh_gate")


def test_overlay_iterators_agree():
    # iter_overlay_paths (path-level, shared by the embed sweep) and iter_overlay_raw
    # (content-level, shared by embed-rcp) must agree on the SAME set: the non-empty
    # valid-CIS overlays of both lanes. This is the single "which overlays exist"
    # definition both the runtime and offline embed paths build on.
    #
    # A zero-byte overlay ("scraped, no RCP": drug delisted from BDPM) must be dropped by
    # BOTH. Regression: paths used to enumerate it, so the embed service's reconcile
    # sweep counted it stale on EVERY pass (its .vec.json can never be written) and
    # re-enqueued it forever, showing a permanent "en retard de 351 pages" on /status.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        eu = d / "eu"; eu.mkdir()
        (rcp / "11111111.html").write_text("<div id='textDocument'>a</div>")
        (rcp / "22222222.html").write_text("")            # zero-byte body -> dropped
        (rcp / "notacis.html").write_text("<div>x</div>")  # non-CIS name -> skipped
        (eu / "33333333.html").write_text("<div id='textDocument'>b</div>")
        (eu / "44444444.html.gz").write_bytes(b"")        # zero-byte .gz -> dropped too
        saved = build.OVERLAY_LANES
        build.OVERLAY_LANES = (("rcp", rcp), ("eu", eu))
        try:
            paths = list(build.iter_overlay_paths())
            raws = list(build.iter_overlay_raw())
        finally:
            build.OVERLAY_LANES = saved
        # Only the embeddable overlays are enumerated: no empty sentinel, no non-CIS name.
        assert {c for c, _, _ in paths} == {"11111111", "33333333"}
        # raw agrees exactly, tagged with the right subdir.
        assert {(c, s) for c, _, s in raws} == {("11111111", "rcp"), ("33333333", "eu")}
        assert {c for c, _, _ in raws} == {c for c, _, _ in paths}
    print("ok  test_overlay_iterators_agree")


def test_dist_pages_index_matches_dist_page_for():
    # The reconcile sweep resolves ~15k CIS per pass; dist_page_for globs the directory
    # each time (minutes of CPU per pass), so the sweep uses dist_pages_index (one scan).
    # The two MUST resolve every CIS identically, tie-break included.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "rcp").mkdir()
        for name in ("11111111-aaa.html", "11111111-zzz.html",  # tie -> lowest slug wins
                     "22222222-bbb.html", "notacis-ccc.html"):
            (d / "rcp" / name).write_text("<html></html>")
        (d / "rcp" / "22222222-bbb.html.gz").write_bytes(b"")  # sibling, never a page
        saved = build.DIST
        build.DIST = d
        try:
            index = build.dist_pages_index("rcp")
            assert set(index) == {"11111111", "22222222"}
            for cis in index:
                assert index[cis] == build.dist_page_for(cis, "rcp")
            assert build.dist_pages_index("nolane") == {}
        finally:
            build.DIST = saved
    print("ok  test_dist_pages_index_matches_dist_page_for")


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


def _render_one_rcp(d, raw=None):
    """Render a minimal RCP page (or ``raw``) in a temp DIST, returning (row, html).
    Sets the module globals render_record reads (normally primed by the pool worker)."""
    # This test now lives in src/ alongside rcp.html (a sibling), same dir as build.py.
    tpl = (Path(__file__).parent / "rcp.html").read_text()
    raw = raw or ('<html><body><div id="textDocument">'
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
        html = build.read_served(Path(d) / "rcp" / f"{row['slug']}.html").decode()
    finally:
        (build.DIST, build._TPL, build._NAMES, build._XREF,
         build._SUBSTANCES) = saved
    return row, html


def test_markdown_page_keeps_the_text_and_drops_the_chrome():
    # llm_pages.page_markdown reads the SERVED page, so render one for real: the
    # official sections must come through as headings, the page's own chrome
    # (breadcrumb, Sommaire, reference pills) must not.
    import llm_pages
    with tempfile.TemporaryDirectory() as d:
        row, html = _render_one_rcp(d)
    md = llm_pages.page_markdown(html, f"/rcp/{row['slug']}")
    assert md.startswith("# DOLIPRANE 1000 mg, comprime\n")
    assert "## 1. DENOMINATION DU MEDICAMENT" in md
    assert "## 4.1 Indications therapeutiques" in md
    assert "Traitement symptomatique des douleurs." in md
    assert f"{build.SITE_URL}/rcp/{row['slug']}" in md
    for chrome in ("Sommaire", "En savoir plus", "Accueil", "{{"):
        assert chrome not in md, chrome
    print("ok  test_markdown_page_keeps_the_text_and_drops_the_chrome")


def test_markdown_table_keeps_every_row_and_splits_cell_paragraphs():
    # Unlike build._linearize_table (an embedding chunk, capped at 60 rows), a page
    # read whole must keep the whole table, a title row included.
    import llm_pages
    rows = "".join(f"<tr><td>SOC {i}</td><td><p>Anorexie</p><p>Nausee</p></td></tr>" for i in range(80))
    page = (f'<html><body><main class="rcp" data-cis="12345678"><h1>X</h1><table>'
            f'<tr><td colspan="2"><p>Effets par frequence</p></td></tr>{rows}</table></main></body></html>')
    md = llm_pages.page_markdown(page, "/rcp/12345678-x")
    assert "| Effets par frequence |" in md
    assert "| SOC 79 | Anorexie; Nausee |" in md
    print("ok  test_markdown_table_keeps_every_row_and_splits_cell_paragraphs")


def test_markdown_routes_answer_pages_letters_and_llms_txt():
    import llm_pages
    with tempfile.TemporaryDirectory() as d:
        row, _ = _render_one_rcp(d)
        saved = (build.DIST, build._NAMES)
        build.DIST, build._NAMES = Path(d), {"12345678": "DOLIPRANE 1000 mg, comprime"}
        llm_pages._index_cache = (None, {})
        try:
            code, body, ctype = llm_pages.route(f"/api/md/rcp/{row['slug']}")
            assert (code, ctype) == (200, llm_pages.MARKDOWN) and "## 1. DENOMINATION" in body
            code, body, _ = llm_pages.route("/api/md/browse/d")
            assert code == 200 and f"/md/rcp/{row['slug']})" in body
            assert "- D: " in llm_pages.route("/api/md/browse")[1]
            code, body, ctype = llm_pages.route("/api/llms.txt")
            assert code == 200 and ctype.startswith("text/plain") and "of 1 drugs" in body
            # Every answer, a 404 included, starts by naming the site and its author.
            assert body.startswith(f"# {llm_pages.BYLINE}\n")
            for p in (f"/api/md/rcp/{row['slug']}", "/api/md/browse", "/api/md/browse/d", "/api/md/rcp/nope"):
                assert llm_pages.route(p)[1].startswith(f"{llm_pages.BYLINE}\n\n# "), p
            # Strict paths: a traversal, an unknown letter or slug is a 404, never a read.
            for bad in ("/api/md/rcp/../../etc/passwd", "/api/md/browse/zz",
                        "/api/md/rcp/12345678-nope", "/api/md/browse/q"):
                assert llm_pages.route(bad)[0] == 404, bad
            assert llm_pages.route("/api/status/12345678") is None
        finally:
            build.DIST, build._NAMES = saved
            llm_pages._index_cache = (None, {})
    print("ok  test_markdown_routes_answer_pages_letters_and_llms_txt")


def test_umami_counts_an_ai_read_by_path_and_agent_only():
    import umami
    sent = []

    def opener(request, timeout):
        sent.append((request.full_url, request.get_header("User-agent"), json.loads(request.data)))
        return tempfile.TemporaryFile()  # anything with .close()

    env = {"ANALYTICS_URL": "https://u.example.org/script.js", "ANALYTICS_WEBSITE_ID": "id-1"}
    assert not umami.Tracker(env={}).enabled
    tracker = umami.Tracker(env=env, opener=opener, log=lambda line: sent.append(line))
    ua = "Mozilla/5.0 (compatible; Claude-User/1.0)"
    tracker.track({"Host": "rcp.example.org", "User-Agent": ua}, "/md/rcp/x?q=secret").join()
    url, header_ua, body = sent.pop()
    assert url == "https://u.example.org/api/send" and header_ua == ua
    assert body["payload"] == {"website": "id-1", "hostname": "rcp.example.org", "url": "/md/rcp/x",
                               "userAgent": ua, "browser": "Claude-User"}
    # DNT, a forged Host, an ordinary browser's family
    assert tracker.track({"Host": "rcp.example.org", "DNT": "1"}, "/llms.txt") is None
    assert tracker.track({"Host": "evil/<x>"}, "/llms.txt") is None
    assert umami.agent_of("Mozilla/5.0 (X11; Linux) Firefox/130.0") is None
    # A failed send is logged, and frees its slot.
    tracker._opener = lambda request, timeout: (_ for _ in ()).throw(OSError("down"))
    for _ in range(umami.MAX_IN_FLIGHT + 1):
        tracker.track({"Host": "rcp.example.org"}, "/llms.txt").join()
    assert sent == ["umami failed for /llms.txt: down"] * (umami.MAX_IN_FLIGHT + 1)
    print("ok  test_umami_counts_an_ai_read_by_path_and_agent_only")


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
    # Installable as an app: the manifest, and the touch icon iOS reads instead of it.
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in html
    # The footer points at the sister site.
    assert 'href="https://psychiatheque.olicorne.org"' in html
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
        dim = 6  # served width; must match the vectors' length for the fresh gate
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
            r1 = build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                         quant="int8", stats=info)
            assert r1 == "ok", r1
            assert info.get("chunks", 0) >= 1 and info.get("chars", 0) > 0, info
            info2: dict = {}
            r2 = build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                         quant="int8", stats=info2)
            assert r2 == "fresh" and info2 == {}, (r2, info2)  # skip leaves stats untouched
        finally:
            build.DIST = saved
    print("ok  test_embed_page_to_vec_reports_stats")


def test_fresh_page_bumps_the_br_mtime_so_the_sweep_stops():
    # The reconcile sweep's cheap gate is mtime-only (vec_is_fresh): a page whose
    # overlay was rewritten with IDENTICAL text looks stale, the worker finds the same
    # src_hash and answers "fresh", and must then bump the vector's mtime so the next
    # sweep stops re-queuing it. The vector is stored br-only, and touching its LOGICAL
    # path was a silent no-op, so such a page stayed in the backlog forever (/status
    # showing "en retard de 1 page(s)" indefinitely).
    class _FakeEncoder:
        query_prefix = "query: "
        dim = 6
        def encode_passages(self, texts):
            return [_l2_normalise([0.1] * 6) for _ in texts]

    raw = ("<div id='textDocument'><h1 class='AmmAnnexeTitre1'>4. Indications</h1>"
           "<p>Une phrase de contenu suffisamment longue pour tenir dans un chunk "
           "unique et dépasser le seuil de fusion des petits paragraphes.</p></div>")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "rcp").mkdir()
        page = d / "rcp" / "12345678-drug.html"
        page.write_text("<html></html>")
        overlay = d / "12345678.html"
        overlay.write_text(raw)
        saved = build.DIST
        build.DIST = d
        try:
            enc = _FakeEncoder()
            vec = build.vec_path_for(page)
            assert build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                           quant="int8") == "ok"
            assert not vec.exists() and build.br_path(vec).exists()  # br-only
            # Identical re-crawl: overlay now newer than the vector.
            os.utime(build.br_path(vec), (1_000_000, 1_000_000))
            assert not build.vec_is_fresh(vec, overlay, "m", check_model=False)
            assert build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                           quant="int8") == "fresh"
            assert build.vec_is_fresh(vec, overlay, "m", check_model=False)
        finally:
            build.DIST = saved
    print("ok  test_fresh_page_bumps_the_br_mtime_so_the_sweep_stops")


def test_embed_page_to_vec_re_embeds_on_a_quantisation_change():
    # The producer path end to end (segment -> encode -> gate -> write), with the
    # quantisation changing under it. int8 bytes decoded as packed bits are noise, not a
    # worse answer, so a flip of EMBED_VEC_QUANT MUST re-encode rather than be skipped by
    # the content-hash gate, and the rewritten file must declare what it now holds.
    class _FakeEncoder:
        query_prefix = "query: "
        dim = 16  # divisible by 8, else the binary packer refuses the width
        def encode_passages(self, texts):
            return [_l2_normalise([0.1 * (i - 8) for i in range(16)]) for _ in texts]

    raw = ("<div id='textDocument'><h1 class='AmmAnnexeTitre1'>4. Indications</h1>"
           "<p>Une phrase de contenu suffisamment longue pour tenir dans un chunk "
           "unique et dépasser le seuil de fusion des petits paragraphes.</p></div>")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "rcp").mkdir()
        page = d / "rcp" / "12345678-drug.html"
        page.write_text("<html></html>")
        saved = build.DIST
        build.DIST = d
        try:
            enc = _FakeEncoder()
            vec = build.vec_path_for(page)
            assert build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                           quant="int8") == "ok"
            assert build.read_vec_meta(vec)["quant"] == "int8"
            int8_bytes = len(base64.b64decode(json.loads(
                build.read_served(vec))["chunks"][0]["q"]))
            # Same content, same model, DIFFERENT quantisation -> re-embed, not "fresh".
            r = build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                        quant="binary")
            assert r == "ok", r
            meta = build.read_vec_meta(vec)
            assert meta["quant"] == "binary" and meta["dim"] == 16, meta
            binary_bytes = len(base64.b64decode(json.loads(
                build.read_served(vec))["chunks"][0]["q"]))
            assert int8_bytes == 8 * binary_bytes, (int8_bytes, binary_bytes)
            # And asking for binary again IS fresh, or the sweep would spin forever.
            assert build.embed_page_to_vec("12345678", raw, "rcp", enc, model="m",
                                           quant="binary") == "fresh"
        finally:
            build.DIST = saved
    print("ok  test_embed_page_to_vec_re_embeds_on_a_quantisation_change")


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


def test_archived_page_is_baked_offline_and_served_as_fresh():
    # A delisted drug (zero-byte overlay) still renders its 2022 baseline text, but
    # iter_overlay_paths drops it, so it was never searchable. The offline pre-bake
    # now embeds it from the baseline (iter_archived_raw), and the embed service, which
    # has no CSV to hash, answers "fresh" for it on model/width/quant alone.
    class _FakeEncoder:
        query_prefix = "query: "
        dim = 6

        def encode_passages(self, texts):
            return [_l2_normalise([0.1] * 6) for _ in texts]

    body = ("<div id='textDocument'><h1 class='AmmAnnexeTitre1'>4. Indications</h1>"
            "<p>Une phrase de contenu suffisamment longue pour tenir dans un chunk "
            "unique et dépasser le seuil de fusion des petits paragraphes.</p></div>")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        (d / "dist" / "rcp").mkdir(parents=True)
        (d / "CIS_RCP.csv").write_text(
            f"Code_CIS\tRCP_html\n11111111\t{body}\n22222222\t{body}\n",
            encoding="utf-8")
        (rcp / "22222222.html").write_text("")  # delisted: zero-byte sentinel
        (d / "dist" / "rcp" / "22222222-drug.html").write_text("<html></html>")
        saved = (build.CSV_PATH, build.RCP_OVERLAY_DIR, build.OVERLAY_LANES, build.DIST)
        build.CSV_PATH, build.RCP_OVERLAY_DIR = d / "CIS_RCP.csv", rcp
        build.OVERLAY_LANES, build.DIST = (("rcp", rcp),), d / "dist"
        try:
            es = _load_embed_service()
            es.build = build
            e = es.Embedder.__new__(es.Embedder)
            e.model, e.encoder, e.quant = "m", _FakeEncoder(), "int8"
            assert e.request_page("22222222") == {"status": "archived"}
            archived = list(build.iter_archived_raw())
            assert [(c, s) for c, _raw, s in archived] == [("22222222", "rcp")], archived
            cis, raw, subdir = archived[0]
            assert build.embed_page_to_vec(cis, raw, subdir, e.encoder, model="m",
                                           quant="int8") == "ok"
            assert e.request_page("22222222") == {"status": "fresh"}
            e._running, e._pending, e._lock = None, {}, __import__("threading").Lock()
            assert e.status_page("22222222")["embedded"] is True
            e.model = "other"  # a model swap makes the pushed vectors stale again
            assert e.request_page("22222222") == {"status": "archived"}
        finally:
            (build.CSV_PATH, build.RCP_OVERLAY_DIR, build.OVERLAY_LANES,
             build.DIST) = saved
    print("ok  test_archived_page_is_baked_offline_and_served_as_fresh")


def test_present_cis_matches_iter_rcp_raw():
    # _present_cis (the backlink target set) must agree with what iter_rcp_raw
    # actually renders. It used to DROP a delisted drug (zero-byte overlay) even
    # when its 2022 baseline still renders as an archived page, so no backlink
    # could reach that page.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rcp = d / "rcp"; rcp.mkdir()
        csv_path = d / "CIS_RCP.csv"
        rows = [
            "Code_CIS\tRCP_html",
            "11111111\t<div id=\"textDocument\">baseline one</div>",
            "22222222\t<div id=\"textDocument\">baseline two</div>",  # delisted, archived
            "33333333\t",  # empty baseline + delisted: no page
        ]
        csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        (rcp / "22222222.html").write_text("")
        (rcp / "33333333.html.gz").write_bytes(b"")
        (rcp / "55555555.html").write_text("<div id=\"textDocument\">new</div>")  # overlay-only
        saved = build.CSV_PATH, build.RCP_OVERLAY_DIR, build.PRESENT_CACHE_PATH
        build.CSV_PATH, build.RCP_OVERLAY_DIR = csv_path, rcp
        build.PRESENT_CACHE_PATH = d / ".rcp-present.json"
        try:
            present = build._present_cis()
            rendered = {cis for cis, _raw, _asof in build.iter_rcp_raw(scrape_dates={})}
        finally:
            build.CSV_PATH, build.RCP_OVERLAY_DIR, build.PRESENT_CACHE_PATH = saved
    assert present == rendered == {"11111111", "22222222", "55555555"}, (present, rendered)


def test_load_names_tolerates_stray_directory_and_parses_like_the_catalog():
    # A bad single-file bind mount leaves a DIRECTORY at CIS_bdpm.txt; load_names
    # used an exists() check and crashed with IsADirectoryError. It must return {}
    # and otherwise parse exactly as bdpm.read_catalog does (minus nameless rows).
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        saved = build.BDPM_PATH
        try:
            build.BDPM_PATH = d / "CIS_bdpm.txt"
            build.BDPM_PATH.mkdir()
            assert build.load_names() == {}
            build.BDPM_PATH = d / "real.txt"
            build.BDPM_PATH.write_bytes(
                b"60000001\tDOLIPRANE 500 mg, comprim\xe9\tcomprim\xe9\n60000002\t\tx\n\n"
            )
            assert build.load_names() == {"60000001": "DOLIPRANE 500 mg, comprim\u00e9"}
        finally:
            build.BDPM_PATH = saved


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


def test_shell_key_follows_the_template_not_the_data():
    # deploy.sh --quick ships assets but never pages, and refuses when the VPS's
    # dist/rcp/.shell-key differs from what this tree would render. So the key must
    # move when the template moves (the 2026-10-03 outage: rcp.html gained util.js,
    # --quick shipped JS that needed it to pages without it) and must be stable for an
    # unchanged template, or every --quick would be refused.
    tpl = (build.SRC / "rcp.html").read_text(encoding="utf-8")
    assert build.shell_key(tpl) == build.shell_key(tpl)
    assert build.shell_key(tpl) != build.shell_key(tpl.replace("/util.js", "/other.js"))
    print("ok  test_shell_key_follows_the_template_not_the_data")


def test_eu_figures_become_shared_files_and_orphans_are_pruned():
    # render_eu_page swaps each base64 figure for /eu/img/<hash>: two sibling pages
    # carrying the same figure share ONE file (the whole point: siblings used to
    # store it once each), and a figure no page links any more is deleted.
    import base64
    png = base64.b64encode(b"\x89PNG fake bytes").decode()
    jpg = base64.b64encode(b"\xff\xd8 other bytes").decode()
    with tempfile.TemporaryDirectory() as d:
        old = build.DIST
        build.DIST = Path(d)
        img_dir = build.img_dir("eu")
        try:
            page = f'<img src="data:image/png;base64,{png}" alt=""><img src="data:image/jpeg;base64,{jpg}" alt="">'
            a = build._externalize_images(page, "eu")
            b = build._externalize_images(page, "eu")
            assert a == b and "data:" not in a, a
            files = sorted(f.name for f in img_dir.iterdir())
            assert len(files) == 2 and files[0].endswith(".jpg") and files[1].endswith(".png"), files
            assert (img_dir / files[1]).read_bytes() == b"\x89PNG fake bytes"
            assert 'loading="lazy"' in a
            # Only the PNG is still linked by a page: the JPEG is an orphan.
            build.write_served(img_dir.parent / "1-x.html",
                               f'<img src="/eu/img/{files[1]}" loading="lazy">'.encode())
            # A figure written AFTER the build started is spared even though no page
            # links it yet: the refresh service writes figures before their page.
            # (one second of slack: the filesystem's coarse clock can stamp a file a
            # few ms before time.time()).
            started = __import__("time").time() - 1
            for f in files:
                os.utime(img_dir / f, (started - 10, started - 10))
            fresh = build._externalize_images(
                f'<img src="data:image/png;base64,{base64.b64encode(b"new fig").decode()}">', "eu")
            fresh_name = fresh.split("/eu/img/", 1)[1].split('"', 1)[0]
            assert build._prune_images("eu", since=started) == 1
            assert sorted(f.name for f in img_dir.iterdir()) == sorted([files[1], fresh_name])
            # Reusing an existing figure bumps its date, so it is spared as well.
            os.utime(img_dir / files[1], (started - 10, started - 10))
            build._externalize_images(page, "eu")  # re-creates the pruned JPEG, touches the PNG
            assert (img_dir / files[1]).stat().st_mtime >= started
            # Once the build start is past them, the unlinked figures go.
            assert build._prune_images("eu", since=__import__("time").time() + 10) == 2
            assert [f.name for f in img_dir.iterdir()] == [files[1]]
        finally:
            build.DIST = old
    print("ok  test_eu_figures_become_shared_files_and_orphans_are_pruned")


def test_br_only_pages_are_listed_pruned_and_served_plain():
    # dist/rcp + dist/eu pages and their .vec.json are stored ONLY as .br. Every
    # listing (page_cis_from_dist, dist_pages_index, the prunes) must still see them,
    # a legacy plain page must too, and plain_for (the refresh service's /api/plain/,
    # for a client without brotli) must decompress them and refuse anything else.
    with tempfile.TemporaryDirectory() as d:
        old = build.DIST
        build.DIST = Path(d)
        try:
            rcp = Path(d) / "rcp"
            build.write_served(rcp / "11111111-aaa.html", b"<p>a</p>")
            build.write_served(rcp / "11111111-aaa.vec.json", b'{"x":1}')
            build.write_served(rcp / "22222222-bbb.html", b"<p>b</p>")
            (rcp / "33333333-ccc.html").write_text("<p>legacy</p>")  # older build
            assert sorted(p.name for p in rcp.iterdir()) == [
                "11111111-aaa.html.br", "11111111-aaa.vec.json.br",
                "22222222-bbb.html.br", "33333333-ccc.html"]
            assert build.page_cis_from_dist() == {"11111111", "22222222", "33333333"}
            assert build.dist_page_for("11111111", "rcp") == rcp / "11111111-aaa.html"
            assert set(build.dist_pages_index("rcp")) == {"11111111", "22222222", "33333333"}
            assert build.plain_for("/rcp/11111111-aaa.html.br") == (
                b"<p>a</p>", "text/html; charset=utf-8")
            assert build.plain_for("/rcp/11111111-aaa.vec.json.br") == (
                b'{"x":1}', "application/json")
            for bad in ("/rcp/99999999-none.html.br", "/rcp/../rcp/11111111-aaa.html.br",
                        "/rcp/11111111-aaa.html", "/img/11111111-aaa.html.br",
                        "/rcp/sub/11111111-aaa.html.br"):
                assert build.plain_for(bad) is None, bad
            # Migration: a plain/.gz copy next to a .br goes, a plain file with NO .br
            # (the legacy page above) stays, since it is the only copy.
            (rcp / "22222222-bbb.html").write_text("<p>old plain</p>")
            (rcp / "11111111-aaa.vec.json.gz").write_bytes(b"stale")
            assert build.drop_plain_copies() == 2
            assert (rcp / "33333333-ccc.html").exists()
            assert build.read_served(rcp / "22222222-bbb.html") == b"<p>b</p>"
            # Pruning a dropped slug takes its sidecar with it, whatever form either is in.
            assert build._prune_pages(rcp, {"22222222-bbb"}) == 2
            assert sorted(p.name for p in rcp.iterdir()) == ["22222222-bbb.html.br"]
        finally:
            build.DIST = old
    print("ok  test_br_only_pages_are_listed_pruned_and_served_plain")


def test_changelog_parses_bilingual_bullets_and_shas():
    """A well-formed release note yields ordered categories, both languages and shas."""
    md = """# 1.2.3 - 2026-07-29

## Bug fixes
- Fixed the thing. [abc1234, def5678]
  fr: Correction du truc.

## New features
- Added the other thing.
  fr: Ajout de l'autre truc.
"""
    rel = build.parse_changelog(md, "1.2.3")
    assert rel["version"] == "1.2.3" and rel["date"] == "2026-07-29"
    # Categories come back in the canonical display order, not the file order.
    assert [s["key"] for s in rel["sections"]] == ["features", "fixes"]
    feat = rel["sections"][0]["items"][0]
    assert feat["en"] == "Added the other thing."
    assert feat["fr"] == "Ajout de l'autre truc."
    assert feat["commits"] == []
    fix = rel["sections"][1]["items"][0]
    assert fix["en"] == "Fixed the thing." and fix["commits"] == ["abc1234", "def5678"]
    print("ok  test_changelog_parses_bilingual_bullets_and_shas")


def test_changelog_rejects_malformed_notes():
    """Every authoring mistake must fail the build, not ship a broken popup."""
    good = "# 1.2.3 - 2026-07-29\n## New features\n- Thing.\n  fr: Truc.\n"
    build.parse_changelog(good, "1.2.3")  # sanity: the base case parses
    bad = {
        "version mismatch": ("# 1.2.4 - 2026-07-29\n## New features\n- T.\n  fr: T.\n", "1.2.3"),
        "no date": ("# 1.2.3\n## New features\n- T.\n  fr: T.\n", "1.2.3"),
        "unknown category": ("# 1.2.3 - 2026-07-29\n## Random\n- T.\n  fr: T.\n", "1.2.3"),
        "missing fr line": ("# 1.2.3 - 2026-07-29\n## New features\n- T.\n", "1.2.3"),
        "bullet with no category": ("# 1.2.3 - 2026-07-29\n- T.\n  fr: T.\n", "1.2.3"),
        "empty category": ("# 1.2.3 - 2026-07-29\n## New features\n", "1.2.3"),
        "no title": ("## New features\n- T.\n  fr: T.\n", "1.2.3"),
    }
    for label, (text, version) in bad.items():
        try:
            build.parse_changelog(text, version)
        except ValueError:
            continue
        raise AssertionError(f"malformed note accepted: {label}")
    print("ok  test_changelog_rejects_malformed_notes")


def test_changelog_requires_notes_for_the_current_version():
    """The release gate: bumping __version__ with no notes must stop the build."""
    payload = build.load_changelog(build.__version__)  # the real repo notes
    assert payload["releases"], "no release notes found in docs/changelog"
    assert payload["releases"][0]["version"] == max(
        (r["version"] for r in payload["releases"]), key=build._version_key
    ), "releases must come newest-first"
    versions = [r["version"] for r in payload["releases"]]
    assert build.__version__ in versions, "the current version must have release notes"
    # Sorting is numeric, not lexicographic (0.9.0 < 0.10.0).
    assert build._version_key("0.9.0") < build._version_key("0.10.0")
    # The SERVED payload is French-only (the English bullet stays in the markdown, for
    # developers) and carries no "current": the client keys off window.__APP_VERSION__.
    assert set(payload) == {"commit_url", "categories", "releases"}
    assert all(isinstance(v, str) for v in payload["categories"].values())
    item = payload["releases"][0]["sections"][0]["items"][0]
    assert set(item) == {"fr", "commits"} and item["fr"]
    try:
        build.load_changelog("99.0.0")
    except SystemExit:
        print("ok  test_changelog_requires_notes_for_the_current_version")
        return
    raise AssertionError("a version with no release notes did not fail the build")


def test_query_cache_survives_concurrent_purge_and_inserts():
    """The query LRU is hit by concurrent HTTP threads AND the reconcile purge.

    It used to be a bare OrderedDict: the purge iterated it while request threads
    inserted, which raises "OrderedDict mutated during iteration" (and the purge ran
    outside the reconcile loop's try, so that killed the loop thread)."""
    import threading
    import onnx_embed
    cache = onnx_embed._QueryCache(max_items=64, ttl=0.001)
    errors = []
    stop = threading.Event()

    def writer(seed):
        try:
            i = 0
            while not stop.is_set():
                now = i * 0.0005
                cache.put(bytes([seed, i % 256]), i, now)
                cache.get(bytes([seed, (i - 1) % 256]), now)
                i += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def purger():
        try:
            while not stop.is_set():
                cache.purge(1e9)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads.append(threading.Thread(target=purger))
    for t in threads:
        t.start()
    import time
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(cache) <= 64
    # LRU + TTL semantics.
    c = onnx_embed._QueryCache(max_items=2, ttl=10)
    c.put(b"a", 1, 0)
    c.put(b"b", 2, 0)
    assert c.get(b"a", 1) == 1          # refreshes a's slot
    c.put(b"c", 3, 1)                   # evicts b, the least recently used
    assert c.get(b"b", 1) is None and c.get(b"c", 1) == 3
    assert c.get(b"a", 11) is None      # expired
    assert c.purge(100) == 1 and len(c) == 0
    assert len(onnx_embed._QueryCache(max_items=0, ttl=10)) == 0


def test_failed_render_is_reported_and_previous_page_kept():
    """A render that raises must not silently drop the page.

    render_record used to swallow the exception and return None; main() then left the
    CIS out of the index and _prune_pages deleted its last good page (a 404)."""
    saved = build.clean_rcp

    def boom(*_a, **_k):
        raise ValueError("malformed markup")

    build.clean_rcp = boom
    try:
        row = build.render_record(("12345678", "<html/>", "2022-05-02"))
    finally:
        build.clean_rcp = saved
    assert row["cis"] == "12345678" and "malformed markup" in row["error"], row
    with tempfile.TemporaryDirectory() as d:
        served = Path(d)
        prev = {"12345678": {"h": "abc", "name": "X", "slug": "12345678-x", "asof": "2025-01-01"}}
        assert build._keep_failed("12345678", prev, served) is None  # page gone: nothing to keep
        build.write_served(served / "12345678-x.html", b"<html>ok</html>")
        kept = build._keep_failed("12345678", prev, served)
        assert kept == {**prev["12345678"], "h": ""}, kept  # blank h => retried next build
        assert build._keep_failed("99999999", prev, served) is None
    fails = [{"cis": str(i), "error": "x"} for i in range(11)]
    build._check_render_failures(fails[:10], 100, "RCP")  # under the floor: report only
    try:
        build._check_render_failures(fails, 100, "RCP")
    except SystemExit:
        pass
    else:
        raise AssertionError("too many render failures must abort the build")


def _stub_worker_pills(cis):
    """Spawned-pool probe for the test below (module-level, so it pickles)."""
    return build._ref_links_html(cis, "ZZBRAND", include_ema=True)


def test_stub_workers_receive_the_substance_map():
    """/eu/ stub workers must get the substance map through initargs.

    _init_stub_worker used to prime every global but _SUBSTANCES, which only
    reached the workers by fork inheritance: under spawn/forkserver (Python 3.14's
    Linux default) every /eu/ "En savoir plus" pill searched the brand instead of
    the active substance."""
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(1, initializer=build._init_stub_worker,
                  initargs=({}, {}, {}, set(), {}, "", {}, {"12345678": "SUBSTANCEX"})) as pool:
        # get(timeout): before the fix the initializer raised and the pool
        # respawned workers forever, so a bare apply() would hang.
        html = pool.apply_async(_stub_worker_pills, ("12345678",)).get(timeout=120)
    assert "substancex" in html and "zzbrand" not in html, html
    print("ok  test_stub_workers_receive_the_substance_map")


def test_rcp_images_unreachable_dropped_inlined_served_from_rcp_img():
    # DEPAMIDE opens on the "surveillance supplémentaire" warning whose black-triangle
    # <img> pointed at ANSM's /images/...: on our site that was a broken icon followed
    # by its alt text "BT_1000x858px". An image that is not inlined is now dropped
    # (keeping the warning text that FOLLOWS it in the same paragraph), and an inlined
    # one is served once from /rcp/img/<hash> like the /eu/ figures.
    import base64
    gif = base64.b64encode(b"GIF89a fake").decode()
    raw = ('<html><body><div id="textDocument">'
           '<p class="AmmCorpsTexte"><a name="_Toc1"><img src="/images/R1/image001.png" '
           'alt="BT_1000x858px">Ce médicament fait l’objet d’une surveillance '
           'supplémentaire.</a></p>'
           '<p class="AmmAnnexeTitre1">1. DENOMINATION DU MEDICAMENT</p>'
           f'<p>ZZDRUG 5 mg.<img src="data:image/gif;base64,{gif}" alt="fig"></p>'
           '<p><img src="12_fichiers/image003.png">Texte après.</p>'
           '</div></body></html>')
    with tempfile.TemporaryDirectory() as d:
        _row, html = _render_one_rcp(d, raw)
        files = [f.name for f in (Path(d) / "rcp" / "img").iterdir()]
    assert "BT_1000x858px" not in html and "/images/R1" not in html and "_fichiers" not in html
    assert "Ce médicament fait l’objet d’une surveillance supplémentaire." in html
    assert "Texte après." in html
    assert len(files) == 1 and files[0].endswith(".gif"), files
    assert f'src="/rcp/img/{files[0]}"' in html and "data:image" not in html
    print("ok  test_rcp_images_unreachable_dropped_inlined_served_from_rcp_img")


def test_untitled_rcp_gets_its_numbered_titles_promoted():
    # BETAMETHASONE ARROW (61111556) sets its section titles as bold AmmCorpsTexte
    # paragraphs and another baseline page puts them one level too deep: with no
    # AmmAnnexeTitre1 the Sommaire was empty. The QRD-numbered titles are promoted,
    # but only on such a document, and numbered prose ("40 gouttes ...", a "4.1"
    # sentence ending in a period) stays body text.
    raw = ('<html><body><div id="textDocument">'
           '<p class="AmmCorpsTexte"><b>1. DENOMINATION DU MEDICAMENT</b></p>'
           '<p class="AmmCorpsTexteGras">ZZDRUG 0,05 %, solution buvable</p>'
           '<p class="AmmAnnexeTitre2">4. DONNEES CLINIQUES</p>'
           '<p class="AmmCorpsTexte">4.1. Indications thérapeutiques</p>'
           '<p class="AmmCorpsTexte">40 gouttes correspondent à 1 ml de solution.</p>'
           '<p class="AmmCorpsTexte">4.2. Voir la rubrique précédente.</p>'
           '</div></body></html>')
    _d, _c, toc, _x = build.clean_rcp(raw)
    assert [(t[1], [c[1] for c in t[2]]) for t in toc] == [
        ("1. DENOMINATION DU MEDICAMENT", []),
        ("4. DONNEES CLINIQUES", ["4.1. Indications thérapeutiques"]),
    ], toc
    # A normal page is left alone: its plain "2. ..." paragraph is not a title.
    normal = raw.replace('<p class="AmmCorpsTexte"><b>1.', '<p class="AmmAnnexeTitre1"><b>1.')
    normal = normal.replace('<p class="AmmAnnexeTitre2">4.', '<p class="AmmCorpsTexte">2.')
    _d, _c, toc, _x = build.clean_rcp(normal)
    assert [t[1] for t in toc] == ["1. DENOMINATION DU MEDICAMENT"], toc
    print("ok  test_untitled_rcp_gets_its_numbered_titles_promoted")


def test_every_drug_page_path_fills_every_template_slot():
    """The /rcp/ page, the full /eu/ page and the /eu/ stub each used to chain their
    own ten ``.replace`` calls over rcp.html, so a slot added to the template could be
    filled in one path and forgotten in another. All three now go through
    ``_fill_page``, whose keywords must cover exactly the template's slots, and none
    of the three leaves a slot behind."""
    import inspect
    import re
    tpl = (Path(__file__).parent / "rcp.html").read_text()
    slot_re = re.compile(r"\{\{([A-Z_]+)\}\}")
    params = {p.upper().replace("HEAD_EXTRA", "HEADEXTRA")
              for p in inspect.signature(build._fill_page).parameters if p != "tpl"}
    assert set(slot_re.findall(tpl)) == params, (set(slot_re.findall(tpl)), params)
    overlay = ('<!DOCTYPE html>\n<html>\n<body><div id="textDocument" '
               'data-ema-date="2025-01-02" data-ema-fetched="2026-01-02" '
               'data-ema-pdf="https://www.ema.europa.eu/x_fr.pdf">'
               '<p class="AmmAnnexeTitre1" id="sec-1">1. DENOMINATION DU MEDICAMENT</p>'
               '<p>ZZDRUG 5 mg, comprime pellicule.</p></div></body>\n</html>')
    meta = ("ZZDRUG 5 mg, comprime", "EU/1/00/000/001", "ZZ Pharma")
    with tempfile.TemporaryDirectory() as d:
        _row, rcp_html = _render_one_rcp(d)
        saved = build.DIST
        build.DIST = Path(d)
        try:
            row = build.render_eu_page("12345678", overlay, meta, tpl)
            full_html = build.read_served(Path(d) / "eu" / f"{row['slug']}.html").decode()
            build.render_eu_stub("87654321", meta[0], "<p>stub</p>", tpl)
            stub_html = build.read_served(
                Path(d) / "eu" / f"87654321-{build.slugify(meta[0])}.html").decode()
        finally:
            build.DIST = saved
    for kind, html in (("rcp", rcp_html), ("eu full", full_html), ("eu stub", stub_html)):
        assert not slot_re.search(html), (kind, slot_re.findall(html))
    assert "ZZDRUG 5 mg, comprime pellicule." in full_html
    assert '<meta name="robots" content="noindex">' in stub_html
    assert '<meta name="robots" content="noindex">' not in full_html
    print("ok  test_every_drug_page_path_fills_every_template_slot")


def _load_embed_service():
    """Import embed-service.py by path (its '-' name is not importable)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "embed_service", Path(build.__file__).parent / "embed-service.py")
    es = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(es)
    return es


def test_embed_popup_routes_are_bounded_and_cached():
    """The public /status popup routes segment + tokenise whole pages on every call.

    They used to run unbounded and uncached, so a few clients polling them could pin
    every core of the embed container. Now at most _DETAIL_SLOTS run at once (the
    rest get 503 immediately), a chunk preview is cached per content hash and a
    backlog answer is reused for _BACKLOG_TTL seconds."""
    import threading
    es = _load_embed_service()
    e = es.Embedder.__new__(es.Embedder)  # no model: only what the routes touch
    e._lock = threading.Lock()
    e._chunks_cache, e._backlog_cache = es.OrderedDict(), None
    e.detail_slots = threading.BoundedSemaphore(es._DETAIL_SLOTS)
    e._overlay_for = lambda cis: (SAMPLE, "rcp")
    e._token_counts = lambda texts: [len(t.split()) for t in texts]
    calls = []
    saved = es.build.section_chunks
    es.build.section_chunks = lambda raw, cis: calls.append(cis) or saved(raw, cis)
    try:
        first = e.chunks_detail("60000001")
        assert first["chunks"] and e.chunks_detail("60000001") is first
        assert calls == ["60000001"], calls  # second call served from the cache
    finally:
        es.build.section_chunks = saved
    scans = []
    e._backlog_detail_uncached = lambda: scans.append(1) or {"items": []}
    e.backlog_detail()
    e.backlog_detail()
    assert len(scans) == 1, scans

    h = es._Handler.__new__(es._Handler)
    sent = []
    h._send = lambda code, payload: sent.append(code)
    saved_embedder, es.EMBEDDER = es.EMBEDDER, e
    try:
        for _ in range(es._DETAIL_SLOTS):
            assert e.detail_slots.acquire(blocking=False)
        h.path = "/api/sem/backlog"
        h.do_GET()
        h.path = "/api/sem/chunks/60000001"
        h.do_GET()
        assert sent == [503, 503], sent
        for _ in range(es._DETAIL_SLOTS):
            e.detail_slots.release()
        h.do_GET()
        assert sent[-1] == 200, sent
        # The slot was released after answering (and after a raise, via finally).
        e.chunks_detail = lambda cis: 1 / 0
        try:
            h.do_GET()
        except ZeroDivisionError:
            pass
        for _ in range(es._DETAIL_SLOTS):
            assert e.detail_slots.acquire(blocking=False), "a slot leaked"
    finally:
        es.EMBEDDER = saved_embedder
    print("ok  test_embed_popup_routes_are_bounded_and_cached")


def test_embed_request_during_embed_is_rerun_not_dropped():
    """A page asked for again WHILE it is being embedded must embed again after.

    The running embed may have read the overlay just before the refresh service
    wrote a new one; ``_enqueue`` used to answer "dup" and drop the request, so the
    page kept vectors of the old text until the next reconcile sweep."""
    import threading
    es = _load_embed_service()
    e = es.Embedder(None, "m", backlog=False, backlog_rate=0.0, reconcile_seconds=60,
                    queue_max=10, refresh_url="", timeout=1.0, min_chars=1, max_chars=10)
    calls = []

    class _Stop(BaseException):
        pass

    def fake_embed(cis, info=None):
        calls.append(cis)
        if len(calls) == 1:  # a fresh overlay lands mid-embed: refresh pings us
            assert e._enqueue(cis, "crawl", front=True) == "queued"
            assert e._enqueue(cis, "sweep", front=False) == "dup"  # sweep: backstop only
            return "fresh"
        raise _Stop

    e._embed_page = fake_embed
    e._enqueue("60000001", "crawl", front=True)

    def run():
        try:
            e._worker()
        except _Stop:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert calls == ["60000001", "60000001"], calls
    print("ok  test_embed_request_during_embed_is_rerun_not_dropped")


def test_web_manifest_icons_ship():
    """The manifest parses, and every icon it names is a PNG of its declared size that
    build.py copies into dist (a missing one silently makes the site uninstallable)."""
    manifest = json.loads((build.SRC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    build_src = (build.SRC / "build.py").read_text(encoding="utf-8")
    icons = [(icon["src"].lstrip("/"), int(icon["sizes"].split("x")[0]))
             for icon in manifest["icons"]] + [("apple-touch-icon.png", 180)]
    assert {size for _, size in icons} >= {192, 512}
    for name, size in icons:
        png = (build.SRC / name).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n", name
        assert int.from_bytes(png[16:20], "big") == size, name
        assert f'"{name}"' in build_src, f"{name} is not copied into dist by build.py"
    print("ok  test_web_manifest_icons_ship")


def test_passage_batches_are_bounded_by_attention_cost():
    """A batch of very long rows is split so its attention memory stays bounded (the
    embed container was OOM-killed on a page of ~3300-char table rows, restarted onto
    the same page, and embedded nothing), while ordinary rows keep full batches."""
    import onnx_embed
    b = onnx_embed._batch_bounds
    assert b(["a" * 500] * 20, 8) == [(0, 8), (8, 16), (16, 20)]
    # each 3300-char row is over budget with a neighbour: one per batch
    assert b(["a" * 3300] * 3 + ["a" * 100], 8) == [(0, 1), (1, 2), (2, 3), (3, 4)]
    # the budget uses the batch's LONGEST row, not the new one alone
    assert b(["a" * 100] * 7 + ["a" * 2300] * 3, 8) == [(0, 7), (7, 10)]
    assert b([], 8) == []
    for bounds in (b(["a" * n for n in range(0, 5000, 37)], 8),):
        assert all(stop - start <= 8 for start, stop in bounds)
        assert [x for s, e in bounds for x in range(s, e)] == list(range(136))
    print("ok passage batches bounded by attention cost")


def test_older_chunk_format_stays_served_and_upgrades_last():
    """A chunk-format bump must not leave the catalog unsearchable for the weeks the VPS
    needs to re-embed it: vectors of the same text under a _CHUNK_FORMAT_LEGACY format
    are still served ("legacy"), and re-embedded only once nothing lacks vectors, in a
    random order weighted by prescription frequency, unmarketed drugs last-ish, pages a
    reader searched first."""
    import random
    import threading
    from collections import OrderedDict, deque
    raw = "<div id='textDocument'><p>bonjour</p></div>"
    old = build._CHUNK_FORMAT_LEGACY[0]
    cfg = ("m", 4, "binary")
    meta = {"src_hash": build.raw_hash(raw, old), "model": "m", "dim": 4, "quant": "binary"}
    assert build.vec_is_legacy(meta, raw, *cfg)
    assert not build.vec_is_current(meta, build.raw_hash(raw), *cfg)
    assert not build.vec_is_legacy(dict(meta, model="other"), raw, *cfg)   # model swap: no
    assert not build.vec_is_legacy(dict(meta, src_hash=build.raw_hash(raw)), raw, *cfg)
    assert not build.vec_is_legacy(dict(meta, src_hash=build.raw_hash(raw + "x", old)),
                                   raw, *cfg)                              # text changed: no

    es = _load_embed_service()
    e = es.Embedder.__new__(es.Embedder)
    e._lock, e._wake = threading.Lock(), threading.Event()
    e._queue, e._pending, e._rerun = deque(), {}, {}
    e._running, e.queue_max, e._failures = None, 500, OrderedDict()
    e._upgrade, e._upgrade_seen = [], set()
    # weighted order: a top-prescribed marketed drug vs a rare unmarketed one
    saved = (es.bdpm.read_catalog, es.bdpm.frequency_scores, es.bdpm.read_marketed)
    es.bdpm.read_catalog = lambda path: [("A", "a"), ("B", "b")]
    es.bdpm.frequency_scores = lambda *a: ({"A": 10.0, "B": 0.0}, set(), {})
    es.bdpm.read_marketed = lambda path: {"A"}
    try:
        random.seed(0)
        firsts = sum(e._upgrade_order(["B", "A"])[0] == "A" for _ in range(2000))
    finally:
        es.bdpm.read_catalog, es.bdpm.frequency_scores, es.bdpm.read_marketed = saved
    assert 1900 < firsts < 2000, firsts   # usually first, still random
    # feeding: nothing while the queue holds work; then the searched page first, in batches
    e._upgrade = ["P1", "P2", "P3", "P4"]
    e._queue.append("X")
    assert e._feed_upgrades() == 0 and e._upgrade == ["P1", "P2", "P3", "P4"]
    e._queue.clear()
    e._upgrade_seen.add("P3")
    assert e._feed_upgrades() == es._UPGRADE_BATCH == 2
    assert list(e._queue) == ["P3", "P1"] and e._upgrade == ["P2", "P4"]
    print("ok  test_older_chunk_format_stays_served_and_upgrades_last")


def test_crash_guard_counts_restarts_and_quarantines_a_killer_page():
    """The VPS embed container was OOM-killed on one page 344 times: each restart reset
    every in-memory counter, /status saw a young healthy service ("à jour", since a
    missing scan figure read as 0) and the same page came back first. _CrashGuard
    records boots and the in-flight page in a file, so a page that killed the process
    twice is skipped until its content changes, and /status sees the restarts."""
    import tempfile
    es = _load_embed_service()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".embed-state.json"
        g = es._CrashGuard(path)
        assert g.restarts_since(3600) == 0 and not g.quarantine_list()
        g.begin("64587553", "h1")              # ... and the kernel kills us here
        g = es._CrashGuard(path)               # restart 1
        assert not g.quarantined("64587553", "h1")
        g.begin("64587553", "h1")
        g = es._CrashGuard(path)               # restart 2: that's the page
        assert g.quarantined("64587553", "h1") and g.quarantine_list() == ["64587553"]
        assert not g.quarantined("64587553", "h2")   # re-scraped: a fresh chance
        assert g.restarts_since(3600) == 2
        g.begin("60000001", "x"); g.end()      # a page that finishes leaves no trace
        g = es._CrashGuard(path)
        assert "60000001" not in g.crashed
        g.begin("66544554", "h3")              # docker stop / a deploy lands mid-page
        es.EMBEDDER = type("E", (), {"crash": g})()
        real_exit, es.os._exit = es.os._exit, lambda code: None
        try:
            es._clean_stop(15, None)
        finally:
            es.os._exit, es.EMBEDDER = real_exit, None
        assert "66544554" not in es._CrashGuard(path).crashed   # not counted as a crash
        path.write_text("{not json")           # corrupt state never stops the service
        assert es._CrashGuard(path).restarts_since(3600) == 0
    # before the first scan of a boot, the summary says "unknown", not "0 behind"
    import threading
    e = es.Embedder.__new__(es.Embedder)
    e._lock = threading.Lock()
    e._last_scan, e.crash, e._running, e._running_since = {}, None, None, 0.0
    e.backlog, e.backlog_rate, e._started = True, 0.0, 0.0
    e._upgrade = ["66544554"]  # one page still served from an older chunk format
    e.stats = lambda: {"enabled": True, "model": "m", "dim": 4, "quant": "binary",
                       "embedded": 0, "skipped": 0, "errors": 0, "chars": 0,
                       "mean_chars_per_s": 0, "embed_seconds": 0.0, "queries": 0,
                       "queries_shed": 0, "crawl_triggered": 0, "queue": 500,
                       "pending": 501, "running": None}
    summary = e.public_summary()
    assert summary["crawl_gap"] == {"scan_pending": True}, summary
    assert summary["health"]["restarts_24h"] == 0
    assert summary["format_upgrade"] == {"remaining": 1,
                                         "version": build._CHUNK_FORMAT_VERSION}, summary
    print("ok  test_crash_guard_counts_restarts_and_quarantines_a_killer_page")


if __name__ == "__main__":
    test_load_cap_meta_excludes_decentralised()
    test_clean_substance_strips_salt_hydrate()
    test_ref_links_include_crat_pill()
    test_quantize_roundtrip()
    test_section_chunks_align_with_toc()
    test_heading_only_section_yields_no_chunk()
    test_heading_path_context_prefix()
    test_unstyled_subheads_promoted()
    test_prose_tagged_as_heading_is_demoted()
    test_eu_toc_keeps_ema_ids_and_nests_subheads()
    test_merge_small_chunks()
    test_merged_chunk_snippet_spans_its_paragraph_run()
    test_flat_table_snippet_resolves_to_its_rows()
    test_linearised_row_snippet_resolves_to_its_row()
    test_chunk_locations_land_on_their_blocks()
    test_vec_add_locations_backfills_without_reencoding()
    test_eu_page_without_heading_ids_gets_the_chunks_anchors()
    test_dsfr_backtotop_chrome_stripped()
    test_filler_paragraphs_dropped()
    test_empty_and_untitled()
    test_demojibake_restores_lost_apostrophes()
    test_nested_toc_keeps_sec_namespace_stable()
    test_table_rows_stay_intact()
    test_layout_table_falls_back_flat()
    test_eu_overlay_preserves_existing_ids()
    test_vec_payload_roundtrip()
    test_quantize_binary_packs_signs_msb_first()
    test_vec_payload_binary_is_eight_times_narrower_and_ranks_the_same()
    test_write_read_vec_meta_roundtrip()
    test_raw_hash_is_the_staleness_key()
    test_vec_is_fresh_gate()
    test_overlay_iterators_agree()
    test_dist_pages_index_matches_dist_page_for()
    test_iter_overlay_raw_honors_caller_order()
    test_sentence_chunks_group_and_fallbacks()
    test_long_section_tail_survives_raised_cap()
    test_duplicate_chunks_within_page_dropped()
    test_sitemap_lists_indexable_pages()
    test_robots_points_at_sitemap()
    test_rcp_page_has_canonical_and_no_leftover_slots()
    test_markdown_page_keeps_the_text_and_drops_the_chrome()
    test_markdown_table_keeps_every_row_and_splits_cell_paragraphs()
    test_markdown_routes_answer_pages_letters_and_llms_txt()
    test_umami_counts_an_ai_read_by_path_and_agent_only()
    test_jsonld_escapes_script_breakout_and_website_searchaction()
    test_embed_page_to_vec_reports_stats()
    test_fresh_page_bumps_the_br_mtime_so_the_sweep_stops()
    test_embed_page_to_vec_re_embeds_on_a_quantisation_change()
    test_rcp_archived_detects_zero_byte_overlay()
    test_iter_rcp_raw_serves_delisted_baseline()
    test_archived_page_is_baked_offline_and_served_as_fresh()
    test_present_cis_matches_iter_rcp_raw()
    test_load_names_tolerates_stray_directory_and_parses_like_the_catalog()
    test_record_hash_changes_when_archived()
    test_shell_key_follows_the_template_not_the_data()
    test_eu_figures_become_shared_files_and_orphans_are_pruned()
    test_br_only_pages_are_listed_pruned_and_served_plain()
    test_changelog_parses_bilingual_bullets_and_shas()
    test_changelog_rejects_malformed_notes()
    test_changelog_requires_notes_for_the_current_version()
    test_query_cache_survives_concurrent_purge_and_inserts()
    test_failed_render_is_reported_and_previous_page_kept()
    test_stub_workers_receive_the_substance_map()
    test_every_drug_page_path_fills_every_template_slot()
    test_rcp_images_unreachable_dropped_inlined_served_from_rcp_img()
    test_untitled_rcp_gets_its_numbered_titles_promoted()
    test_embed_popup_routes_are_bounded_and_cached()
    test_embed_request_during_embed_is_rerun_not_dropped()
    test_web_manifest_icons_ship()
    test_passage_batches_are_bounded_by_attention_cost()
    test_crash_guard_counts_restarts_and_quarantines_a_killer_page()
    test_older_chunk_format_stays_served_and_upgrades_last()
    print("\nAll tests passed.")

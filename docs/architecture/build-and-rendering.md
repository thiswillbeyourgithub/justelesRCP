# Build pipeline and RCP rendering

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

- **Names come from `CIS_bdpm.txt`**, falling back to the `AmmDenomination`
  parsed from the RCP HTML when the mapping is missing. See `load_names()`.
- **Cross-drug backlinks link one RCP to another.** `build_xref_index()` builds,
  once per build, a map `term -> (cis, slug, display)` of the single canonical
  page to link a given drug/substance name to; `_linkify()` then wraps mentions
  of those terms in each cleaned RCP body with `<a class="drug-xref">` and emits
  a "Médicaments liés" `<details>` (`_xref_html`) into the `{{XREF}}` slot. Terms
  come from each drug's brand root plus, for **mono-substance drugs only**, its
  active-substance tokens; the target per term is picked by prescription
  frequency (mono preferred), reusing the SAME scoring as the scrape queue. That
  scoring + tokenising now lives in a shared, pure-stdlib **`bdpm.py`** imported
  by BOTH `build.py` and `scrape-rcp.py` (build.py can't cheaply import
  scrape-rcp.py, which needs httpx/loguru/click; bdpm.py has no third-party
  deps). Matching is deliberately conservative on medical text: whole-word,
  accent-folded via a **length-preserving** fold (so match offsets map back onto
  the original text), `>= _XREF_MIN_LEN` chars, capped at `_XREF_MAX_LINKS`/page,
  each term once, never self-linking, and **gated on the frequency list of real
  drug/substance names** (the primary false-positive guard: it stops descriptive
  words baked into substance denominations, e.g. STAMARIL's "virus de la fièvre
  jaune", from linkifying "fièvre") plus an `_XREF_STOP` salt/dosage-form
  stoplist. It is ALSO restricted to link **only CIS that actually render a
  page**: ~15% of CIS have an empty RCP and are pageless, so a target drawn from
  the full name catalog can 404 (e.g. HELICOBACTER's only carriers are pageless
  breath-test diagnostics, and NORVIR/ritonavir is EMA-centrally-authorized with
  an empty ANSM RCP). `build_xref_index(names, page_cis)` rejects any target not
  in `page_cis`; a term whose best-scored carrier is pageless falls back to its
  best carrier that has a page, or drops out. The full build computes `page_cis`
  via `_present_cis()` (the baseline CSV presence set, cached in
  `dist/.rcp-present.json` keyed by the frozen CSV's size+mtime so it is not
  reparsed every build, then adjusted by overlays exactly as `records()` resolves
  them); the refresh service, which has no CSV, uses `page_cis_from_dist()` (glob
  of the already-built `dist/rcp` pages, `served_pages`). The whole index is folded into the
  incremental-build `_global_key`
  (a page's links depend on the WHOLE index, not just its own inputs), so a
  changed dictionary busts the cache but an unchanged rebuild still reuses
  everything. Keep the contract in sync across `build_xref_index`/`_linkify`/
  `_xref_html`, the `{{XREF}}` slot in `src/rcp.html`, and `.drug-xref` /
  `.drug-xref-list` in `style.css`. The refresh service builds the same index at
  startup so a refreshed page keeps its backlinks (see the runtime bullet).
- **The ANSM HTML keeps its `Amm*` CSS class hooks** (e.g. `AmmAnnexeTitre1`,
  `AmmDenomination`). `clean_rcp()` strips decoration (BackToTop images,
  inline `font-*` styles, scripts) but preserves those classes; `style.css`
  restyles them. If you change class names in one place, change both.
- **The frozen 2022 baseline CSV has Windows-1252-as-Latin-1 mojibake, repaired
  at render time.** Its punctuation (apostrophes `’`, quotes, dashes, ellipsis)
  was stored as the raw cp1252 byte mis-decoded into the matching *invisible* C1
  control (e.g. the apostrophe `0x92` became U+0092), so `d’élimination` displayed
  as `d élimination` (the apostrophe silently vanished; ~1.8M occurrences across
  `dist`). `_demojibake()` (via a `_C1_DEMOJIBAKE` translate map of the whole
  U+0080..U+009F range, which is never legitimate here, back to its cp1252 char) is
  applied in `_parse_clean()`, the single point BOTH the rendered page and the
  semantic-search chunks are parsed from, so the reader and the embeddings get the
  same repaired text. It is a **pure render-time fix (no re-scrape, just a
  rebuild)** and a **no-op on overlays** (fresh ANSM/EMA scrapes are clean UTF-8:
  re-scraping a drug is the ONLY way to fix the rarer residual defects where the
  original export used a genuine space instead of an apostrophe, which no
  codepoint map can recover). Covered by `test_demojibake_restores_lost_apostrophes`.

- **ANSM HTML quirks handled at render time** (all in `_parse_clean` or `style.css`,
  so a rebuild fixes them, no re-scrape): (1) Word bookmarks wrap whole paragraphs
  in `<a name="_Toc...">`, so `a:not([href])` resets to `color: inherit` (it used to
  paint ~12.6k pages' text in the link colour) and the `AmmAnnexeTitre1/2` accent is
  set explicitly; (2) a page with NO `AmmAnnexeTitre1` (a few baseline pages set
  their titles as plain or bold paragraphs, or one level too deep) gets its
  QRD-numbered titles promoted by `_promote_numbered_titles` ("N. ALL CAPS" ->
  Titre1, "N.N. Title" without a closing period -> Titre2, never in a table/list),
  else its Sommaire is empty (test: `test_untitled_rcp_gets_its_numbered_titles_promoted`);
  (3) the few raw Word `<h1>`-`<h6>` in a body are kept at text size. Literal QRD
  placeholders (`<...>` text) found on a handful of pages are SOURCE content, left as is. Added by Claude Code.
- **Every page has an `<h1>` drug/presentation-name header** at the top, emitted
  from the shared template's `{{TITLE}}` slot (filled with the drug name by
  `render_record`, `render_eu_page` and `render_eu_stub` alike, `.rcp-title` in
  `style.css`). So `_stub_content`/`_eu_full_content` must NOT emit their own `<h1>`
  (it would duplicate the heading); the ANSM body's `AmmDenomination` is separate.
- **The build is incremental** (`main()` in `build.py`). It no longer wipes
  `dist/`; instead `dist/.build-manifest.json` maps each CIS to a hash of its
  inputs (raw HTML + mapped name). A record whose hash is unchanged and whose
  output files still exist is reused (no parse, no compress); stale pages
  (renamed slugs, dropped CIS) are pruned by slug set. A page whose render RAISES (`render_record`/`_render_stub` return `{cis, error}` via `_render_guarded`) is reported and keeps its previous output + manifest record with a blank hash (`_keep_failed`, retried next build) instead of falling out of the slug set and being pruned to a 404; past 1% failures (`_check_render_failures`) the build aborts before pruning or writing the manifest. A `_global_key` (hash of
  `build.py`'s source MINUS the `__version__` line, plus the RCP template) busts
  the whole cache when build logic or the template changes, so any real code
  edit forces a full rebuild while a version-only bump does not. Both render
  stages fan out over a `multiprocessing.Pool` behind a `%`/ETA `tqdm` bar: the
  RCP loop (`render_record`, primed by `_init_worker`) and, since v0.36.2, the
  `/eu/` loop (`_render_stub`, primed per worker by `_init_stub_worker`), which
  was the serial long pole once a batch of EMA overlays lands (each full
  converted SmPC page is brotli-heavy). A from-scratch full rebuild (all RCP
  pages plus ~2.3k full converted `/eu/` pages) is ~12 min on ~11 workers (RCP
  ~3.5 min, `/eu/` ~9 min); an unchanged-data rebuild reuses everything in ~35 s.
- **RCP freshness is an overlay, not a live server.** The bulk `data/CIS_RCP.csv`
  is a frozen 2 May 2022 snapshot (the only bulk RCP *HTML* dump in existence;
  the official BDPM download is daily-fresh but metadata-only). `scrape-rcp.py`
  (a `uv` PEP 723 script) runs as a background/cron job, fetches drug pages from
  the live ANSM site (`/medicament/<cis>/extrait`, which the old
  `affichageDoc.php?specid=…&typedoc=R` now redirects to), extracts the RCP body
  from `#tabpanel-rcp-panel #contenu` (scoped because `id="contenu"` is not
  unique: the notice panel has one too), strips DSFR chrome (`fr-no-print`
  toolbars, buttons), and re-wraps it in the exact `<div id="textDocument">…`
  envelope the 2022 dump used. It writes one overlay file per drug, gzipped as
  `data/rcp/<cis>.html.gz` by default or plain `data/rcp/<cis>.html` with
  `--no-gzip` (env `RCP_OVERLAY_GZIP`); it keeps only one format per CIS and
  `build.py` reads either transparently (`_overlay_path`/`_read_overlay`, newest
  wins if both coexist), so the flag only trades disk/rsync size for
  greppability. `records()` prefers the overlay over the baseline CSV cell (a
  *zero-byte* overlay means "scraped, no RCP": the drug was delisted from BDPM. If
  the frozen 2022 baseline cell still has the RCP, `iter_rcp_raw` KEEPS serving that
  text as an ARCHIVED page rather than dropping it to a 404, see the archived bullet
  below; only when the baseline is empty too is the page truly skipped).
  On a page with no RCP body (an EMA-centrally-authorized drug), `extract_ema_pdf`
  also harvests the real EMA `product-information` `_fr.pdf` href the ANSM page
  links to and stores it in the manifest as `ema_pdf`; `build_stubs` uses it to
  point the `/eu/` stub straight at the document (see the EU-stub bullet). Nothing
  is fetched from the EMA: the href is read off the already-fetched ANSM page.
  Ordering is frequency-first: `--frequency` (default `data/drugs_frequency.jsonl`)
  is a JSONL of `{term, score}` (drug/substance name -> priority) matched to each
  CIS's accent-folded token pool: its denomination plus, when the optional
  `CIS_COMPO_bdpm.txt`/`CIS_GENER_bdpm.txt` joins are present, its active-substance
  and generic-group tokens (so a brand can match a substance or reference-brand
  term, e.g. XENAZINE -> tétrabénazine). A CIS no term matches gets the
  25th-percentile score. Combined with a `--ttl-days` (default 30) skip window, the static
  architecture is preserved:
  nothing dynamic runs at serve time. `data/.scrape-manifest.json` holds per-CIS
  `last_fetch`/hash for the TTL. `last_fetch` is the last SUCCESSFUL fetch: a failure goes through `scrape.store_entry`/`record_error`, which merges `{status: error, error, last_error}` into the previous entry (keeping its `last_fetch`/`hash`/`ema_pdf`) and is retried `ERROR_RETRY_SECONDS` (6h) after `last_error` (`due_at`, shared by `is_due` and the refresh crawler's idle wait), so an error neither fakes freshness nor gets re-fetched on every rotation. Keep the extraction envelope in sync with
  `clean_rcp`'s `div#textDocument` lookup if either changes.

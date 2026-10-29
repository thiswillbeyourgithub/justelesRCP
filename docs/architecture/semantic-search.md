# Per-drug semantic search

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

- **Per-drug semantic search runs SERVER-SIDE, on crawled data only, optional**
  (`onnx_embed.py` + `embed-service.py` + `src/rcp-semsearch.js`; `embed-rcp.py` +
  `scripts/download-model.sh` are the offline pre-bake). Each RCP / full `/eu/` page has a
  "Recherche sémantique dans ce RCP" box (the summary is worded to signal it is a
  MEANING-based search, and a "?" badge `.semsearch-help` in the summary toggles a
  one-line `.semsearch-help-text` explaining to phrase a question, not keywords; its
  click `preventDefault`s so it toggles the help, not the `<details>`): the reader
  types a natural-language question and it
  ranks/scroll-highlights the closest SECTIONS of that one drug, with a prev/next
  navigator. **The query is embedded by a warm server container, NOT the browser**:
  the old design downloaded a ~120 Mo model per visitor (transformers.js + ONNX +
  wasm under `/vendor` + `/models`), which was the feature's worst wart and forced a
  `'wasm-unsafe-eval'` CSP relaxation. Now the browser downloads nothing; the trade
  is that the query text transits our same-origin server (never logged, dropped right
  after encoding: the warm encoder's query LRU is keyed by a BLAKE2 hash of the text,
  not the text itself, so only a hash -> lossy vector is ever retained, and even that
  is purged after `EMBED_QUERY_CACHE_TTL_SECONDS` (default 60s: lazily on access +
  swept by the reconcile loop) so query-derived data does not linger, read-only
  `cap_drop: ALL` container). **Segmentation is shared**:
  `build.section_chunks(raw, cis)` runs the SAME `clean_rcp` path as the rendered
  page, so its chunks carry the same `sec-N` ids the ToC/anchors use (a hit scrolls
  to a heading that exists); it returns `(sec_id, snippet, chunk_text)` per chunk
  (`_sentence_chunks` groups WHOLE sentences up to `_SEC_CHUNK_CHARS` ~500 chars, so a
  chunk ends on a sentence boundary, not mid-sentence; an oversized sentence falls back
  to word-windows and a single oversized token to a between-letters split, so every
  chunk stays `<= ~_SEC_CHUNK_CHARS`) and is the single source of truth. The per-page
  `_SEC_MAX_CHUNKS` is a pure failsafe (10000, ~5 Mo of one page's text), NOT a normal
  limit: the old 160 cap silently DROPPED the tail of long drugs (quetiapine LP spent
  it all in early sections, so its fatty-meal pharmacokinetics passage was never
  embedded and unfindable). It handles BOTH page kinds: ANSM raw
  carries no ids so it assigns `sec-N` exactly as `clean_rcp`/`_build_toc` does, while
  a converted `/eu/` overlay ALREADY carries (QRD-numbered, non-sequential) `sec-N`
  ids from `ema_pdf` that `render_eu_page` keeps verbatim, so it must NOT renumber
  them (it only calls `_build_toc` when the headings have no id); a `<table>` is
  linearised row-by-row (`_linearize_table`: each body row -> "header: cell; header:
  cell", kept intact) instead of `text_content()`-flattened, so posology/table rows
  stay retrievable; non-data/layout tables fall back to flat text. The section HEADING
  PATH IS embedded, as a CONTEXT PREFIX (v6 reversed the earlier "heading not embedded"
  design): each `chunk_text` is `"<top heading> > <subheading> > ...\n<body>"`
  (`_heading_context` builds the path from the `AmmAnnexeTitre1` top heading plus the
  numbered `AmmAnnexeTitre2`/3/4 subheadings the passage sits under, `_titre_level`
  reading the level off the class), so every vector carries its section's topic even
  when the sentence never names it (a passage under "4.6 Grossesse" embeds with that
  heading). The prefix is ONLY in the embedded `chunk_text`; the stored `snippet` stays
  the body ALONE, so the client's `locate()` still matches it against the rendered DOM
  paragraph and the reader sees the passage, not the path. Chunks are anchored to the
  TOP-level `sec-N` (a subheading only shapes the prefix, never the anchor), and a
  heading-only section still yields no chunk (a bare path is not content). Tiny chunks
  are MERGED into a neighbour (`_merge_small`, below `_SEC_MERGE_MIN_CHARS`, bounded by
  `_SEC_MERGE_CEIL_CHARS`) rather than stranded, so a stray enumeration item or a short
  sentence tail never becomes its own thin vector (the reader prefers a longer paragraph
  with context); table rows stay atomic. Dedup is on the BODY text, so identical prose
  repeated on a page (EMA per-presentation notices) embeds once. DSFR back-to-top links
  + their "Redirection vers le haut de page" tooltip
  spans (fresh-scrape chrome) are stripped in `_STRIP_XPATH` so they never reach a
  chunk; and filler narrative paragraphs (`_is_filler_paragraph`: exactly "Sans objet",
  the "[à compléter ultérieurement par le titulaire]" QRD placeholder, `<= 5` chars, or
  `<= 2` words) are dropped before the section body is assembled (table rows are exempt). `_CHUNK_FORMAT_VERSION` (folded into `raw_hash`/`src_hash`) busts
  every `.vec.json` when this segmentation changes, since the raw overlay is untouched. **A format bump does NOT blank search while the catalog re-embeds.** The outgoing version goes into `_CHUNK_FORMAT_LEGACY`: `build.vec_is_legacy` (same overlay text hashed under a legacy format via `raw_hash(raw, fmt)`, same model/width/quant) makes the embed service keep SERVING those vectors (`_vec_state` -> `legacy`, `request_page` answers `fresh`), while the sweep re-embeds them LAST: the boot's first reconcile pass collects them into `_upgrade`, ordered by `_upgrade_order` (random, weighted by the scrape queue's prescription-frequency score via `bdpm.frequency_scores`, a not-marketed drug (`bdpm.read_marketed`, CIS_bdpm column 6) at `_UNMARKETED_WEIGHT`), and `_feed_upgrades` hands out `_UPGRADE_BATCH` at a time only when the queue is empty and every page lacking usable vectors has embedded or failed; a legacy page a reader searched (`_upgrade_seen`) goes first. That ordering needs the BDPM files, now mounted read-only into the embed container too (absent, the order is uniform random). /status shows the remaining count (`format_upgrade` in the summary, reason `format` in the backlog popup). Drop a version from `_CHUNK_FORMAT_LEGACY` only when its chunks are bad enough to be worth blanking the search for. Test: `test_older_chunk_format_stays_served_and_upgrades_last`. Added by Claude Code.
  **The warm encoder** (`onnx_embed.Encoder`) loads the int8
  `jinaai/jina-embeddings-v5-text-small-retrieval` ONNX weights (`onnx/model_int8.onnx`,
  quantised LOCALLY by `scripts/quantise-model.py` because Jina publishes fp32 only) +
  tokenizer ONCE with `onnxruntime` + `tokenizers` ONLY (NO torch: a ~300 Mo image, not
  ~2 Go; the weights are ~570 Mo, mounted read-only), and is shared by the query path and
  the background page path (`session.run` is thread-safe). A per-model recipe
  (`onnx_embed._profile`, keyed on `RUNTIME_MODEL`) picks the ONNX file, pooling, prefixes
  and MRL width: jina-embeddings-v5 = **LAST-token pool -> L2, `Query: ` on queries and
  `Document: ` on passages (arctic prefixed the query only, so BOTH sides moved),
  Matryoshka-truncated to 1024 dims, its full width** (truncate THEN normalise once), verified
  against the repo config + ONNX graph (inputs `input_ids`/`attention_mask` only, output
  `token_embeddings`). The MRL width is the profile default but is **env-configurable via
  `EMBED_OUT_DIM`** (`Encoder(out_dim=...)`, `--out-dim` on embed-service.py + embed-rcp.py;
  unset = the profile width, 1024, `0`=full model width, which for this model 1024 also is): the served width is baked into each `.vec.json` and
  gated alongside the model, so a change to EITHER `RUNTIME_MODEL` OR `EMBED_OUT_DIM`
  re-embeds the whole catalog (`read_vec_meta`/`embed_page_to_vec`/`vec_is_fresh` compare
  both, keeping the reader's query vectors and the stored passage vectors at one width; a
  stored `dim` of 0 = a chunkless page, so it stays fresh at any width). Swapping
  `RUNTIME_MODEL` also needs the matching `scripts/download-model.sh` fetch. The prior model
  was e5-small (mean-pool, `query:`/`passage:` prefixes, 384 dims). Keep the `EMBED_OUT_DIM`
  env in sync across `Encoder`/`_profile` (onnx_embed.py), `read_vec_meta`/`vec_is_fresh`/
  `embed_page_to_vec` (build.py), the `--out-dim` option + `_is_embedded`/`_scan_and_enqueue`
  (embed-service.py), `--out-dim` (embed-rcp.py), and `docker/env.example` +
  `docker/docker-compose.yml`. `EMBED_OUT_DIM` is the PASSAGE width and the DEFAULT query
  width; it is not a cap on the query width. `POST /api/sem/embed` takes an optional `dim`
  (`Encoder.encode_query(q, dim=...)` -> `Encoder.encode(..., width=...)`) and truncates the
  query vector to it, which is what lets the sibling justelesrecos bake its index at another
  width against this one shared encoder without re-embedding this catalog. It is free: the
  query cache stores the FULL-width vector and truncates per request, exact because
  truncating an L2-normalised vector and renormalising equals truncating the raw one and
  normalising. Out of range or not an integer is a 400, deliberately, since a width mismatch
  gives meaningless cosines rather than merely worse ones. Watch the shadowing that broke the
  first version: `Encoder.encode`'s batch loop has a local for the padded token length
  (`seq_len`), and when it was also called `width` every vector came back as wide as the
  batch had tokens; `src/onnx_embed.py`'s `__main__` self-test now asserts the widths. `build.quantize_int8`
  (symmetric `q=round(v*127)`, dequant `q/127`) is the ONE canonical formula, mirrored
  in JS `decodeVec`.

  **The stored PASSAGE quantisation is a second knob, `EMBED_VEC_QUANT`** (`build.VEC_QUANTS`,
  `--vec-quant` on embed-service.py + embed-rcp.py; default `binary`, the single
  `build.DEFAULT_VEC_QUANT` every default reads). `int8` is one byte per
  dimension; `binary` (`build.quantize_binary`) is one BIT per dimension, its sign, MSB-first,
  so a vector is 8x narrower and `dim` in the payload becomes the LOGICAL width rather than
  the byte count. The query stays int8 whatever the passages are: scoring is asymmetric, a
  full-precision query dotted with the unpacked +-1/sqrt(dim) passage, which is a unit vector,
  so the result is still a cosine and nothing downstream (the dot loop, `semFloor`, the
  percentage badge) needs a special case. It is baked into each `.vec.json` and gated exactly
  like the width, everywhere the width is: a change re-embeds the catalog, because bytes
  decoded under the wrong scheme are noise rather than a worse answer. A file with NO `quant`
  key predates the option and reads back as `int8` (`read_vec_meta`), so old and new sidecars
  can be served side by side while the catalog re-embeds.

  Why it exists: given a byte budget, buy WIDTH rather than precision. 1024-dim binary is 128
  bytes a vector against 256 for 256-dim int8. Measured in the sibling justelesrecos with
  `scripts/evaluate.py --within-document`, which masks everything outside the gold document
  and so reproduces THIS project's regime (search inside one drug's RCP), 117 queries, SE
  +-0.045: 1024-binary page@1 **0.675**, 1024-int8 0.675, 256-int8 **0.641**, 256-binary
  0.556. So 1024-binary ranks as well as 256-int8 at half the bytes. Binary only collapses
  where the corpus is large and the runner-up close: the same measurement run cross-corpus
  over 27k chunks gives 256-binary 0.282 against 0.419 for 1024-binary. What it buys on the
  wire, measured by baking two real pages (274 sections) both ways: the served `.vec.json`
  drops 33% plain and 37% gzipped (144 KB -> 97 KB, 79 KB -> 50 KB gzipped). Less than the
  50% the vectors alone would suggest, because a sidecar is vectors AND snippets. Two ordering rules
  when flipping it: deploy the site FIRST (an older `rcp-semsearch.js` cannot decode
  `binary`), and pre-bake locally (`embed-rcp.py --vec-quant binary`, then
  `deploy.sh --push-vectors`) unless you want the VPS re-embedding ~15k pages at CPU speed
  while serving. The reader's percentage badge reads LOWER under binary (dropping magnitudes
  shrinks the cosine by about sqrt(2/pi)); ranking is unaffected, but `EMBED_SEM_FLOOR` needs
  re-measuring if it was ever raised above its 0.0 default. Keep the env in sync across
  `VEC_QUANTS`/`quantize_binary`/`vec_payload`/`read_vec_meta`/`vec_is_fresh`/
  `embed_page_to_vec` (build.py), `--vec-quant` + `Embedder.quant`/`_is_embedded`/
  `_scan_and_enqueue`/`stats` (embed-service.py), `--vec-quant` (embed-rcp.py), `decodeVec` +
  `loadIndex` (rcp-semsearch.js), and `docker/env.example` + `docker/docker-compose.yml`. **The embed service** (`embed-service.py`, behind Caddy's
  same-origin `/api/sem/*`, mirrors `refresh-service.py`: `ThreadingHTTPServer`,
  `_lock`, a priority queue, ONE background worker) embeds the query on request
  (`POST /api/sem/embed` `{q, dim?}` -> `{q: base64-int8, dim}`, on a request thread so it never
  waits behind the worker; bounded LRU; `>= EMBED_MIN_QUERY_CHARS` / `<=
  EMBED_MAX_QUERY_CHARS` else 400; a `BoundedSemaphore` of `EMBED_MAX_CONCURRENT_QUERIES`
  bounds concurrent encodes so a query flood can't pin every core, shedding past it with
  503; query CONTENT never logged) and (re-)embeds each
  **crawled** page in the background (`POST /api/sem/page/<cis>` embeds now, front of
  queue; a reader/crawler request for a page MID-embed is re-run once that embed ends
  (`_rerun`), never dropped as a duplicate; `GET /api/sem/page/<cis>` -> `{embedded, pending}`; `/api/sem/health` never
  logged; `/api/sem/stats`). **Crawled-only + content-hash staleness**: it embeds ONLY
  overlay pages (`build.iter_overlay_raw`, `data/rcp` + `data/eu`, NEVER the frozen
  2022 baseline CSV) via the SINGLE `build.embed_page_to_vec` core (segment -> encode
  -> gate -> write), and re-embeds a page ONLY when the `src_hash` baked into its
  `.vec.json` (`build.raw_hash` of the overlay) or the model changed (`build.read_vec_meta`),
  so a no-op refresh that rewrote identical bytes re-embeds nothing and there is NO
  manifest. A search on a never-crawled (baseline-only) page returns `crawling`: the
  service asks the refresh service (`REFRESH_TRIGGER_URL`) to crawl it first, then
  embeds it when the overlay lands (driven by the refresh service's `EMBED_NOTIFY_URL`
  ping, with a periodic reconcile scan as backstop). A DELISTED drug (`_scraped_empty`: an overlay
  file exists but is zero-byte, so the page is the 2022 archive copy, ~460 pages) is
  embedded OFFLINE only: the embed container has no baseline CSV, so `embed-rcp.py`
  (`--archived`, default on) bakes them from `build.iter_archived_raw` and `deploy.sh
  --push-vectors` ships them. The service answers `fresh` for such a page when its pushed
  `.vec.json` passes `build.archive_vec_is_current` (model/width/quant only, no
  `src_hash` check since the 2022 text is frozen and the service cannot hash it;
  `Embedder._archive_embedded`), else `archived`; without that `archived` branch the
  reader polled a crawl that could only re-confirm the deletion, until the poll budget
  ran out. The reconcile sweep never sees these pages, so it never re-embeds or deletes
  them; after a model/width/quant change they must be re-baked locally and pushed again. **A zero-byte overlay is dropped by
  `build.iter_overlay_paths` itself** (the shared "which overlays exist" enumeration),
  so it is neither embedded nor counted: it used to be enumerated, and since its
  `.vec.json` can never be written, the reconcile sweep re-enqueued all ~351 of them on
  EVERY pass and `/status` showed a permanent "en retard de 351 page(s)" that no amount
  of embedding could clear. The sweep resolves each CIS to its rendered page through
  `build.dist_pages_index(subdir)` (ONE directory scan per lane), NOT `dist_page_for`
  (which globs per CIS: ~10 minutes of CPU per pass over the full catalog, which is what
  made the sweep look stalled); keep `dist_page_for` for single lookups, the two agree
  by construction (same lowest-slug tie-break). The reconcile scan's cheap enqueue
  gate is `build.vec_is_fresh` (stat-only mtime), but its FIRST pass runs with
  `check_model=True` so a MODEL swap (which leaves each already-embedded `.vec.json`
  newer than its unchanged overlay, hiding the mismatch from a pure mtime gate) is
  re-embedded across the whole catalog on restart; the authoritative gate is
  `build.vec_is_current` (src_hash + model + width + quant), shared by
  `embed_page_to_vec`, `embed-rcp.py` and the service. When that gate says "fresh" but
  the mtime gate did not (an overlay re-fetched with identical bytes, or vectors pushed
  by `deploy.sh --push-vectors`, which keeps the LOCAL mtime), `embed_page_to_vec` bumps
  the vector's date with `build.touch_served`, which touches the `.br` (a plain
  `os.utime` on the logical path silently missed the br-only file, so such a page was
  re-queued on every sweep forever: the "backlog=1 that never clears" bug). It writes the served
  `dist/rcp/<slug>.vec.json` / `dist/eu/<slug>.vec.json` (`{model, dim, query_prefix,
  src_hash, chunks:[{sec, snippet, q}]}`) DIRECTLY via `build.write_vec_json`, in a
  SEPARATE sidecar (NOT inline in the page, NOT in `_record_hash`/the template), so
  page HTML is unchanged and the feature is refresh-safe. `build.py` no longer bakes
  or mirrors anything for it (no `data/emb`, no `vendor`/`models`); `main()` only
  prunes orphan `.vec.json` when a slug is dropped. **The runtime**
  `src/rcp-semsearch.js` gates on `.rcp[data-cis]` (skips `.rcp-stub`); on first open
  it `POST`s `/api/sem/page/<cis>`, polls `GET` until `embedded`, then fetches the
  `.vec.json`. It does NOT search per keystroke (each query encode is a server
  round-trip, heavier since the arctic swap): EDITING the query hides the previous
  results (`onQueryEdited` -> `clearHits`) and prompts for a deliberate search; the
  reader runs it with the **"Rechercher" button** (`.semsearch-go`) or **Enter**, with a
  long-pause auto-search (`AUTO_SEARCH_MS`, ~1.2 s) only as a fallback for someone who
  never clicks. Enter steps to the next hit when the shown results still match the text,
  else it searches. A search (>= min chars, `AbortController` cancels the superseded
  embed) `POST`s the query, dequantises + ranks locally with a HYBRID score:
  the semantic cosine PLUS a client-side lexical bonus (`lexicalScore`/`termCredit`, a
  fuzzy word match of the query's terms, accent-folded, stopwords dropped, against each
  section's own words: exact/prefix-inflection/small-edit-distance credit, mean over the
  query terms x `KEYWORD_BOOST`), so a passage that both reads close AND literally mentions
  the query floats up. The candidate gate is the RAW cosine `>= semFloor` (a `continue`,
  not a `break`, since the list is sorted by the hybrid score), capped at `MAX_RESULTS`
  (25) distinct passages; a wholly-irrelevant query (nothing clears the floor) still
  yields "Aucun passage pertinent." (only when the floor is above 0). **The floor is
  server config, not a baked constant**: the embed service reads `EMBED_SEM_FLOOR` (env,
  default **0.0**) and returns it as `floor` in every `/api/sem/embed` response;
  `rcp-semsearch.js` applies it (`SEM_FLOOR_DEFAULT`/`semFloor`, defaulting to 0.0 if the
  service omits it). 0.0 keeps EVERY section (only the hybrid rank + `MAX_RESULTS` cap
  prune, so a query always surfaces its closest passages); raise it (e.g. 0.5, "at least
  50% similarity") to drop weak matches. Keep the `EMBED_SEM_FLOOR` env in sync across
  `Embedder.sem_floor`/`embed_query`/the `--sem-floor` option (embed-service.py), the
  `floor` field consumed in `src/rcp-semsearch.js`, and `docker/env.example` +
  `docker/docker-compose.yml`. **A hit resolves to a RUN of DOM blocks, not one
  paragraph.** One embedded chunk routinely covers several sibling `<p>`s (`_merge_small`
  folds short paragraphs into their neighbour, and `_sentence_chunks` groups sentences
  across the whole section body), so `locate(sec, snippet)` anchors on the block holding
  the snippet's first 40 chars and then WALKS FORWARD consuming the snippet block by
  block (a block wholly inside it is consumed and the walk continues; the block the
  TRUNCATED `_SEC_SNIPPET_CHARS` tail lands in ends the run; `MAX_RUN_BLOCKS` bounds it),
  returning `[el, ...]` (a snippet whose head spans table cells is matched against the joined cells by `locateAcross` and resolves to their `<tr>`s; a linearised "header: value; ..." table-row chunk is matched by `locateRow`, which rebuilds each rendered row the way `_linearize_table` does, and keeps its snippet as display text; only a chunk neither finds falls back to `[heading]`). That walk
  only works because build.py joins a chunk body with SINGLE spaces (`_norm_ws(" ".join
  (...))`); `test_merged_chunk_snippet_spans_its_paragraph_run` pins both halves. Resolving
  to ONE block was a real bug: on VERATRAN, "Fraction de liaison aux protéines" ranked #1
  yet displayed as the 42-char paragraph merged in front of it ("Le volume de distribution
  est de 3,5 l/kg."), hiding the answer. So `hits[]` carries `els` (an array, `els[0]` is
  the scroll anchor); `displayText` joins EVERY block of the run, `renderHits` tints them
  all with `.semsearch-hit` but puts `data-semrank` on `els[0]` ALONE (hence the
  `.semsearch-hit[data-semrank]::after` badge selector in `style.css`: without the
  attribute gate the other blocks of a run would each print a bare "#"), `setCurrent`
  marks the whole run `.semsearch-current`, and the `seen` dedup rejects a chunk whose run
  OVERLAPS an already-claimed block, so every element belongs to exactly one hit. Each
  surviving hit shows that full passage (a table-row/heading-resolved hit keeps the stored
  snippet) plus its
  blended relevance small + muted as a percentage (`.semsearch-score`, clamped to 100%,
  tooltip `Score hybride ...` naming BOTH the semantic proximity and the keyword match).
  Consecutive hits from the SAME section print the heading once (`.semsearch-cont`
  continuations get a dashed divider), and a trailing `.semsearch-capped` note shows when
  the `MAX_RESULTS` cap bites; editing the query scrolls the results list back to the top.
  Then it LIGHTLY tints every
  surviving ranked passage (`.semsearch-hit`) but does NOT auto-jump: `current` stays
  `-1` and the reader picks a passage (click a result, or step the nav bar / Enter),
  which promotes it to `.semsearch-current` and scrolls to it (`setCurrent(i, true)` is
  the ONLY scroll path; `rank()` never scrolls). Clicking a result ALSO collapses the
  whole `.semsearch` box first (so the hit in the drug text is unobstructed and the
  smooth scroll lands on a settled layout); prev/next call `setCurrent` directly and
  keep the box open (on wide screens a click keeps it open too, it sits in the right
  margin). Under the field, `PRESETS` (four example questions: food and bioavailability,
  injection sites, children's dosing, pregnancy) render as `.semsearch-preset` buttons in
  `.semsearch-presets`; a tap fills the field and searches, and they hide while results
  are shown (`renderHits`/`clearHits`). Added by Claude Code. **Selecting a hit makes it shareable**: `setCurrent` ends with
  `shareHit`, which `history.replaceState`s the URL to a browser-native text fragment
  `#<sec-N>:~:text=<first words>,<last words>` (`FRAGMENT_WORDS` per side, a short lone
  paragraph quoted whole, words split on ASCII whitespace only so the no-break spaces of
  French text still match), so a copied link scrolls to AND highlights the passage with no
  script on the recipient's side; a browser ignoring the directive still jumps to the
  `sec-N` heading. `replaceState`, never `pushState` (stepping hits must not fill Back).
  Known limit: a hit inside a collapsed `/eu/` annex group relies on the browser
  auto-expanding `<details>` (Chrome does), and JS cannot read the directive back
  (`location.hash` hides it). It is injected right AFTER the ToC (so
  the reading column reads pills -> Sommaire -> search) as the last panel of the sticky
  dock (see the ToC bullet in [drug-page.md](drug-page.md): `src/dock.js` owns every offset), so a reader can search a
  long RCP without scrolling back up, and its results list scrolls internally so the
  stuck panel never outgrows the screen. **Opening it is tuned for a
  phone**, where the box is typically tapped half-way down the screen and the panel would
  otherwise sit in the bottom sliver: (1) `scrollBoxToTop()` scrolls the page by the box's
  distance to its resting place (`jlrcpDock.restTop`, not hardcoded), so the
  box settles at the top of the usable area and the screen below it becomes reading room
  (a no-op when it is already stuck); (2) `fitResults()` replaces the static
  `max-height: 60dvh` CSS cap with the space ACTUALLY left under the panel's chrome, via
  CSSOM (CSP-safe), measured from that resting place (not the live rect, which is still
  moving during the open scroll) and off `visualViewport` (which is what shrinks when the
  mobile keyboard opens); it re-runs on `resize`/`visualViewport resize`/`jlrcp-dock` and at the end of
  `renderHits`/`clearHits` (the nav row entering/leaving changes the chrome height); (3)
  the field is FOCUSED on open, but ONLY when `hits` is empty: with results on screen the
  reader most likely reopened the box to re-read them, and a focus would throw the keyboard
  over half of them. The focus rides on a `summary` CLICK handler that drives the open
  itself (`preventDefault` + `box.open = true`) because `<details>` fires `toggle`
  asynchronously, i.e. outside the tap's user-gesture window, and mobile browsers only
  raise the keyboard for a focus made inside it. All three stand down while
  `window.__TOUR_ACTIVE__` (the tour does its own scrolling and keeps keyboards down on
  purpose). Both the search box and the ToC use the native
  `<details>` disclosure triangle as their collapse affordance (`list-style-position:
  inside`), kept consistent on purpose. If the embed service is absent, `/api/sem/*` 502s
  and the box degrades to "indisponible". **The offline pre-bake** `embed-rcp.py` (optional; warms
  the backlog before a first deploy) reuses the SAME `onnx_embed.Encoder` and
  `build.embed_page_to_vec` over `build.iter_overlay_raw()`, writing the SAME
  `.vec.json` with the SAME `src_hash`, so offline and online vectors never disagree.
  `onnx_embed.RUNTIME_MODEL` (server) and the model `scripts/download-model.sh` fetches MUST
  stay the same weights (query and passage vectors must match). Keep the contract in
  sync across `section_chunks`/`_heading_context`/`_titre_level`/`_merge_small`/
  `quantize_int8`/`raw_hash`/`iter_overlay_paths`/
  `iter_overlay_raw`/`dist_page_for`/`dist_pages_index`/`read_vec_meta`/`vec_is_fresh`/`vec_payload`/
  `write_vec_json`/`vec_is_current`/`embed_page_to_vec` (+ the shared `OVERLAY_LANES`/`CIS_RE`)
  (build.py), `onnx_embed.py`, `embed-service.py`, `embed-rcp.py`,
  `src/rcp-semsearch.js`, the `<script>` in `src/rcp.html`, `.semsearch*` in
  `style.css`, `scripts/download-model.sh`, the `/api/sem/*` route + strict CSP in
  `docker/Caddyfile`, and the `embed` service in `docker/docker-compose.yml` +
  `docker/embed.Dockerfile`. `test_embed.py` covers the pure pieces (int8 round-trip
  bound, `section_chunks` `sec-N` alignment, `vec_payload`/`read_vec_meta` round-trip,
  `raw_hash` staleness key).

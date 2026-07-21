# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

justelesRCP is a fast, ad-free static site serving the **RCP** (résumés des
caractéristiques du produit) of medicines sold in France, sourced from the ANSM
BDPM public dataset. It exists to be a lightweight alternative to slow, for-profit
sites like vidal.fr. The whole thing is precomputed to static files; the only
runtime code is two optional companion services (see the architecture note below): a
refresh service that re-scrapes a single drug on demand behind a rate limit, and an
embed service that powers per-drug semantic search (embeds the reader's query and the
crawled pages, server-side). Leave them out and the site is 100% static.

**Language convention:** the website (page text, UI strings) is in French; the
code, comments, and developer docs are in English. `README.md` is the French
readme and `README.en.md` is its English translation. They are cross-linked and
MUST be kept in sync: whenever you edit one, update the other accordingly.

**Page parity (RCP vs EMA):** an `/eu/` (EMA) full page IS an RCP, just sourced
at the EMA instead of the ANSM. The two page types MUST stay almost identical:
same freshness banner, same "Ouvrir la source officielle" source button, same
"En savoir plus" pill row, same ToC, same semantic search, same refresh control.
So when a change is requested WITHOUT naming the page type, apply it to BOTH by
default (`render_record` for `/rcp/` AND `render_eu_page` for full `/eu/`). Only
diverge where the data genuinely forces it (e.g. the EMA source button targets
the direct PDF, and a bare `/eu/` stub has no freshness card). If a requested
change truly should hit only one type, the request will say so explicitly.

**Versioning:** the project version is `__version__` in `build.py` (single
source of truth, printed at build start). There are no git tags; bump it
patch/minor per change. The version is NOT baked into page HTML: `build.py`
writes `dist/app-version.js` (`window.__APP_VERSION__`) and `src/app-init.js`
injects it into every `[data-app-version]` slot (RCP sidebar, About page, home
and browse footers). This keeps page content independent of the version so a
version-only bump does not invalidate the incremental-build cache (below).

## Architecture (the important part)

**Repository layout:** ALL Python lives in `src/` (the same directory as the
frontend templates/assets it renders), NOT the repo root: `src/build.py`,
`src/bdpm.py`, `src/ema_pdf.py`, `src/onnx_embed.py`, `src/scrape-rcp.py`,
`src/scrape-ema.py`, `src/refresh-service.py`, `src/embed-service.py`,
`src/embed-rcp.py`, plus the tests `src/test_embed.py` / `src/test_ema_seed.py`.
They stay siblings so `import bdpm` and the importlib-by-path service imports keep
resolving; each anchors the repo root as `Path(__file__).resolve().parent.parent`
(so `data/`, `src/`, `dist/`, `models/` hang off it). Invoke them as `uv run
src/<script>.py`. The refresh/embed Docker images COPY these into `/app/src/` so the
container mirrors the repo (same `parent.parent` anchoring; data/dist mounted at
`/app/...`). The committed shell helpers (`download-data.sh`, `download-model.sh`) live
under `scripts/` (invoke them as `./scripts/download-*.sh`, they anchor the repo root via
`cd "$(dirname "$0")/.."`); the two gitignored helpers (`reset-scrape-data.sh` and
`deploy.sh`) stay at the repo root. Note: elsewhere in
this file the scripts are still referred to by their bare name (`build.py`, etc.);
they all live under `src/`. When editing, keep this in sync with `docker/*.Dockerfile`
(the `COPY src/*.py ./src/` lines + `CMD ["python", "src/<svc>.py", ...]`) and
`deploy.sh`'s `BAKED_SOURCES`.

Two stages, cleanly separated:

1. **Build** (`build.py`, a `uv` PEP 723 script): reads the source data from
   `./data`, cleans each RCP's ANSM HTML, and writes a fully static site to
   `./dist`. Run with `uv run src/build.py`. This is where all the work happens.
2. **Serve** (`docker compose up`): Caddy serves `./dist` read-only. No dynamic
   code, no database.

Data flow:

```
data/CIS_RCP.csv    (TSV: Code_CIS <TAB> RCP_html, CSV-quoted multi-line HTML)
data/CIS_bdpm.txt   (official CIS -> drug name, latin-1, tab-separated)
data/rcp/<cis>.html[.gz]   ANSM re-scrape overlay (scrape-rcp.py, wins over CSV)
data/eu/<cis>.html[.gz]    EMA SmPC converted from the PDF (scrape-ema.py + ema_pdf.py;
                           wraps the same <div id="textDocument"> envelope)
      | build.py
      v
dist/rcp/<cis>-<slug>.html   one cleaned page per drug (slug from drug name),
                             with a sidebar table of contents (ToC) of sections
dist/search-index.json       [{cis,name,slug[,sub]}] consumed by client-side search
                             (sub = active-substance/DCI string, present when known and
                             not already in the name, so search matches by substance too)
                             (+ {cis,name,slug,eu:1} rows for /eu/ pages below)
dist/eu/<cis>-<slug>.html    EU-authorization page for a centrally-authorized drug
                             whose RCP lives at the EMA (empty ANSM cell): a full
                             converted SmPC/notice if data/eu has an overlay (INDEXABLE,
                             in the sitemap), else a lightweight stub pointing to the
                             EMA (noindex). Findable via search only, NOT in browse
dist/browse/index.html       A-Z landing (letter grid with counts)
dist/browse/<letter>.html    alphabetical drug list per letter ('#' -> num.html)
dist/sitemap.xml robots.txt  SEO: sitemap of every crawlable URL (home, /a-propos,
                             browse, every /rcp/ + full /eu/) + robots.txt pointing at
                             it. Every page also bakes a canonical link + Open Graph +
                             JSON-LD (see the SEO bullet). Origin = SITE_URL constant
dist/index.html a-propos.html style.css search.js
dist/logo.svg               site logo (SVG favicon on every page + README header)
dist/og.png                 raster social card for og:image / twitter:image (1200x630)
dist/app-config.js app-init.js dev-banner.js toc.js app-version.js rcp-semsearch.js  (runtime client assets)
dist/rcp/<cis>-<slug>.vec.json   OPTIONAL per-page section vectors for server-side
dist/eu/<cis>-<slug>.vec.json    per-drug semantic search, written DIRECTLY by
                             embed-service.py (runtime) or embed-rcp.py (offline
                             pre-bake) for CRAWLED pages only; build.py only prunes
                             orphans. Absent unless one of them has run
dist/.build-manifest.json    incremental-build cache (per-CIS input hashes)
+ .gz and .br precompressed siblings for every text file (Caddy serves these)
```

Docker lives under `docker/` (compose, Caddyfile, entrypoint.sh, env.example).
Run compose with `-f docker/docker-compose.yml`; paths inside are relative to it
(`../dist` is the web root).

Key facts that aren't obvious from a single file:

- **Browse pages are server-navigable (SEO), search is client-side.** The A-Z
  browse pages under `/browse/` are plain static links (crawlable, no JS), built
  by `write_browse()` in `build.py`. Names are bucketed by accent-folded first
  letter; non-alpha names go under `#` (`/browse/num`).
- **Search is 100% client-side.** `src/search.js` fetches `search-index.json`
  (~15k entries) once and does substring matching in the browser, against BOTH the
  brand name AND the active-substance/DCI string (the optional `sub` field, from the
  same cleaned `CIS_COMPO` map `load_substances` builds), so a search on the substance
  (e.g. "acétylcystéine" -> HIDONAC) surfaces every brand carrying it; a name hit ranks
  above a substance-only hit, and each result shows its DCI under the name
  (`.result-sub`). There is no search API. Full-text search over RCP *content* is
  intentionally NOT supported (that was the tradeoff for a zero-runtime static
  architecture). Keep the `sub` contract in sync across the search-index enrichment
  (build.py), `search.js`, and `.result-sub`/`.result-name` in `style.css`.
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
  of the already-built `dist/rcp/*.html`). The whole index is folded into the
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
- **Each RCP page has a sidebar table of contents.** `_build_toc()` walks the
  headings down to `_TOC_DEPTH` (default 2: top-level `AmmAnnexeTitre1` plus the
  numbered `AmmAnnexeTitre2` subsections like "4.1 Indications", "4.2 Posologie")
  and returns a NESTED `(id, title, children)` tree; `render_record()` emits a
  `<details class="toc">` of jump links (plus the version slot) into `{{TOC}}` via
  the shared, recursive `_toc_html`/`_toc_ol_html`. It renders in the reading column
  (inside `main`, after `{{ASOF}}`, above the search box) as a sticky collapsible
  `<details>` block on ALL viewports: there is no longer a desktop left sidebar
  (`.rcp-layout` is a single centered column now; see `.rcp-layout`/`.toc` in
  `style.css`, whose `.toc nav ol ol` indents the nested level). CRITICAL id
  contract: **only top-level headings get the 0-based `sec-N` ids**; deeper
  headings get a SEPARATE `sub-N` namespace, so the `sec-N` anchors that
  `section_chunks` / the semantic-search `.vec.json` share NEVER shift when
  `_TOC_DEPTH` changes (already-embedded vectors stay aligned). `_TOC_DEPTH` is a
  purely cosmetic knob: bumping it re-renders pages on the next build but needs NO
  re-scrape (all four ANSM heading levels are already in the stored HTML);
  `section_chunks` deliberately calls `_build_toc(inner, depth=1)` so it is
  independent of the ToC depth. The SAME `_toc_html` renders the full `/eu/`
  pages, fed by `_eu_toc()` (annexe group > SmPC section, same nested shape). Keep
  the contract in sync across `_build_toc`/`_toc_html`/`_toc_ol_html`/`_eu_toc`/
  `_TOC_DEPTH` (build.py), the `{{TOC}}` slot in `src/rcp.html`, `src/toc.js`, and
  `.toc` in `style.css`.
- **Every page has an `<h1>` drug/presentation-name header** at the top, emitted
  from the shared template's `{{TITLE}}` slot (filled with the drug name by
  `render_record`, `render_eu_page` and the stub branch alike, `.rcp-title` in
  `style.css`). So `_stub_content`/`_eu_full_content` must NOT emit their own `<h1>`
  (it would duplicate the heading); the ANSM body's `AmmDenomination` is separate.
- **The build is incremental** (`main()` in `build.py`). It no longer wipes
  `dist/`; instead `dist/.build-manifest.json` maps each CIS to a hash of its
  inputs (raw HTML + mapped name). A record whose hash is unchanged and whose
  output files still exist is reused (no parse, no compress); stale pages
  (renamed slugs, dropped CIS) are pruned by slug set. A `_global_key` (hash of
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
  *zero-byte* overlay means "scraped, no RCP" and is skipped, not fallen back).
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
  `last_fetch`/hash for the TTL. Keep the extraction envelope in sync with
  `clean_rcp`'s `div#textDocument` lookup if either changes.
- **The on-demand refresh service is one of two runtime components** (the other is
  the embed service, further below) (`refresh-service.py`,
  a `uv` PEP 723 script; opt-in, off by default). The site is otherwise fully
  static, but each RCP page has a "Rafraîchir maintenant" button and a >1-year
  auto-refresh (both in `src/app-init.js`) that `POST /api/refresh/<cis>`. Caddy
  reverse-proxies `/api/*` to this service, which runs as a SEPARATE hardened
  container so the web server stays read-only. It does NOT duplicate the scrape or
  build logic: it imports `scrape-rcp.py` and `build.py` by path (importlib) and
  reuses `fetch_one` -> `extract_rcp` -> `write_overlay` -> `render_record` to
  fetch one live page and rebuild just that one `dist/rcp/<slug>.html` (+ .gz/.br).
  It also calls `build_xref_index(names, page_cis)` once at startup and passes the
  result into `build._init_worker`, so a refreshed page keeps the SAME cross-drug
  backlinks a full build makes (that index needs the COMPO/GENER/frequency files,
  mounted read-only, see the hardening notes; absent them it degrades to no
  backlinks). Its `page_cis` comes from `build.page_cis_from_dist()` (a glob of the
  mounted `dist/rcp/*.html`), not the CSV (which this container does not mount), so
  its links target only pages that exist, same as the full build.
  TWO separate worker threads, one per lane, each with its OWN rate limit, so a
  click is NOT stuck behind the crawler's slow gap (that decoupling is the whole
  point). The ON-DEMAND lane (`_demand`, worker `_demand_run`; the button + the >1yr
  auto-refresh) is throttled by the small `REFRESH_DEMAND_RATE_SECONDS` (default 5s),
  so a lone click after an idle period fetches almost at once, and is bounded by
  `REFRESH_QUEUE_MAX` (sheds load as "busy") AND by a catalog-wide
  anti-amplification ceiling `REFRESH_DEMAND_HOURLY_MAX` (default 300, 0=off): a
  rolling-hour cap on distinct on-demand outbound fetches (`_demand_budget_ok_locked`
  prunes `_demand_window` under `_lock`), so nobody can turn the service into a
  high-volume ANSM/EMA scraper by enumerating CIS (the per-CIS floor stops hammering
  ONE drug, this stops hammering the WHOLE catalog); over-budget requests get "busy"
  and a `budget` stat ticks. The crawler is exempt (TTL-bounded, the intended steady
  traffic). It complements, not replaces, the per-IP `rate_limit` on `/api/*` in the
  Caddyfile. The perpetual CRAWLER (worker
  `_crawl_run`) trickles on the large `REFRESH_RATE_SECONDS` (+ jitter, default 120s).
  Both lanes are still SERIAL (one worker each) and share the per-CIS min-interval
  floor (`REFRESH_MIN_INTERVAL_SECONDS`, default 1h), so repeat clicks and many
  visitors on one stale page collapse to a single fetch. The two workers share
  `_pending` (under `_lock`, so the same CIS is never fetched by both at once) and a
  `_persist_lock` (so their manifest writes don't race on `save_manifest`'s one temp
  path). Each throttle mark (`_last_demand_fetch`/`_last_crawl_fetch`) is touched only
  by its own thread, so `_wait_rate(since, rate)` needs no lock. `_process` is
  throttle-free (each worker waits on its lane's rate before calling `_handle`, which
  wraps `_process` with the shared error/pending bookkeeping). Endpoints: `GET /api/health`, `GET /api/status/<cis>`
  (asof + pending), `GET /api/stats` (crawl counters + `crawl` gauge), `POST
  /api/refresh/<cis>[?src=user|auto]` (returns fresh|queued|busy). It is
  same-origin, so the strict `connect-src 'self'` CSP covers the button's fetches.
  If the service is absent, `/api/*` just 502s and the button degrades gracefully,
  so static-only deploys omit it entirely. Both `build.py` and `scrape-rcp.py`
  guard `__main__`, so importing them must stay import-safe (no side effects at
  module load); the refresh service depends on that.
  **The perpetual crawler.** With `REFRESH_CRAWL` on (default), the worker builds
  a frequency-ordered list of every page once (`_build_crawl_order()` reuses
  `scrape.build_queue(force=True, restrict=page_cis)`, the SAME ordering the batch
  scraper CLI uses, restricted to CIS that actually render a page) and then rotates
  a cursor through it (`_claim_next_crawl()`): it picks the next page that is due
  per `scrape.is_due` against `REFRESH_CRAWL_TTL_DAYS` (default 365 ~= 12mo) and is
  not already queued on-demand, marks it pending as `crawl`, and refreshes it. When
  a full rotation finds nothing due, it flips to idle and `_idle_wait_seconds()`
  sleeps until the oldest fresh page next crosses the TTL (capped so it re-polls at
  least hourly), then resumes. So the crawler keeps the whole catalog no staler
  than the TTL, sweeping (~15k pages / one-per-`rate`) then idling, and needs no
  cron. It runs on its OWN worker/rate limit, separate from the on-demand lane (a
  click never waits behind it). Missing BDPM inputs degrade it to off (empty order,
  the `_crawl_run` worker just exits) rather than crashing; `REFRESH_CRAWL=0`
  disables it (crawl worker not started), leaving only button/auto refreshes.
  **Forced re-crawl on SIGHUP (`deploy.sh --rebuild`).** To re-sweep the WHOLE
  catalog on demand (e.g. after a render change) without deleting overlays (which
  would blank pages until re-fetched), send the refresh container `SIGHUP`:
  `deploy.sh --rebuild` does `docker kill --signal=SIGHUP <container>-refresh` after
  the deploy is healthy. The handler (installed in `main()`) calls
  `Refresher.request_recrawl()`, which just flips each enabled lane's one-shot
  `force` flag and sets its `wake` Event (lock-free, so it is signal-safe); the
  worker consumes `force` in `_claim_next_crawl` by seeding `force_pending` with the
  whole order and handing those pages out in frequency order IGNORING the TTL, one
  full pass, before normal TTL rotation resumes. The `wake` Event bumps an idle
  worker out of its (up-to-hour) sleep so the sweep starts at once. Overlays are
  kept and keep serving until each page is re-fetched. BOTH lanes (ANSM + EMA) are
  armed. Installing the SIGHUP handler is also what stops `docker kill --signal=
  SIGHUP` from terminating the container (SIGHUP's default action is to kill). Keep
  the contract in sync across `request_recrawl`/`_claim_next_crawl`/`_crawl_run`/the
  `_CrawlLane` `force`/`force_pending`/`wake` fields (refresh-service.py) and the
  `--rebuild` branch in `deploy.sh` (local, gitignored).
  **The EMA `/eu/` lane.** Centrally-authorized drugs have no ANSM page: the same
  service ALSO refreshes their `/eu/` pages (button + a second crawler) through a
  parallel EMA lane, so one runtime component covers both. It imports
  `scrape-ema.py` (which imports `ema_pdf.py`) by path and reuses
  `process_one` -> re-read overlay -> `build.render_eu_page` to fetch one EMA PDF,
  convert it and rebuild that one `dist/eu/<slug>.html`. The lane is fully DRY: the
  crawler machinery is generalised into a `_CrawlLane` object (name, ttl, rate,
  manifest, order function, cursor/idle state), and there are two instances,
  `_ansm_lane` and `_eu_lane`, driven by the SAME lane-parameterised methods
  (`_build_crawl_order(lane)`, `_claim_next_crawl(lane)`, `_gauge_locked(lane)`,
  `_crawl_run(lane)`, …). `_process` dispatches on `_is_eu(cis)` (a CIS is EU iff
  it is in the cap-meta set but has no RCP page) to `_process_ema` vs
  `_process_ansm`, and `_entry(cis)` / `_persist(manifest, path)` route to the EMA
  manifest (`data/.scrape-ema-manifest.json`) vs the ANSM one, so the two lanes'
  TTL/last_fetch never collide. The EMA lane has its OWN knobs
  (`REFRESH_EMA_CRAWL`, default on; `REFRESH_EMA_RATE_SECONDS`, default 300s, gentle
  because the EMA is strict; `REFRESH_EMA_CRAWL_TTL_DAYS`, default 180) and its own
  worker/rate limit, so an EMA fetch never blocks an ANSM one and vice-versa. Its
  crawl order (`_build_eu_order()`) is the frequency-ordered set of centrally-
  authorized CIS whose **authorization GROUP** has an `ema_pdf` link harvested in
  the ANSM manifest OR a `data/eu` overlay: because every strength/pack of one
  product shares a single EMA PDF (see the authorization-group bullet below), a
  scraped SIBLING seeds the whole group, so all its presentations crawl and each
  grows its OWN overlay on first fetch (self-healing). The on-demand `_eu_url(cis)`
  delegates to `build.resolve_eu`, so it resolves this CIS's link, else a sibling's
  link, else the PDF URL baked into this CIS's (or a sibling's) overlay; a `/eu/`
  page therefore keeps refreshing even with zero harvested links of its own. And
  when NOTHING in the group has a link yet (its group was never ANSM-scraped), the
  button's on-demand refresh calls `_harvest_ema_url` to fetch the EMA PDF href
  live off the ANSM page (the same href `scrape-rcp.py` harvests) and cache it into
  the ANSM manifest first, so a reader never waits for the batch scraper.
  `render_eu_page` needs no live EMA read at build (the overlay is self-describing),
  and everything stays import-safe (`scrape-ema.py`/`ema_pdf.py` guard `__main__`).
  Missing inputs (no cap-meta, no PDF links, no overlays) degrade the lane to off
  (empty order, worker exits); `REFRESH_EMA_CRAWL=0` disables it entirely.
  **Crawl statistics + logging.** Each refresh is tagged with its trigger
  *source* (`user` = button, `auto` = the >1yr page-load refresh, `crawl` = the
  perpetual crawler, set internally and NOT acceptable from a caller); `app-init.js`
  sends `?src=user`/`?src=auto` and the service records counts by source and outcome
  (ok/empty/error) plus on-demand queue depth/ETA, logged as per-item +
  rolling-aggregate lines and exposed at `GET /api/stats` (which also carries a
  `crawl` gauge for the ANSM lane AND a `crawl_eu` gauge for the EMA lane, same
  shape: `{enabled,total,idx,ttl_days,idle,due,forced,eta_seconds}`, where `due`
  is the still-to-fetch page count (the larger of the TTL-due count and the
  `forced` re-crawl backlog, see the SIGHUP bullet) and `eta_seconds` the sweep ETA
  = `due` x that lane's crawl rate; the crawl/aggregate log lines print both as `sweep-eta` in
  `DdHhMm` form (`_fmt_dhm`, since a full sweep spans days), distinct from
  the on-demand `eta` which drains the button/auto queue and reads 00:00 when no
  clicks are pending). `request()` must NOT call
  `asof_of()` while holding `self._lock` (it re-reads the manifest under the same
  non-reentrant lock: that path deadlocked before). `REFRESH_LOG_LEVEL` (default
  INFO) sets the level; `/api/health` is never logged at any level (it fires every
  30s from the container healthcheck). Keep the `src` values in sync across
  `app-init.js`, `_SOURCES`/`request()`, and the `/api/stats` shape.
- **Every RCP page shows a freshness banner ("Informations à jour au …").**
  It bakes TWO distinct *absolute* dates (not the age, so pages stay
  cacheable; `src/app-init.js` turns each into a relative "il y a X" client-side):
  1. **ANSM's own revision date** (the headline) = `data-rcp-ansm`, extracted by
     `_ansm_date()` from the RCP body's `<span class="DateNotif">ANSM - Mis à jour
     le : DD/MM/YYYY</span>` (present on all ~12k pages and every live scrape).
     This is *when the official text was last revised*, which is the meaningful
     date for the reader: a drug ANSM last touched in 2021 is still its current
     text, so an old ANSM date is NOT staleness.
  2. **Our capture date** = `data-rcp-asof` = `BASELINE_DATE` (`2022-05-02`) for a
     baseline CSV cell, or the scrape date for an overlay (from
     `.scrape-manifest.json`'s `last_fetch`, else the overlay mtime; see
     `_load_scrape_dates`/`_overlay_date`). Shown as a small "Version vérifiée par
     justelesRCP le …" line (`.rcp-checked`). `app-init.js` also keys the `.stale`
     "notre copie" notice AND the on-demand refresh trigger off THIS date only
     (not the ANSM date): a copy we have not re-checked in >1 year may lag ANSM's
     live version. So `data-rcp-asof` MUST stay on the element even though
     `data-rcp-ansm` is headlined; the refresh button/service compare against it.
  `_asof_html(ansm, asof)` builds the banner (headline `.rcp-primary` + optional
  `.rcp-checked`); it falls back to the capture date as headline if the ANSM date
  is somehow missing, so the banner is never dateless. The ANSM date derives from
  the RCP HTML (already in each CIS's `_record_hash` via `raw`), and the `asof`
  value is also folded into that hash, so a re-scrape that changes either
  re-renders the page. Keep the contract in sync across `_asof_html`/`_ansm_date`,
  the `{{ASOF}}` slot in `src/rcp.html`, `.rcp-asof`/`.rcp-checked`/`.rcp-warn` in
  `style.css`, and the enhancer in `src/app-init.js`. The "Rafraîchir maintenant"
  refresh control (`.rcp-refresh`, built by `app-init.js`) is `append`ed INSIDE this
  `.rcp-asof` card so it reads as belonging to the capture dates; a stub (no card,
  no `data-rcp-asof`) keeps it just under the `.cis` line instead. Directly under the banner,
  `_official_source_html(url, label)` (injected into the same `{{ASOF}}` slot)
  renders a `.rcp-source` link to the authoritative page (ANSM `ANSM_PAGE_URL`,
  labelled "Ouvrir la source officielle", on `/rcp/`; the direct EMA PDF on full
  `/eu/` pages) so a reader who doubts our copy or spots a rendering bug can open
  the official one. On BOTH RCP AND full `/eu/` pages `app-init.js` relocates this
  lone `.official-link` UP into the `.rcp-refresh` control (after the button, with an
  "ou" separator), so the card reads "Rafraîchir maintenant ou Ouvrir la source
  officielle"; with no JS it stays a standalone `.rcp-source` link below the card.
  `/eu/` full pages now carry a SINGLE source button (the direct PDF): the EMA
  *search* moved into the "En savoir plus" pill row (`include_ema=True` there), so
  the lone PDF button is paired just like an RCP page's.
  It shares the `.official-link` button style with the stub's EMA button (and, once
  relocated, with the refresh button via `.rcp-refresh .official-link`).
- **Every page has an external-reference pill row** (`_ref_links_html`, injected
  into the same `{{ASOF}}` slot AND duplicated at the bottom): an **"En savoir plus"** bounding box
  (`.rcp-more` + `.rcp-more-title`) wrapping a centered `.rcp-refs` row of small
  `.ref-pill` buttons to **BDPM** (this drug's official record, `ANSM_PAGE_URL`
  keyed by CIS), then **HAS**, **EMA**, **CRAT** and **Vidal**, all full-text
  searches on the drug's active substance. **CRAT** (`CRAT_SEARCH_URL`, lecrat.fr
  WordPress `?s=` search) is the pregnancy/breastfeeding reference; Vidal stays
  last per the product intent. The EMA pill is shown on `/eu/` pages too
  (`include_ema=True`): it carries the EMA *search*, while the *direct* EMA PDF is a
  separate source button paired next to "Rafraîchir maintenant". The substance query is
  `load_substances()` (CIS -> cleaned active-substance string from
  `CIS_COMPO_bdpm.txt` column 3, keeping only `SA` rows, combos space-joined). The
  cleaning (`_clean_substance`) drops the parenthetical/after-comma salt AND the
  INLINE salt/hydrate/connector words BDPM bakes into the denomination
  (`_SUBSTANCE_QUALIFIERS`, accent-folded: e.g. "AMOXICILLINE TRIHYDRATEE" ->
  "amoxicilline"), because LeCRAT's strict all-terms search returns nothing on a
  query carrying "trihydratée"; "ACIDE" is deliberately kept so multi-word acids
  survive, and an all-qualifier term (e.g. "SULFATE DE MAGNESIUM") is kept whole. It
  falls back to the drug's `_brand_root` when the composition is unknown. Nothing is fetched at build time (plain search links). The substance map
  is a process-wide global (`_SUBSTANCES`, primed by `_init_worker` for pool
  workers, set in `main()` for build_stubs/render_eu_page, and by the refresh
  service at startup) and is folded into `_global_key` (it feeds the pills but is
  NOT in `_record_hash`, so a composition change must bust the whole cache). The
  SAME block is rendered a second time at the very BOTTOM of `/rcp/` + full `/eu/`
  pages (after "Médicaments liés"), via a separate `{{MORE_BOTTOM}}` slot: each
  render path computes the block once and reuses the string for both `{{ASOF}}` (top)
  and `{{MORE_BOTTOM}}` (bottom); thin `/eu/` stubs fill `{{MORE_BOTTOM}}` empty. Keep
  the contract in sync across
  `load_substances`/`_ref_links_html`/`_init_worker`/`_global_key` (build.py), the
  `{{ASOF}}` + `{{MORE_BOTTOM}}` slots in `src/rcp.html`,
  `.rcp-more`/`.rcp-more-title`/`.rcp-refs`/
  `.ref-pill` in `style.css`, and the refresh service's `_init_worker` call.
- **`/a-propos` is a static About page** (`src/a-propos.html`, shipped as a
  static asset): what the site is, the author, a privacy/hosting note, and a
  direct link to the GitHub repo. (`SOURCE_URL` still drives the separate "Code
  source" link in the DEV banner, `src/dev-banner.js`.)
- **~15% of CIS have an empty RCP field** in the source and are skipped (no RCP
  page, not counted in browse). This is expected, not an error. But the centrally-
  authorized ones among them now get an EU-authorization stub instead of vanishing
  (next bullet).
- **EU-authorization pages (`/eu/`, `build_stubs()`).** A big share of the
  empty-RCP CIS are EMA-centrally-authorized (`procédure centralisée`), whose RCP
  text is published by the EMA, not the ANSM (ABILIFY/aripiprazole, many biologics
  and oncology drugs). They render no RCP page and so were unfindable by search
  (the original bug report: searching "abilify" returned nothing). `build_stubs()`
  emits one `dist/eu/<cis>-<slug>.html` per such CIS (~1883), in ONE of two forms
  per CIS: a **full converted page** when an EMA overlay is available (the
  SmPC/notice converted from the EMA PDF, rendered by `render_eu_page`, see the next
  bullet), otherwise a **lightweight stub** that only points to the EMA.
  **Overlay sharing by authorization group.** One product's EMA PDF covers EVERY
  strength/pack (all presentations share one `EU/x/xx/xxx` number and one SmPC), so
  a presentation with no overlay of its own **borrows a sibling's**: `_auth_key`
  (EU number, brand-root fallback) + `auth_groups` group the CIS, and
  `resolve_eu(cis, cap, groups, links)` returns `(overlay_html, pdf_url)` preferring
  this CIS's own overlay/link else the freshest sibling's (deterministic tie-break
  by CIS, so the incremental cache doesn't churn). So the moment ANY presentation of
  a product is scraped, ALL of them render the full converted page (e.g. ABILIFY
  MAINTENA 300 mg follows 400 mg); a stub with no overlay anywhere still borrows a
  sibling's harvested link so its PDF button points at the real doc, not a search.
  `resolve_eu` is shared with the refresh service (`_eu_url`, the EMA lane).
  The stub: it explains the EU-authorization case, shows the EU number + holder (parsed by
  `load_cap_meta()` from `CIS_bdpm.txt`: a row with an `EU/x/xx/xxx` number or a
  `centralisée` (NOT `décentralisée`, a national procedure that merely shares the
  `centralis` substring: see `_is_central_proc`) procedure), links to the official RCP via **the exact EMA
  product-information PDF when known, else an EMA medicines-search URL by brand
  root**. The direct link is NOT constructed here: the ANSM `/medicament/<cis>/extrait`
  page for a centrally-authorized drug carries no RCP body but DOES link the real
  EMA `_fr.pdf`; `scrape-rcp.py`'s `extract_ema_pdf` harvests that href at SCRAPE
  time into the manifest (`ema_pdf`), and `build_stubs` reads it via
  `_load_ema_links()` and prefers it (`_stub_content(..., ema_pdf)` labels it
  "(PDF)"). A stub whose CIS hasn't been scraped since this landed falls back to
  `_ema_search_url`/`_brand_root` (a search, never a constructed EPAR deep link,
  so it can't 404). Either way **NOTHING is fetched from the EMA at build time**
  (the PDF href is captured during the ANSM scrape, which already runs), so the
  site stays 100% static. ONLY when a
  same-substance generic actually renders here, links to it via `/?q=<substance>`
  (~880; the substance is the longest of the stub's `CIS_COMPO` active-substance
  tokens that also appears in a real page name, salts/short words dropped via
  `_XREF_STOP`/`_XREF_MIN_LEN`; a `(len, token)` tiebreak keeps the pick
  deterministic so the incremental cache doesn't churn on set-iteration order).
  Design that keeps them clean: they ARE in `search-index.json` (an `eu:1` flag
  routes `search.js` to `/eu/` instead of `/rcp/`) and kept OUT of `/browse`; a
  **full** converted `/eu/` page is `INDEXABLE` (real EMA SmPC content, in the
  sitemap), but a bare **stub** stays `noindex` (thin, just a pointer out) so it adds
  no thin-content SEO surface. Their own `/eu/` URL space
  keeps them OUT of the RCP cross-link graph (`page_cis_from_dist()` globs
  `dist/rcp`, never `dist/eu`, so full build and the refresh service agree on link
  targets); they reuse `src/rcp.html` via a `{{HEADEXTRA}}` slot (which now carries the
  canonical + Open Graph + JSON-LD, plus the `noindex` meta on stubs only) so there is
  no chrome duplication; and they fold
  into the incremental manifest (stub CIS never collide with RCP CIS: one has an
  empty RCP, the other non-empty), so an unchanged rebuild reuses all of them. The
  manifest also carries a per-CIS `full` flag + `asof` date (never in `search-index.
  json`), so `write_sitemap` lists full pages with a `<lastmod>` and excludes stubs.
  `src/app-init.js` shows a refresh button on EVERY `/eu/` page: a full page bakes
  `data-rcp-asof` and gets "Rafraîchir maintenant" (+ the >1yr auto-refresh keyed
  off that date), while a stub (which has no `data-rcp-asof`) is detected by its
  `.rcp-stub` body and gets a manual-only "Importer le RCP de l'EMA sur justelesRCP"
  button (worded so the reader understands the SITE downloads + shows the doc here, not
  that they leave for the EMA), so a reader never has to wait for the background crawler.
  The click status sets a clear expectation (the page reloads itself, usually under 30 s,
  else refresh manually). Clicking either POSTs
  `/api/refresh/<cis>`; the refresh service's EMA lane resolves the PDF (own,
  sibling, or harvested live off the ANSM page) and converts it, and the button's
  poll reloads into the now-full page. Keep the stub contract in sync across
  `build_stubs` (which now fans its per-CIS body out over a `Pool`: that body is
  `_render_stub`, primed per worker by `_init_stub_worker`, so a change to how a
  stub or full page renders goes there, not in an inline loop)/`load_cap_meta`/
  `_stub_content`/`_load_ema_links`/`resolve_eu`/
  `auth_groups` (build.py) and `extract_ema_pdf` + the manifest `ema_pdf` field
  (scrape-rcp.py), the `{{HEADEXTRA}}` slot in `src/rcp.html`, the `eu`-flag branch
  in `src/search.js`, the `.rcp-stub` button gate in `src/app-init.js`, and
  `.rcp-stub`/`.stub-*` in `style.css`.
- **EMA SmPC conversion, the `/eu/` full page (phase 2, done).** `ema_pdf.py`
  (PEP 723, `pymupdf`/fitz, pure + import-safe) converts an EMA
  product-information PDF into ONE self-contained HTML blob wrapped in the SAME
  `<div id="textDocument">` envelope the ANSM overlays use: `convert(pdf_bytes)`
  reflows text into the QRD-numbered `AmmAnnexeTitre*` headings the site already
  styles, rebuilds real `<table>`s (`find_tables`), embeds meaningful figures
  base64 (dropping tiny pictograms, transcoding to JPEG/PNG, downscaling), groups
  the doc into collapsible `<details class="ema-annexe">` blocks (the SmPC open),
  and reads the capture date from the PDF `ModDate` (else `CreationDate`).
  `scrape-ema.py` (PEP 723; imports scrape-rcp.py + ema_pdf.py) is the EMA
  counterpart of `scrape-rcp.py`: it reads the `ema_pdf` links scrape-rcp.py
  harvested into the ANSM manifest, fetches each PDF politely, and writes one
  overlay per drug at `data/eu/<cis>.html[.gz]` (its OWN manifest,
  `data/.scrape-ema-manifest.json`, so the EMA TTL/last_fetch never collides with
  the ANSM one). It stays DRY by importing scrape-rcp.py's manifest/queue/overlay
  helpers (parameterised by path/dir) rather than re-implementing them.
  **Bulk `ema_pdf` seeding from the EMA JSON dump (`--seed-from-ema-json`).** The
  `ema_pdf` links usually come from scrape-rcp.py's per-CIS `extract_ema_pdf`
  harvest off each ANSM page, but the EMA ALSO publishes every EPAR document as one
  ~28 Mo JSON (`EMA_EPAR_JSON_URL`), the SmPC "product-information" among them, so
  `seed_ema_links` can learn every centrally-authorized product's French SmPC PDF
  in one download WITHOUT a prior ANSM harvest. It downloads (or reads a local
  `--ema-json` file), tolerantly parses the dump (`_parse_ema_documents`: records
  carrying a `translations` object are NOT comma-separated, so `json.loads` fails,
  decode successive objects instead), keeps `type == "product-information"`
  (`_ema_pi_index`, French `translations.fr` preferred), and writes the matching
  `ema_pdf` into the ANSM manifest for each `build.auth_groups` group that lacks a
  link, seeding ONE representative CIS (`min(members)`; siblings borrow the overlay
  via `build.resolve_eu`). The join is by BRAND (`_match_brand` whole-word-boundary
  match of `build._brand_root` vs the EMA `medicine_name`): the dump keys on the EMA
  *product* number `EMEA/H/C/xxxxxx`, NOT the `EU/x/xx/xxx` marketing-auth number
  `load_cap_meta` keys on, so there is no id join; it covers ~97% of auth-groups
  (~1080/1114), the ~3% brand-mismatched generics still falling to the per-CIS ANSM
  harvest. A harvested link always wins (never overwritten); a later scrape-rcp
  re-fetch of a seeded CIS may drop the seeded link, but by then the overlay is
  fetched + self-describing, so the /eu/ page stays full. `--dry-run` reports the
  plan and writes nothing. It lazily imports `build.py` (needs `brotli`); pure
  helpers are covered by `test_ema_seed.py`. Keep in sync across
  `seed_ema_links`/`_parse_ema_documents`/`_ema_pi_index`/`_match_brand`/
  `EMA_EPAR_JSON_URL` (scrape-ema.py) and `load_cap_meta`/`auth_groups`/
  `_brand_root`/`resolve_eu` (build.py). **Internet
  Archive fallback:** when the live EMA PDF fails to download (network error, 404,
  or a 200 that is actually an HTML error page: `_fetch_pdf` requires the `%PDF-`
  signature), `process_one` falls back to the Wayback Machine (`_wayback_pdf`, the
  availability API + the raw `id_` snapshot) and converts the archived copy. The
  reader NEVER sees the archive URL (the overlay still bakes the real EMA URL as its
  source button); we only STAMP that the archive was used, so these can be re-tried
  later against the live EMA: on the overlay (`data-ema-archive="1"`) and in the
  manifest (`via_archive: true`, which `scrape-ema.py --retry-archived` re-fetches).
  The overlay wrapper bakes these self-describing facts so build stays
  overlay-self-contained (no EMA-manifest read at build): `data-ema-date` (the
  ModDate), `data-ema-fetched` (OUR capture date), `data-ema-pdf` (the source PDF
  URL), and `data-ema-archive` (present only on archive-sourced overlays).
  `build.render_eu_page(cis, overlay, meta, tpl, pdf_fallback)` is the
  single-page `/eu/` renderer (the counterpart of `render_record`), reused by BOTH
  `build_stubs` (batch) and the refresh service (on-demand). Its freshness banner
  mirrors an ANSM page's two dates: the ModDate is the "à jour au" headline
  (`data-rcp-ansm`) and OUR fetch date the "vérifiée le" line + refresh key
  (`data-rcp-asof`); keying the refresh off the fetch date (not the ModDate)
  makes a refresh detectable even when the EMA PDF is unchanged (the common case).
  It bakes ONE source button (the direct PDF; the EMA search is a pill in "En savoir
  plus") that app-init.js pairs next to the refresh button, and, when the overlay
  is archive-sourced (`_eu_via_archive`), a warn-tinted note telling the reader the
  text came through a web archive. Keep the /eu/
  full-page contract in sync across `ema_pdf.convert`/`_overlay_html`
  (scrape-ema.py), `render_eu_page`/`_eu_date`/`_eu_fetched`/`_eu_pdf`/`_eu_toc`/
  `_eu_via_archive` (build.py), and `.rcp-eu`/`.ema-annexe`/`figure`/
  `.rcp-archive-note` in `style.css`. The EMA link
  is a search only as a *fallback*; a fetched page links the exact PDF. **NOTHING
  is fetched from the EMA at build time** (the site stays 100% static); the PDF is
  fetched only by `scrape-ema.py` or the refresh service's EMA lane.
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
  is NOT embedded (it is navigation, not content, and prefixing it dilutes every
  vector), so `chunk_text` is the section body alone and a heading-only section yields
  no chunk; DSFR back-to-top links + their "Redirection vers le haut de page" tooltip
  spans (fresh-scrape chrome) are stripped in `_STRIP_XPATH` so they never reach a
  chunk; and filler narrative paragraphs (`_is_filler_paragraph`: exactly "Sans objet",
  the "[à compléter ultérieurement par le titulaire]" QRD placeholder, `<= 5` chars, or
  `<= 2` words) are dropped before the section body is assembled (table rows are exempt). `_CHUNK_FORMAT_VERSION` (folded into `raw_hash`/`src_hash`) busts
  every `.vec.json` when this segmentation changes, since the raw overlay is untouched.
  **The warm encoder** (`onnx_embed.Encoder`) loads the int8
  `Snowflake/snowflake-arctic-embed-l-v2.0` ONNX weights (`onnx/model_int8.onnx`) +
  tokenizer ONCE with `onnxruntime` + `tokenizers` ONLY (NO torch: a ~300 Mo image, not
  ~2 Go; the weights are ~570 Mo, mounted read-only), and is shared by the query path and
  the background page path (`session.run` is thread-safe). A per-model recipe
  (`onnx_embed._profile`, keyed on `RUNTIME_MODEL`) picks the ONNX file, pooling, prefixes
  and MRL width: arctic-l-v2.0 = **CLS-pool -> L2, query-only `query: ` prefix (NO passage
  prefix), Matryoshka-truncated to 256 dims** (truncate THEN normalise once), verified
  against the repo config + ONNX graph (inputs `input_ids`/`attention_mask` only, output
  `token_embeddings`). Swapping `RUNTIME_MODEL` (+ the matching `scripts/download-model.sh` fetch)
  re-embeds the whole catalog (`read_vec_meta` gates on the model name). The prior model
  was e5-small (mean-pool, `query:`/`passage:` prefixes, 384 dims). `build.quantize_int8`
  (symmetric `q=round(v*127)`, dequant `q/127`) is the ONE canonical formula, mirrored
  in JS `decodeVec`. **The embed service** (`embed-service.py`, behind Caddy's
  same-origin `/api/sem/*`, mirrors `refresh-service.py`: `ThreadingHTTPServer`,
  `_lock`, a priority queue, ONE background worker) embeds the query on request
  (`POST /api/sem/embed` -> `{q: base64-int8, dim}`, on a request thread so it never
  waits behind the worker; bounded LRU; `>= EMBED_MIN_QUERY_CHARS` / `<=
  EMBED_MAX_QUERY_CHARS` else 400; a `BoundedSemaphore` of `EMBED_MAX_CONCURRENT_QUERIES`
  bounds concurrent encodes so a query flood can't pin every core, shedding past it with
  503; query CONTENT never logged) and (re-)embeds each
  **crawled** page in the background (`POST /api/sem/page/<cis>` embeds now, front of
  queue; `GET /api/sem/page/<cis>` -> `{embedded, pending}`; `/api/sem/health` never
  logged; `/api/sem/stats`). **Crawled-only + content-hash staleness**: it embeds ONLY
  overlay pages (`build.iter_overlay_raw`, `data/rcp` + `data/eu`, NEVER the frozen
  2022 baseline CSV) via the SINGLE `build.embed_page_to_vec` core (segment -> encode
  -> gate -> write), and re-embeds a page ONLY when the `src_hash` baked into its
  `.vec.json` (`build.raw_hash` of the overlay) or the model changed (`build.read_vec_meta`),
  so a no-op refresh that rewrote identical bytes re-embeds nothing and there is NO
  manifest. A search on a never-crawled (baseline-only) page returns `crawling`: the
  service asks the refresh service (`REFRESH_TRIGGER_URL`) to crawl it first, then
  embeds it when the overlay lands (driven by the refresh service's `EMBED_NOTIFY_URL`
  ping, with a periodic reconcile scan as backstop). The reconcile scan's cheap enqueue
  gate is `build.vec_is_fresh` (stat-only mtime), but its FIRST pass runs with
  `check_model=True` so a MODEL swap (which leaves each already-embedded `.vec.json`
  newer than its unchanged overlay, hiding the mismatch from a pure mtime gate) is
  re-embedded across the whole catalog on restart; the authoritative `src_hash`+`model`
  gate still lives in `embed_page_to_vec`. It writes the served
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
  the query floats up. The candidate gate is the RAW cosine `>= SEM_FLOOR` (~0.5, "at
  least 50% similarity"; a `continue`, not a `break`, since the list is sorted by the
  hybrid score), capped at `MAX_RESULTS` (25) distinct passages; a wholly-irrelevant query
  (nothing clears the floor) still yields "Aucun passage pertinent.". Each surviving hit
  shows the FULL on-page passage it resolves to (read from that DOM paragraph via
  `displayText`; a table-row/heading-resolved hit keeps the stored snippet) plus its
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
  keep the box open. It is injected right AFTER the ToC (so
  the reading column reads pills -> Sommaire -> search) and is `position: sticky` (like
  the ToC), so a reader can search a long RCP without scrolling back up; its offset
  (`top: 7.2rem` on all viewports now the sidebar is gone) stacks under the sticky top
  bar + collapsed Sommaire bar (`.semsearch` in `style.css`), and its results list scrolls
  internally (`max-height: 40vh`) so the stuck panel never outgrows the screen. Both the
  search box and the ToC use the native
  `<details>` disclosure triangle as their collapse affordance (`list-style-position:
  inside`), kept consistent on purpose. If the embed service is absent, `/api/sem/*` 502s
  and the box degrades to "indisponible". **The offline pre-bake** `embed-rcp.py` (optional; warms
  the backlog before a first deploy) reuses the SAME `onnx_embed.Encoder` and
  `build.embed_page_to_vec` over `build.iter_overlay_raw()`, writing the SAME
  `.vec.json` with the SAME `src_hash`, so offline and online vectors never disagree.
  `onnx_embed.RUNTIME_MODEL` (server) and the model `scripts/download-model.sh` fetches MUST
  stay the same weights (query and passage vectors must match). Keep the contract in
  sync across `section_chunks`/`quantize_int8`/`raw_hash`/`iter_overlay_paths`/
  `iter_overlay_raw`/`dist_page_for`/`read_vec_meta`/`vec_is_fresh`/`vec_payload`/
  `write_vec_json`/`embed_page_to_vec` (+ the shared `OVERLAY_LANES`/`CIS_RE`)
  (build.py), `onnx_embed.py`, `embed-service.py`, `embed-rcp.py`,
  `src/rcp-semsearch.js`, the `<script>` in `src/rcp.html`, `.semsearch*` in
  `style.css`, `scripts/download-model.sh`, the `/api/sem/*` route + strict CSP in
  `docker/Caddyfile`, and the `embed` service in `docker/docker-compose.yml` +
  `docker/embed.Dockerfile`. `test_embed.py` covers the pure pieces (int8 round-trip
  bound, `section_chunks` `sec-N` alignment, `vec_payload`/`read_vec_meta` round-trip,
  `raw_hash` staleness key).
- **Precompression (.gz/.br) is baked at build time** so Caddy spends zero CPU
  compressing. `compress()` writes both siblings; the Caddyfile uses
  `precompressed br gzip`.
- **SEO is baked at build time, from ONE origin constant (`SITE_URL`).** The site
  is a mirror of public ANSM/EMA data, so the SEO job is discovery + making its
  advantages (speed, structure, no ads) legible, not unique content. `SITE_URL`
  (the deployed origin) is a hardcoded constant, deliberately NOT an env var:
  `render_record`/`render_eu_page` run in BOTH the batch build AND the
  refresh-service container, so baking it into `build.py`'s source keeps a
  re-rendered page's canonical/OG identical to a batch-built one with no env-var
  plumbing to forget (a self-hoster edits the one line). What's emitted, all from
  `SITE_URL` via `_abs_url`: (1) `dist/sitemap.xml` (`write_sitemap`) listing home,
  `/a-propos`, the browse pages, every `/rcp/` and the INDEXABLE full `/eu/` pages,
  each with a `<lastmod>` from its capture date (bare `/eu/` stubs are noindex, so
  excluded); (2) `dist/robots.txt` (`write_robots`) allowing all but `/api/*` and
  pointing at the sitemap; (3) a `<link rel="canonical">` on every page
  (`_canonical_link`); (4) Open Graph + Twitter Card tags (`_social_meta`) including
  `og:image` = `/og.png` (a raster social card; the SVG logo is NOT used for
  `og:image` because most social crawlers can't render SVG); (5) JSON-LD (`_jsonld`, `<`-escaped, CSP-exempt as
  a data block): `WebSite` + `SearchAction` on the home page, and a `Drug` +
  `MedicalWebPage` @graph on each RCP/full-`/eu/` page (`lastReviewed` = the ANSM
  revision date, or the EMA PDF ModDate on `/eu/`); (6) a visible breadcrumb +
  matching `BreadcrumbList` on drug pages (`_breadcrumb`, built together so they
  never disagree). The per-drug `<meta description>` is dynamic too
  (`_rcp_description`/`_eu_description`: substance + intent keywords, not one
  boilerplate on 12k pages). Injection points: the shared `src/rcp.html` fills
  `{{HEADEXTRA}}` (canonical/OG/JSON-LD/noindex), `{{DESCRIPTION}}` and
  `{{BREADCRUMB}}` in all three render paths; the hand-written `src/index.html`,
  `src/a-propos.html` and `src/browse.html` carry a `{{HEAD}}` slot filled by
  `_static_page_head`/`write_browse`. All of this is baked, so the strict CSP is
  untouched and the site stays 100% static; the refresh service (single-page
  rebuilds) does NOT regenerate the sitemap, so a NEW full `/eu/` page enters the
  sitemap only at the next full build. `asof`/`full` ride in the build manifest (NOT
  in the client-downloaded `search-index.json`, kept lean). Keep the contract in
  sync across `SITE_URL`/`_abs_url`/`write_sitemap`/`write_robots`/`_canonical_link`/
  `_social_meta`/`_jsonld`/`_drug_jsonld`/`_website_jsonld`/`_breadcrumb`/
  `_rcp_description`/`_eu_description`/`_static_page_head` (build.py), the `{{HEAD}}`/
  `{{HEADEXTRA}}`/`{{DESCRIPTION}}`/`{{BREADCRUMB}}` slots in `src/*.html`, and
  `.breadcrumb` in `style.css`. `test_embed.py` covers the pure pieces. The brand
  assets `src/logo.svg` (SVG favicon linked in all four `src/*.html` heads + the
  README header) and `src/og.png` (the `og:image` card, regenerated from the logo)
  are copied to `dist/` by `main()` (`og.png` uncompressed).
- **Runtime config is injected, not baked.** Every page loads `/app-config.js`,
  which defines `window.__APP_CONFIG__` (optional umami analytics + a DEV
  banner). `src/app-config.js` is the local-dev fallback (all empty = nothing
  loads). In the container, `docker/entrypoint.sh` renders an equivalent file
  from `docker/.env` into the `/gen` tmpfs and `docker/Caddyfile` serves THAT for
  `/app-config.js`, so the build stays config-free. `src/app-init.js` injects the
  umami tag + a delegated click tracker (`window.trackEvent(name, data)`, a no-op
  when metrics are off / Do-Not-Track), and `src/dev-banner.js` shows the WIP
  banner when `DEV=1`. Keep the config keys in sync between `src/app-config.js` and
  the heredoc in `docker/entrypoint.sh`. **Analytics privacy for per-drug semantic
  search:** the reader's query is NEVER sent to umami (it goes only to the
  same-origin embed service). The click tracker explicitly skips `.semsearch` (so a
  result snippet, i.e. page text, never becomes an event label), and
  `src/rcp-semsearch.js` emits its own query-free events instead: `recherche-rcp`
  (once per opened search session, `{resultats: <count>}`) and `recherche-rcp-nav`
  (`{sens: "precedent"|"suivant"}` per prev/next click). Keep that skip + the two
  events in sync across `src/app-init.js` and `src/rcp-semsearch.js`.
- **A guided product tour onboards new visitors** (`src/tour.js`, a small
  dependency-free spotlight/card driver; loaded on `src/index.html` + `src/rcp.html`,
  registered in `build.py`'s `static_assets` tuple so it is copied + `.gz`/`.br`
  compressed like any client asset). It is a **9-step** tour running across TWO pages
  via a sessionStorage handoff (`RESUME_KEY = jlrcp_tour`, value `rcp` going forward,
  `home` going back): a **HOME phase** on the landing page (a welcome popup, then step
  1 `homeSearchBox` spotlighting the empty `#q` field (WITHOUT focusing it, so mobile
  keyboards stay down), then step 2 `homeSearch` which
  types "quétiapine" letter-by-letter into `#q`, pulses the FIRST `#results` link, and
  installs a capturing document click-guard that forbids clicking anything but the
  search box / a result / the card, a result click routing to the example page) then
  navigates to **one hardcoded quetiapine page** (CIS `60078765`) where the **RCP
  phase** walks the reader through: the freshness/refresh/source/"En savoir plus" card
  (step 3, `.rcp-asof` + `.rcp-more`), the Sommaire (step 4, `.toc`), then the semantic
  search broken into FOUR steps: open `.semsearch` (step 5, starts the embed warm-up),
  type the real query « Impact des repas sur la biodisponibilité » letter-by-letter
  (step 6), highlight the hit whose snippet contains "repas riche en graisses" and let
  the reader click it (step 7, the click collapses the box + scrolls, then advances),
  a "vous y êtes" confirmation on the scrolled-to passage (step 8, `.semsearch-current`/
  `.semsearch-hit`), and finally "Médicaments liés" (step 9, `.drug-xref-list`), then a
  "Bonne visite" end modal. EVERY step card carries a **"‹ Précédent"** back button
  (via `spec.back`; back just re-invokes the previous step function, which re-establishes
  its own DOM state; the first RCP step's back hops to `/` and resumes `homeSearch` via
  the `home` handoff) and a top-right **close cross** (the only "skip"; there is no
  separate skip button). Each step change re-triggers a `.tour-anim` fade/rise on the
  card (centered modals are placed in PIXELS, not a translate transform, so the
  animation's `transform` is free); the spotlight stays faded out while the step scrolls
  its target into view (`afterScrollSettles` polls the scroll offset, gated by `revealPending`
  + a `scrollGen` token) and only fades in once the page is stationary, so the rect appears
  at rest instead of chasing the target across a moving page.
  Triggers: auto on first landing visit (`localStorage['jlrcp_tour_seen']`), `?tour=1`
  on the landing page (always), or `?tour=rcp` directly on the quetiapine page. It
  drives the closure-encapsulated semantic search purely via the DOM (set `.open`, set
  `.semsearch-input.value` + dispatch `input`, then click the `.semsearch-go` button to
  run the search since typing no longer auto-searches, observe `.semsearch-results`), so
  it needs no new API there; the embed service may be slow/absent, so the pick step (7)
  has an ~18s "Passer cette étape" fallback that jumps to step 9. Relaunch entry
  points: a landing-page `.browse-link`, a landing footer link, and the `?tour=1` URL.
  It is CSP-safe (same-origin script, no inline handlers/eval, no external assets, all
  styling via classes / CSSOM setters; only reads the guarded `window.trackEvent`).
  Keep the contract in sync across `src/tour.js`, the `<script src="/tour.js">` tags
  in `src/index.html` + `src/rcp.html`, the `.tour-*` styles in `style.css` (incl.
  `.tour-anim`), the `static_assets` tuple (build.py), and the target ids/classes it
  hooks (`#q`, `.searchbox`, `#results`, `main.rcp[data-cis]`, `.rcp-asof`, `.rcp-more`,
  `.toc`, `.semsearch`/`.semsearch-input`/`.semsearch-go`/`.semsearch-results`/
  `.semsearch-hit-link`/`.semsearch-current`/`.semsearch-hit`, `.drug-xref-list`).

## Commands

```bash
./scripts/download-data.sh        # fetch + auto-extract the frozen 2022 data/CIS_RCP.csv (zip), and
                          #  refresh data/CIS_bdpm.txt (+ COMPO/GENER) from the LIVE daily BDPM feed
./scripts/download-model.sh       # OPTIONAL: fetch the arctic-embed-l-v2.0 int8 ONNX + tokenizer into
                          #  ./models (~570 Mo, gitignored) for SERVER-SIDE per-drug semantic search
                          #  (mounted read-only
                          #  into the embed container; no longer served to browsers). Skip it and the
                          #  "Recherche sémantique dans ce RCP" box just degrades to "indisponible".
uv run src/scrape-rcp.py --limit 60   # OPTIONAL manual/one-time bulk scrape into data/rcp/ overlay
                                  # (the refresh service's perpetual crawler now does routine
                                  #  freshening; use this for a first --all seed or an ad-hoc batch)
                                  # env: RCP_OVERLAY_GZIP (gzip overlays, default on),
                                  # RCP_SCRAPE_RATE_SECONDS (base gap between fetches);
                                  # logs a progress bar + ETA and the trigger (user/timer)
uv run src/scrape-ema.py --limit 60   # OPTIONAL bulk scrape of EMA product-information PDFs into
                                  # data/eu/ overlays (converted to HTML by ema_pdf.py). Reads the
                                  # ema_pdf links scrape-rcp.py harvested; own manifest
                                  # data/.scrape-ema-manifest.json. The refresh service's EMA crawler
                                  # does routine freshening; use this for a first seed / ad-hoc batch.
                                  # Falls back to the Internet Archive if a live EMA PDF fails (never
                                  # exposes the archive URL; stamps via_archive). --retry-archived
                                  # re-fetches just the archive-sourced CIS against the live EMA.
                                  # --seed-from-ema-json bulk-seeds the ema_pdf links from the EMA's
                                  # own EPAR-documents JSON dump (brand-joined, ~97% of auth-groups)
                                  # so a first seed needs no prior per-CIS ANSM harvest; --ema-json
                                  # <file> uses a local dump, --dry-run reports the plan and exits.
uv run src/embed-rcp.py --limit 60    # OPTIONAL offline pre-bake of the semantic-search vectors (warms
                                  # the backlog before a first deploy). Reuses the SAME warm encoder
                                  # (onnx_embed) + one-page core (build.embed_page_to_vec) the embed
                                  # service uses, over build.iter_overlay_raw (CRAWLED overlays only,
                                  # --no-eu for RCP only), writing dist/<rcp|eu>/<slug>.vec.json
                                  # DIRECTLY with the same src_hash. No manifest (content-hash gated).
                                  # Needs ./scripts/download-model.sh's model + a prior `uv run src/build.py`.
uv run src/build.py           # build ./dist from ./data (overlay wins over the 2022 CSV; a data/eu
                          #  overlay makes /eu/<cis> a full converted page instead of a stub). Does NOT
                          #  bake vectors anymore: the embed service / embed-rcp.py write .vec.json
                          #  directly; build.py only prunes orphan .vec.json when a slug is dropped.
uv run src/refresh-service.py # optional: run the refresh API on :8460 (behind Caddy /api/*)
                          # runs TWO perpetual frequency-ordered crawlers (ANSM + EMA /eu/) +
                          #  on-demand button/auto refreshes for both; pings the embed service
                          #  (EMBED_NOTIFY_URL) after each fresh overlay so it re-embeds the page
                          # knobs (env): REFRESH_RATE_SECONDS (default 120), REFRESH_CRAWL (1=on) +
                          # REFRESH_CRAWL_TTL_DAYS (365); EMA lane REFRESH_EMA_CRAWL (1=on) +
                          # REFRESH_EMA_RATE_SECONDS (300) + REFRESH_EMA_CRAWL_TTL_DAYS (180);
                          # REFRESH_LOG_LEVEL (INFO, health never logged);
                          # GET /api/stats returns crawl counters + crawl (ANSM) + crawl_eu gauges
uv run src/embed-service.py   # optional: warm SERVER-SIDE embedder on :8461 (behind Caddy /api/sem/*)
                          # keeps the ONNX encoder warm; embeds the reader's query on request
                          #  (no browser model) + (re-)embeds each CRAWLED page's sections in the
                          #  background, writing dist/<rcp|eu>/<slug>.vec.json (never the 2022 baseline)
                          # knobs (env): EMBED_ENABLE (backlog sweep 1=on), EMBED_INTRA_THREADS (4),
                          # EMBED_BACKLOG_RATE_SECONDS (2), EMBED_RECONCILE_SECONDS (60), EMBED_QUEUE_MAX
                          #  (500), EMBED_MAX_CONCURRENT_QUERIES (8, bounds encode CPU; per-IP rate limit
                          #  belongs at the proxy), EMBED_MIN/MAX_QUERY_CHARS (5/400), EMBED_QUERY_CACHE
                          #  (256) + EMBED_QUERY_CACHE_TTL_SECONDS (60, bounds query-data retention; 0=off),
                          #  EMBED_MODEL_DIR, REFRESH_TRIGGER_URL (baseline auto-crawl), EMBED_LOG_LEVEL;
                          # GET /api/sem/stats; query CONTENT is never logged (privacy)
cp docker/env.example docker/.env                      # optional: umami analytics / DEV banner / refresh + embed knobs
docker compose -f docker/docker-compose.yml up -d      # serve ./dist on :8459 + refresh + embed services (read-only, hardened)
docker compose -f docker/docker-compose.yml up --build # after changing Caddyfile/compose/refresh.Dockerfile/embed.Dockerfile
                                                       # OR the scripts baked into the refresh/embed images (build.py /
                                                       # scrape-rcp.py / scrape-ema.py / ema_pdf.py / onnx_embed.py /
                                                       # refresh-service.py / embed-service.py / bdpm.py / src/rcp.html):
                                                       # a plain `up` reuses the old image and ships stale code
```

To rebuild after a data refresh: re-run `scripts/download-data.sh` then `uv run src/build.py`;
the build is incremental (only changed drugs are re-rendered; see the
`.build-manifest.json` note above), so a rebuild on unchanged data is fast.
Restart is not needed (Caddy reads the mounted dir live), but a
`docker compose -f docker/docker-compose.yml restart web` is harmless.

## Deployment / hardening notes

- Caddy listens on **:8459 plain HTTP**; TLS is expected to be terminated by an
  upstream reverse proxy you already run. There is no ACME/TLS config here.
- **The exposed `/api/*` scraping endpoints are rate-limited at the edge.** The
  `web` image is NOT the stock `caddy:2-alpine`: it is a custom xcaddy build
  (`docker/web.Dockerfile`, compose `build: context: .`) that bakes in the
  `caddy-ratelimit` plugin (the stock image has no rate-limit module), so the
  Caddyfile can `rate_limit` `/api/*` and `/api/sem/*` **per client IP** (returns
  429 over the limit). Because requests arrive via your upstream TLS proxy, a
  global-options `servers { trusted_proxies static {$TRUSTED_PROXIES:private_ranges} }`
  is what lets `{http.request.client_ip}` (the rate-limit key) resolve to the real
  visitor instead of the proxy IP; set `TRUSTED_PROXIES` if your proxy is on a
  public IP. The limits are tunable via `API_RATE_EVENTS`/`API_RATE_WINDOW` and
  `SEM_RATE_EVENTS`/`SEM_RATE_WINDOW` (env, read by Caddy). Both API handles also
  `request_body { max_size 16KB }` (reject oversized POSTs at the edge) and the two
  operational stats endpoints (`/api/stats`, `/api/sem/stats`) are **blocked**
  (`respond 404`, exact-path `handle` before the wildcards) so they are reachable
  only from inside the docker network, not publicly. These edge limits COMPLEMENT
  the refresh service's global hourly scrape ceiling (`REFRESH_DEMAND_HOURLY_MAX`,
  which caps the aggregate) and its per-CIS min-interval floor; keep all three.
  The `order rate_limit before reverse_proxy` global option is required (the plugin
  directive is non-standard). `web.Dockerfile` leaves the plugin version UNPINNED
  (a TODO): pin it for reproducible builds. Rebuild with `up --build` after any
  Caddyfile/web.Dockerfile change. Keep the contract in sync across
  `docker/web.Dockerfile`, the global block + the `/api/*` and `/api/sem/*`
  `handle`s in `docker/Caddyfile`, the `web` `build:` in `docker/docker-compose.yml`,
  and the rate-limit knobs in `docker/env.example`.
- The container runs `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, with tmpfs for Caddy's scratch dirs (`/tmp`, `/config`,
  `/data`, and `/gen` for the rendered `app-config.js`). Keep it that way; if
  Caddy needs a new writable path, add a tmpfs mount rather than dropping
  read-only.
- `cap_drop: ALL` is paired with `cap_add: [NET_BIND_SERVICE]`, and that one cap
  must stay. The Caddy binary (whether stock `caddy:2-alpine` or our xcaddy build,
  which is copied onto that same base) ships with `setcap
  cap_net_bind_service=+ep`; the effective bit makes `execve()` fail with EPERM
  ("Operation not permitted", crash loop at the `exec caddy` line of
  entrypoint.sh) if the cap is absent from the bounding set that `cap_drop: ALL`
  empties. We don't bind a privileged port (we listen on 8459), but the binary's
  file cap still has to be satisfiable at exec time. Do not remove it.
- A strict CSP (`default-src 'self'`) is set in the Caddyfile. The site uses no
  external fonts, scripts, or CDNs by design. The ONLY escape hatch is the umami
  origin: `entrypoint.sh` derives `ANALYTICS_ORIGIN` from `ANALYTICS_URL` and the
  Caddyfile adds it to `script-src`/`connect-src` (empty when analytics is off).
  Keep it self-contained otherwise so the CSP holds. Since per-drug semantic search
  moved server-side, the browser no longer instantiates any WebAssembly, so the old
  `'wasm-unsafe-eval'` relaxation is GONE and `script-src` is back to `'self'` (+ the
  optional umami origin); the query POST to `/api/sem/embed` is same-origin, covered
  by `connect-src 'self'`. The `/vendor` + `/models` immutable-cache matchers are also
  gone (the ~120 Mo model is no longer served to browsers, only mounted into the embed
  container), so ALL responses are plain `Cache-Control: no-cache`.
- **`docker/.env` is gitignored** (real analytics ids); `docker/env.example` is
  the committed template. Compose loads it via `env_file`; there is deliberately
  no `environment:` block (it would shadow `env_file` with empty defaults).
- `ANALYTICS_URL` must point at the umami **script** (`.../script.js`), not the
  instance base URL. `entrypoint.sh` validates this at startup (reachable AND
  serves JavaScript) and refuses to start otherwise, so a misconfig fails loud.
- **The refresh service is a second, separately-hardened container** (compose
  `refresh`, `docker/refresh.Dockerfile`), kept apart from `web` precisely so the
  web server can stay fully read-only. It is `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, tmpfs `/tmp`, and is NOT published to the host (no `ports:`);
  it is only reachable through Caddy's `/api/*` proxy. Its writable mounts are the
  narrow paths it must write, in two matching triples (one per lane): the ANSM lane
  writes `data/rcp`, `dist/rcp`, and the `data/.scrape-manifest.json` file; the EMA
  `/eu/` lane writes `data/eu`, `dist/eu`, and the SEPARATE
  `data/.scrape-ema-manifest.json` file. Everything else is mounted read-only: the
  CIS->name map (`data/CIS_bdpm.txt`) plus the backlink-index inputs
  (`data/CIS_COMPO_bdpm.txt`, `data/CIS_GENER_bdpm.txt`, `data/drugs_frequency.jsonl`),
  which the EMA lane also reuses for its cap-meta + crawl ordering.
  Note the single-file mounts (both manifests, `data/CIS_bdpm.txt`,
  and those three backlink files) must exist as real files on the host before `up`,
  else Docker auto-creates a *directory* in their place. The manifest still crashes
  the service if it is a directory, but the CIS_bdpm/COMPO/GENER/frequency reads are
  now `is_file`-guarded (`load_names` tolerates it; `build_xref_index` /
  `bdpm.column_tokens` degrade to fewer/no backlinks) rather than raising
  IsADirectoryError. `deploy.sh` handles all of them (heals a stray directory, writes
  `{}` for both manifests, `mkdir`s `data/eu`, syncs `data/eu` overlays up/down like
  `data/rcp`, and rsyncs `CIS_bdpm.txt` + the three backlink files since
  the main rsync excludes `/data`). It runs as
  `${REFRESH_UID:-1000}:${REFRESH_GID:-1000}` so
  the overlays and rebuilt pages it writes stay owned by the host user (clean
  ownership + Syncthing). The manifest is bind-mounted as a single file, so it must
  exist on the host before first `up` (run `scrape-rcp.py` once, or
  `touch data/.scrape-manifest.json`), else Docker creates a *directory* in its
  place. `deploy.sh` heals this automatically (removes a stray directory and writes
  `{}` before `up`), and `scrape.load_manifest` now tolerates a directory / empty /
  invalid file (returns `{}`) so a misconfig degrades instead of crash-looping the
  service; but a manual `docker compose up` on a fresh host still needs the file to
  pre-exist. Because the container's `/app/data` is read-only except that one file,
  `scrape.save_manifest` writes it in place when its atomic temp+rename cannot work
  (a `.tmp` sibling is EROFS / renaming onto a bind-mount point is EBUSY); keep that
  fallback. Runtime knobs come from `docker/.env` (`env_file`) as `REFRESH_*` / `RCP_OVERLAY_GZIP`.
  The refresh AND embed images' build context is the repo ROOT (compose `context:
  ..`), so a root `.dockerignore` is REQUIRED to exclude `dist/` (~720M), `data/`
  (~260M) and the semantic-search `models/` (~120M, mounted read-only at runtime, NOT
  COPYd); without it every `up --build` ships ~1GB+ to the daemon and can fail the
  build on a small VPS ("no space left on device"), leaving no containers. The refresh
  Dockerfile bakes `src/build.py` / `src/scrape-rcp.py` / `src/scrape-ema.py` /
  `src/ema_pdf.py` / `src/refresh-service.py` / `src/bdpm.py` / `src/rcp.html` (COPYd
  into `/app/src/`), and pip-installs `pymupdf` (fitz)
  alongside the other deps because `ema_pdf.py` needs it for the EMA PDF conversion.
- **The embed service is a THIRD, separately-hardened container** (compose `embed`,
  `docker/embed.Dockerfile`), same posture as `refresh` (`read_only: true`,
  `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp`, no `ports:`, reached only via
  Caddy's `/api/sem/*` proxy) and run as `${EMBED_UID:-1000}:${EMBED_GID:-1000}` so
  the `.vec.json` it writes stay host-owned. Its ONLY writable mounts are `dist/rcp` +
  `dist/eu` (the sidecars); everything else is read-only: `models` (the encoder, so it
  can stay out of the image + `.dockerignore`-excluded build context) and the crawled
  overlays `data/rcp` + `data/eu` (the only text it embeds; the 2022 baseline CSV is
  not mounted at all). NO manifest mount: staleness is the content hash baked in each
  `.vec.json`. Because the embed service (not `build.py`) writes those sidecars ON THE
  VPS, they are runtime artifacts, NOT build outputs: a deploy must NOT mirror-delete
  them (`deploy.sh`'s main rsync `--exclude='*.vec.json*'` both skips shipping an
  empty/stale local copy AND, since rsync protects excluded paths from `--delete`, keeps
  a normal deploy from wiping the live index). To regenerate them on purpose after a
  segmentation change (see `_CHUNK_FORMAT_VERSION`) OR a model swap (see
  `onnx_embed.RUNTIME_MODEL`), there are TWO paths: `deploy.sh --re-embed` deletes the
  VPS sidecars so the embed service's reconcile sweep re-embeds every crawled page on the
  VPS (grinds through ~15k pages there), OR `deploy.sh --push-vectors` ships vectors
  pre-baked LOCALLY (`uv run src/embed-rcp.py`, same weights so identical vectors) UP to
  the VPS via a dedicated additive rsync (the model-gated reconcile self-heals any page
  you did not bake). Both are gitignored deploy.sh modes; either way, do NOT re-add
  `*.vec.json` to the rsync mirror. The Dockerfile bakes `src/build.py` / `src/bdpm.py` /
  `src/onnx_embed.py` / `src/embed-service.py` / `src/rcp.html` (COPYd into `/app/src/`)
  and pip-installs `onnxruntime` + `tokenizers`
  (NOT torch, so ~300 Mo not ~2 Go). It is fully optional: `up web refresh` omits it
  and the search box degrades to "indisponible". PRIVACY: it embeds the reader's query
  same-origin but logs NO query content (counts/latency only) and drops it right after
  encoding; the operator *could* in principle observe queries where the old browser
  design made that impossible, a deliberate trade for dropping the 120 Mo download.

## Gotchas

- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

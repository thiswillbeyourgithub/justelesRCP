# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

justelesRCP is a fast, ad-free static site serving the **RCP** (résumés des
caractéristiques du produit) of medicines sold in France, sourced from the ANSM
BDPM public dataset. It exists to be a lightweight alternative to slow, for-profit
sites like vidal.fr. The whole thing is precomputed to static files; the only
runtime code is an optional companion refresh service (see the architecture note
below) that re-scrapes a single drug on demand behind a rate limit. Leave it out
and the site is 100% static.

**Language convention:** the website (page text, UI strings) is in French; the
code, comments, and developer docs are in English. `README.md` is the French
readme and `README.en.md` is its English translation. They are cross-linked and
MUST be kept in sync: whenever you edit one, update the other accordingly.

**Versioning:** the project version is `__version__` in `build.py` (single
source of truth, printed at build start). There are no git tags; bump it
patch/minor per change. The version is NOT baked into page HTML: `build.py`
writes `dist/app-version.js` (`window.__APP_VERSION__`) and `src/app-init.js`
injects it into every `[data-app-version]` slot (RCP sidebar, About page, home
and browse footers). This keeps page content independent of the version so a
version-only bump does not invalidate the incremental-build cache (below).

## Architecture (the important part)

Two stages, cleanly separated:

1. **Build** (`build.py`, a `uv` PEP 723 script): reads the source data from
   `./data`, cleans each RCP's ANSM HTML, and writes a fully static site to
   `./dist`. Run with `uv run build.py`. This is where all the work happens.
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
dist/search-index.json       [{cis,name,slug}] consumed by client-side search
                             (+ {cis,name,slug,eu:1} rows for /eu/ pages below)
dist/eu/<cis>-<slug>.html    EU-authorization page for a centrally-authorized drug
                             whose RCP lives at the EMA (empty ANSM cell): a full
                             converted SmPC/notice if data/eu has an overlay, else a
                             lightweight stub pointing to the EMA. noindex, findable
                             via search only, NOT in browse
dist/browse/index.html       A-Z landing (letter grid with counts)
dist/browse/<letter>.html    alphabetical drug list per letter ('#' -> num.html)
dist/index.html a-propos.html style.css search.js
dist/app-config.js app-init.js dev-banner.js toc.js app-version.js  (runtime client assets)
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
  (~15k name entries) once and does substring matching in the browser. There is
  no search API. Full-text search over RCP *content* is intentionally NOT
  supported (that was the tradeoff for a zero-runtime static architecture).
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
- **Each RCP page has a sidebar table of contents.** `clean_rcp()` assigns
  `id="sec-N"` to every top-level `AmmAnnexeTitre1` heading and returns the
  section list; `render_record()` emits a `<details class="toc">` of jump links
  (plus the version slot) into `{{TOC}}`. It is a sticky sidebar on wide screens
  and a collapsible block on phones (see `.rcp-layout`/`.toc` in `style.css`).
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
  edit forces a full rebuild while a version-only bump does not. Full build is
  ~3 min; an unchanged-data rebuild reuses everything in ~35 s.
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
- **The on-demand refresh service is the only runtime component** (`refresh-service.py`,
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
  `REFRESH_QUEUE_MAX` (sheds load as "busy"). The perpetual CRAWLER (worker
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
  shape: `{enabled,total,idx,ttl_days,idle,due,eta_seconds}`, where `due`
  is the still-due page count and `eta_seconds` the sweep ETA = `due` x that lane's
  crawl rate; the crawl/aggregate log lines print both as `sweep-eta` in
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
  `style.css`, and the enhancer in `src/app-init.js`. Directly under the banner,
  `_official_source_html(url, label)` (injected into the same `{{ASOF}}` slot)
  renders a `.rcp-source` button to the authoritative page (ANSM `ANSM_PAGE_URL`
  on `/rcp/`, the EMA on `/eu/` stubs) so a reader who doubts our copy or spots a
  rendering bug can open the official one; it shares the `.official-link` button
  style with the stub's EMA button.
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
  `centralisée` procedure), links to the official RCP via **the exact EMA
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
  routes `search.js` to `/eu/` instead of `/rcp/`), but are `noindex` and kept OUT
  of `/browse`, so they add no thin-content SEO surface; their own `/eu/` URL space
  keeps them OUT of the RCP cross-link graph (`page_cis_from_dist()` globs
  `dist/rcp`, never `dist/eu`, so full build and the refresh service agree on link
  targets); they reuse `src/rcp.html` via a `{{HEADEXTRA}}` slot (the noindex meta
  on stubs, empty on RCP pages) so there is no chrome duplication; and they fold
  into the incremental manifest (stub CIS never collide with RCP CIS: one has an
  empty RCP, the other non-empty), so an unchanged rebuild reuses all of them.
  `src/app-init.js` shows a refresh button on EVERY `/eu/` page: a full page bakes
  `data-rcp-asof` and gets "Rafraîchir maintenant" (+ the >1yr auto-refresh keyed
  off that date), while a stub (which has no `data-rcp-asof`) is detected by its
  `.rcp-stub` body and gets a manual-only "Récupérer le RCP depuis l'EMA" button, so
  a reader never has to wait for the background crawler. Clicking either POSTs
  `/api/refresh/<cis>`; the refresh service's EMA lane resolves the PDF (own,
  sibling, or harvested live off the ANSM page) and converts it, and the button's
  poll reloads into the now-full page. Keep the stub contract in sync across
  `build_stubs`/`load_cap_meta`/`_stub_content`/`_load_ema_links`/`resolve_eu`/
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
  helpers (parameterised by path/dir) rather than re-implementing them. **Internet
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
  It also bakes two source buttons (direct PDF + EMA search) and, when the overlay
  is archive-sourced (`_eu_via_archive`), a warn-tinted note telling the reader the
  text came through a web archive. Keep the /eu/
  full-page contract in sync across `ema_pdf.convert`/`_overlay_html`
  (scrape-ema.py), `render_eu_page`/`_eu_date`/`_eu_fetched`/`_eu_pdf`/`_eu_toc`/
  `_eu_via_archive` (build.py), and `.rcp-eu`/`.ema-annexe`/`figure`/
  `.rcp-archive-note` in `style.css`. The EMA link
  is a search only as a *fallback*; a fetched page links the exact PDF. **NOTHING
  is fetched from the EMA at build time** (the site stays 100% static); the PDF is
  fetched only by `scrape-ema.py` or the refresh service's EMA lane.
- **Precompression (.gz/.br) is baked at build time** so Caddy spends zero CPU
  compressing. `compress()` writes both siblings; the Caddyfile uses
  `precompressed br gzip`.
- **Runtime config is injected, not baked.** Every page loads `/app-config.js`,
  which defines `window.__APP_CONFIG__` (optional umami analytics + a DEV
  banner). `src/app-config.js` is the local-dev fallback (all empty = nothing
  loads). In the container, `docker/entrypoint.sh` renders an equivalent file
  from `docker/.env` into the `/gen` tmpfs and `docker/Caddyfile` serves THAT for
  `/app-config.js`, so the build stays config-free. `src/app-init.js` injects the
  umami tag + a click tracker; `src/dev-banner.js` shows the WIP banner when
  `DEV=1`. Keep the config keys in sync between `src/app-config.js` and the
  heredoc in `docker/entrypoint.sh`.

## Commands

```bash
./download-data.sh        # fetch data/CIS_RCP.csv + data/CIS_bdpm.txt (see TODOs in it)
uv run scrape-rcp.py --limit 60   # OPTIONAL manual/one-time bulk scrape into data/rcp/ overlay
                                  # (the refresh service's perpetual crawler now does routine
                                  #  freshening; use this for a first --all seed or an ad-hoc batch)
                                  # env: RCP_OVERLAY_GZIP (gzip overlays, default on),
                                  # RCP_SCRAPE_RATE_SECONDS (base gap between fetches);
                                  # logs a progress bar + ETA and the trigger (user/timer)
uv run scrape-ema.py --limit 60   # OPTIONAL bulk scrape of EMA product-information PDFs into
                                  # data/eu/ overlays (converted to HTML by ema_pdf.py). Reads the
                                  # ema_pdf links scrape-rcp.py harvested; own manifest
                                  # data/.scrape-ema-manifest.json. The refresh service's EMA crawler
                                  # does routine freshening; use this for a first seed / ad-hoc batch.
                                  # Falls back to the Internet Archive if a live EMA PDF fails (never
                                  # exposes the archive URL; stamps via_archive). --retry-archived
                                  # re-fetches just the archive-sourced CIS against the live EMA.
uv run build.py           # build ./dist from ./data (overlay wins over the 2022 CSV; a data/eu
                          #  overlay makes /eu/<cis> a full converted page instead of a stub)
uv run refresh-service.py # optional: run the refresh API on :8460 (behind Caddy /api/*)
                          # runs TWO perpetual frequency-ordered crawlers (ANSM + EMA /eu/) +
                          #  on-demand button/auto refreshes for both
                          # knobs (env): REFRESH_RATE_SECONDS (default 120), REFRESH_CRAWL (1=on) +
                          # REFRESH_CRAWL_TTL_DAYS (365); EMA lane REFRESH_EMA_CRAWL (1=on) +
                          # REFRESH_EMA_RATE_SECONDS (300) + REFRESH_EMA_CRAWL_TTL_DAYS (180);
                          # REFRESH_LOG_LEVEL (INFO, health never logged);
                          # GET /api/stats returns crawl counters + crawl (ANSM) + crawl_eu gauges
cp docker/env.example docker/.env                      # optional: umami analytics / DEV banner / refresh knobs
docker compose -f docker/docker-compose.yml up -d      # serve ./dist on :8459 + refresh service (read-only, hardened)
docker compose -f docker/docker-compose.yml up --build # after changing Caddyfile/compose/refresh.Dockerfile
                                                       # OR the scripts baked into the refresh image (build.py /
                                                       # scrape-rcp.py / scrape-ema.py / ema_pdf.py /
                                                       # refresh-service.py / bdpm.py / src/rcp.html):
                                                       # a plain `up` reuses the old image and ships stale code
```

To rebuild after a data refresh: re-run `download-data.sh` then `uv run build.py`;
the build is incremental (only changed drugs are re-rendered; see the
`.build-manifest.json` note above), so a rebuild on unchanged data is fast.
Restart is not needed (Caddy reads the mounted dir live), but a
`docker compose -f docker/docker-compose.yml restart web` is harmless.

## Deployment / hardening notes

- Caddy listens on **:8459 plain HTTP**; TLS is expected to be terminated by an
  upstream reverse proxy you already run. There is no ACME/TLS config here.
- The container runs `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, with tmpfs for Caddy's scratch dirs (`/tmp`, `/config`,
  `/data`, and `/gen` for the rendered `app-config.js`). Keep it that way; if
  Caddy needs a new writable path, add a tmpfs mount rather than dropping
  read-only.
- `cap_drop: ALL` is paired with `cap_add: [NET_BIND_SERVICE]`, and that one cap
  must stay. The `caddy:2-alpine` binary ships with `setcap
  cap_net_bind_service=+ep`; the effective bit makes `execve()` fail with EPERM
  ("Operation not permitted", crash loop at the `exec caddy` line of
  entrypoint.sh) if the cap is absent from the bounding set that `cap_drop: ALL`
  empties. We don't bind a privileged port (we listen on 8459), but the binary's
  file cap still has to be satisfiable at exec time. Do not remove it.
- A strict CSP (`default-src 'self'`) is set in the Caddyfile. The site uses no
  external fonts, scripts, or CDNs by design. The ONLY escape hatch is the umami
  origin: `entrypoint.sh` derives `ANALYTICS_ORIGIN` from `ANALYTICS_URL` and the
  Caddyfile adds it to `script-src`/`connect-src` (empty when analytics is off).
  Keep it self-contained otherwise so the CSP holds.
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
  The refresh image's build context is the repo ROOT (compose `context: ..`), so a
  root `.dockerignore` is REQUIRED to exclude `dist/` (~720M) and `data/` (~260M);
  without it every `up --build` ships ~1GB to the daemon and can fail the build on a
  small VPS ("no space left on device"), leaving no containers. The Dockerfile bakes
  `build.py` / `scrape-rcp.py` / `scrape-ema.py` / `ema_pdf.py` / `refresh-service.py`
  / `bdpm.py` / `src/rcp.html`, and pip-installs `pymupdf` (fitz) alongside the other
  deps because `ema_pdf.py` needs it for the EMA PDF conversion.

## Gotchas

- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

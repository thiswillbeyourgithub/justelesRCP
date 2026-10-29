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

**Ending a message about deploying:** whenever a reply tells the user that a change needs deploying (or how), its LAST line must be the exact command to run, with the right `deploy.sh` arguments, e.g. "Deploy with `git push && ./deploy.sh`". Pick the mode from what changed: plain `./deploy.sh` (full build, needed whenever `src/rcp.html`, `build.py` or a script tag on drug pages changed), `--quick` (only static assets, home/a-propos/status, JS/CSS not affecting the drug-page shell; it refuses otherwise), `--rebuild` (also re-crawl the whole catalog after a render change), `--rebuild-web` (after a `docker/web.Dockerfile` change), `--re-embed` / `--push-vectors` (after a chunking or model change, see the embed bullets). Say so explicitly even when it seems obvious. Added by Claude Code.

**Versioning:** the project version is `__version__` in `build.py` (single
source of truth, printed at build start). There are no git tags; bump it
patch/minor per change. A bump MUST come with that version's user-facing release
notes in `docs/changelog/<version>/changelog.md` (same commit): the build refuses
to run without them, see the release-notes bullet below. The version is NOT baked into page HTML: `build.py`
writes `dist/app-version.js` (`window.__APP_VERSION__`) and `src/app-init.js`
injects it into every `[data-app-version]` slot (RCP sidebar, About page, home
and browse footers). This keeps page content independent of the version so a
version-only bump does not invalidate the incremental-build cache (below).

## Architecture (the important part)

**Repository layout:** ALL Python lives in `src/` (the same directory as the
frontend templates/assets it renders), NOT the repo root: `src/build.py`,
`src/bdpm.py`, `src/ema_pdf.py`, `src/onnx_embed.py`, `src/scrape-rcp.py`,
`src/scrape-ema.py`, `src/refresh-service.py`, `src/embed-service.py`,
`src/embed-rcp.py`, `src/svc_http.py` (the shared HTTP layer of the two services:
`JSONHandler`, `QuietHTTPServer`, `setup_logging`, plus `load_sibling`, the by-path
importer of the hyphenated scripts, which caches each sibling in `sys.modules` so two
importers share one instance), plus the tests `src/test_embed.py` / `src/test_ema_seed.py`.
They stay siblings so `import bdpm` / `import svc_http` and the by-path service imports
keep resolving; each anchors the repo root as `Path(__file__).resolve().parent.parent`
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
dist/search-index.json       [{cis,name,slug[,sub][,ret]}] consumed by client-side search
                             (sub = active-substance/DCI string, present when known and
                             not already in the name, so search matches by substance too;
                             ret:1 = drug delisted from BDPM, search.js appends " [RETIRÉ]")
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
dist/status.html status.js  noindex live ops dashboard (crawl/embed/query stats via
                            the curated /api/summary + /api/sem/summary endpoints)
dist/logo.svg               site logo (SVG favicon on every page + README header)
dist/og.png                 raster social card for og:image / twitter:image (1200x630)
dist/manifest.webmanifest icon-192.png icon-512.png apple-touch-icon.png  makes the site installable as an app
dist/app-config.js app-init.js dev-banner.js dock.js app-version.js rcp-semsearch.js  (runtime client assets)
dist/rcp/<cis>-<slug>.vec.json   OPTIONAL per-page section vectors for server-side
dist/eu/<cis>-<slug>.vec.json    per-drug semantic search, written DIRECTLY by
                             embed-service.py (runtime) or embed-rcp.py (offline
                             pre-bake) for CRAWLED pages only; build.py only prunes
                             orphans. Absent unless one of them has run
dist/.build-manifest.json    incremental-build cache (per-CIS input hashes)
+ a .br precompressed sibling for every other text file (Caddy serves it; no .gz);
  dist/rcp + dist/eu pages and .vec.json are stored as the .br ONLY (no plain copy)
dist/eu/img/<hash>.png|jpg|gif   /eu/ page figures, one file per distinct image (see the
dist/rcp/img/<hash>.png|jpg|gif  EMA conversion bullet; /rcp/ ones come from scrape-rcp's
                             inline_images), not precompressed
```

Docker lives under `docker/` (compose, Caddyfile, entrypoint.sh, env.example).
Run compose with `-f docker/docker-compose.yml`; paths inside are relative to it
(`../dist` is the web root).

Key facts that aren't obvious from a single file:

- **Search and browse** ([docs/architecture/search.md](docs/architecture/search.md)): A-Z browse pages are static and crawlable; search is 100% client-side over `search-index.json` (row flags `sub`/`ret`/`aip`/`eu`); the dropdown and the shareable `/?q=` results page share `search()`/`resultItem()`. No search API and no full-text search over RCP content, by design.
- **Build pipeline and RCP rendering** ([docs/architecture/build-and-rendering.md](docs/architecture/build-and-rendering.md)): names from `CIS_bdpm.txt`; cross-drug backlinks (`bdpm.py` is shared with the scraper and must stay pure stdlib; links only target CIS that render a page); the `Amm*` class hooks shared with `style.css`; the 2022 mojibake repair and the ANSM HTML quirks fixed in `_parse_clean`; the incremental build (`_global_key`, `_record_hash`, failed renders keep their old page); ANSM overlays from `scrape-rcp.py` win over the frozen 2022 CSV, and a zero-byte overlay means delisted (archived page).
- **Drug page chrome** ([docs/architecture/drug-page.md](docs/architecture/drug-page.md)): the Sommaire (ToC) and the sticky dock (`src/dock.js` owns every offset through `--chrome-off`; never give `.toc` or `.semsearch` their own `position: sticky`). CRITICAL: only top-level headings get the `sec-N` ids, deeper ones get `sub-N`, because the semantic-search vectors anchor to `sec-N`. Also the freshness banner (`data-rcp-ansm` headline; `data-rcp-asof` drives the refresh and must stay), the official source button and the "En savoir plus" pill row (top and bottom).
- **Refresh service** ([docs/architecture/refresh-service.md](docs/architecture/refresh-service.md)): optional runtime component behind `/api/*`; imports `scrape-rcp.py`/`build.py`/`scrape-ema.py` by path, so those must stay import-safe; on-demand and crawler lanes for ANSM and EMA with separate rate limits, a catalog-wide hourly budget, SIGHUP forced re-crawl (`deploy.sh --rebuild`); the archived/delisted contract (`build.rcp_archived` is the single test). Never call `asof_of()` while holding `self._lock` (deadlock).
- **Release notes, /a-propos, /status** ([docs/architecture/release-notes-and-status.md](docs/architecture/release-notes-and-status.md)): every `__version__` bump needs `docs/changelog/<version>/changelog.md` (the build refuses otherwise); `src/changelog.js` owns the generic modal frame `window.jlrcpModal`; `/status` reads only the curated public `/api/summary` + `/api/sem/summary`.
- **EU-authorization pages and EMA conversion** ([docs/architecture/eu-pages.md](docs/architecture/eu-pages.md)): ~15% of CIS have an empty RCP; the centrally-authorized ones get an `/eu/` page, a stub or a full SmPC converted from the EMA PDF (`ema_pdf.py`, `scrape-ema.py`); overlays are shared by authorization group (`resolve_eu`); figures go to content-addressed `dist/<eu|rcp>/img/`; every EMA URL passes the SSRF guard `bdpm.is_ema_pdf_url`; nothing is fetched from the EMA at build time.
- **Per-drug semantic search** ([docs/architecture/semantic-search.md](docs/architecture/semantic-search.md)): server-side embed service behind `/api/sem/*`, crawled overlays only, writes `.vec.json` sidecars; `build.section_chunks` is the single segmentation source; a segmentation change bumps `_CHUNK_FORMAT_VERSION` (keep the old one in `_CHUNK_FORMAT_LEGACY`); changing `RUNTIME_MODEL`, `EMBED_OUT_DIM` or `EMBED_VEC_QUANT` re-embeds the catalog; the reader's query is never logged nor sent to analytics.
- **Precompression (.br only) is baked at build time** so Caddy spends zero CPU
  compressing. `compress()` writes the `.br` sibling and deletes any stale `.gz` one;
  the Caddyfile uses `precompressed br`, and its `encode zstd gzip` covers the rare
  client without brotli on the fly. The `.gz` siblings were dropped in 0.56.0: they
  cost ~1.8 GB of disk on the VPS for almost no reader. Do not bring them back. `deploy.sh`
  (local, gitignored) lists and offers (y/N) to delete what the VPS keeps but never
  uses: leftover `dist/**/*.gz` (the `.vec.json.gz` ones sit outside the rsync mirror),
  and `data/CIS_RCP.csv`/`.zip`/`data/emb`, which no container mounts.
  **The bulk (`dist/rcp` + `dist/eu` pages and `.vec.json`, ~45k files) is stored as
  the `.br` ONLY, with NO plain copy** (since 0.57.0; the plain copies were ~2.6 GB
  that almost no reader downloads). `precompressed` cannot serve those (Caddy stats
  the plain original first and 404s without it), so the Caddyfile gives them two
  routes before the generic `handle`: `@br_only` (client sends `br` and a `.br`
  exists) rewrites to the `.br` and sets `Content-Encoding: br` + the Content-Type
  per URL (`encode` leaves an already-encoded response alone), and `@br_fallback`
  (any other client, no plain file) rewrites to `/api/plain/<rcp|eu>/<file>.br` on
  the refresh service, which decompresses via `build.plain_for` (strict path regex)
  and lets `encode` gzip it. So a non-br client (curl, some bots) needs the refresh
  service; a browser never does. In Python, callers keep the LOGICAL path
  (`<slug>.html`, `<slug>.vec.json`) and go through `write_served`/`read_served`/
  `served_mtime`/`served_pages`/`delete_served`/`br_path`, never `read_text`/`glob
  ("*.html")` on those dirs; each also accepts a legacy plain file, and
  `drop_plain_copies` (run by the build and at embed-service startup, which owns the
  VPS's vectors) removes a plain/`.gz` copy that has a `.br`. Top-level assets, browse
  pages and `/eu/img/` + `/rcp/img/` stay plain (+ `.br` via `compress()`). Keep the contract in
  sync across those helpers + `plain_for`/`_prune_pages` (build.py), `_send_plain`
  (refresh-service.py), `Embedder.start` (embed-service.py), the `@br_only`/
  `@br_fallback` routes in `docker/Caddyfile`, and
  `test_br_only_pages_are_listed_pruned_and_served_plain`.
- **The site as text for AI assistants (`/llms.txt`, `/md/...`) is NOT stored.**
  `src/llm_pages.py`, imported by the refresh service, converts a served `/rcp/` or
  `/eu/` page to Markdown on each request (`read_served` then lxml), and lists the
  served pages for `/md/browse[/<letter>]` from the directories (cached on their
  mtimes). Caddy's `@llm` route rewrites `/llms.txt` and `/md/*` to `/api{path}` in
  its own rate zone (`MD_RATE_EVENTS`/`MD_RATE_WINDOW`). Deliberate: a stored `.md`
  would have joined every br-only rule above, and an edit to build.py re-renders the
  whole site, which llm_pages.py does not. The public paths sit outside `/api/` because
  `robots.txt` disallows it and assistants fetching for a person (Claude-User &co)
  honour that; the service sends `X-Robots-Tag: noindex` so search engines skip them,
  and they stay out of the sitemap. The `/md/` path is the page path with `/md` in
  front; keep `llm_pages.MD_PAGE_RE` as strict as `_PLAIN_RE`. Tests:
  `test_markdown_*` in `src/test_embed.py`. Built with Claude Code.
  Each answer is counted in umami server side (`src/umami.py`, a daemon thread per
  send, path only, no IP), because an assistant runs no script; it reuses
  `ANALYTICS_URL`/`ANALYTICS_WEBSITE_ID`, and umami must set `DISABLE_BOT_CHECK`
  or drops every such event. `src/umami.py` is in refresh.Dockerfile's COPY and in
  deploy.sh's `BAKED_SOURCES`; a new module the refresh service imports needs both.
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
  `{{BREADCRUMB}}` in all three render paths, which all fill the template through ONE
  `_fill_page(tpl, title=, description=, head_extra=, breadcrumb=, cis=, toc=, asof=,
  content=, xref=, more_bottom=)` (a new `src/rcp.html` slot is a new REQUIRED keyword
  there, and `test_every_drug_page_path_fills_every_template_slot` checks the two match);
  the hand-written `src/index.html`,
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
  are copied to `dist/` by `main()` (`og.png` uncompressed). So are
  `src/manifest.webmanifest` and the icons it and iOS ask for (`icon-192.png`,
  `icon-512.png`, `apple-touch-icon.png` at 180), linked from every page head, which
  make the site installable as an app; the icons are rendered from the logo with
  `rsvg-convert` and committed (the command is in `build.py`). No service worker: no
  current browser needs one to install. Redraw the icons whenever the logo changes.
- **Shared client helpers live in `src/util.js`** (`window.jlrcp = {el, track, lsGet,
  lsSet, lsDel, fold, frDate, ago}`: DOM element factory, guarded analytics forward,
  storage that degrades in private mode, the accent-fold search and the semantic search
  share, the UTC French date, the "il y a X" age). It is the FIRST deferred script in all
  five `src/*.html` (deferred scripts run in document order, so every later one can use
  it at load time) and is in `static_assets`. Do not re-add local copies of these in a
  script. `theme.js` is the one exception for its first-paint read: it runs
  synchronously in `<head>`, before `util.js`. Because the drug-page template and the
  browse pages carry the `<script>` tag, a change here needs a FULL build, never
  `deploy.sh --quick` alone (old pages would load the new scripts without `util.js`).
  That is exactly what took semantic search down on 2026-10-03, silently, despite this note,
  so it is now enforced: a full build stamps `dist/rcp/.shell-key` (`build.shell_key`: the
  template plus the build code) and `--quick` refuses unless the VPS's stamp equals
  `build.py --shell-key`. `rcp-semsearch.js` also writes any crash of its own into the box.
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
  registered in `build.py`'s `static_assets` tuple so it is copied + `.br`
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
  Triggers: auto on first landing visit (`localStorage['jlrcp_tour_seen']`, but NOT
  when the URL carries a `q`/`query` search term: a reader following a shared results
  link must not get a modal over it, see the results-page bullet), `?tour=1`
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
./scripts/download-model.sh       # OPTIONAL: fetch the jina-embeddings-v5-text-small-retrieval fp32 ONNX
                          #  + tokenizer into ./models and quantise it to int8 (~650 Mo kept,
                          #  ~2.4 Go downloaded, gitignored) for SERVER-SIDE per-drug semantic search
                          #  (mounted read-only
                          #  into the embed container; no longer served to browsers). Skip it and the
                          #  "Recherche sémantique dans ce RCP" box just degrades to "indisponible".
uv run src/scrape-rcp.py --limit 60   # OPTIONAL manual/one-time bulk scrape into data/rcp/ overlay
                                  # (the refresh service's perpetual crawler now does routine
                                  #  freshening; use this for a first --all seed or an ad-hoc batch)
                                  # env: RCP_OVERLAY_GZIP (gzip overlays, default on),
                                  # RCP_SCRAPE_RATE_SECONDS (base gap between fetches);
                                  # logs a progress bar + ETA and the trigger (user/timer)
                                  # --missing-images re-scrapes only the overlays whose figures
                                  #  are not inlined yet (see the EMA conversion bullet)
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
                                  # RESUMABLE: a no-encode pre-scan (~1 min) keeps only STALE pages
                                  # (build.vec_is_current, the gate embed_page_to_vec uses), so
                                  # --limit N = the next N stale pages and --all = every stale page;
                                  # stop/re-run freely. Only --force re-embeds up-to-date pages.
                                  # Needs ./scripts/download-model.sh's model + a prior `uv run src/build.py`.
                                  # Shows a tqdm bar (overlay order is SHUFFLED so long /eu/ + short /rcp/
                                  # pages interleave and the ETA is representative). Depends on
                                  # onnxruntime-GPU (not the VPS's CPU-only onnxruntime): --gpu (default)
                                  # uses a local CUDA/ROCm GPU if present, else falls back to CPU (--no-gpu
                                  # forces CPU); --batch-size 128 feeds a GPU better. A GPU needs CUDA 12 +
                                  # cuDNN 9; if "libcudnn.so.9" is missing, supply the wheels no-root via
                                  # `uv run --with nvidia-cudnn-cu12 --with nvidia-cublas-cu12 --with
                                  # nvidia-cuda-runtime-cu12 --with nvidia-cufft-cu12 --with
                                  # nvidia-curand-cu12 src/embed-rcp.py ...` (_preload_cuda_libs makes ort
                                  # find them) or install cuDNN 9 system-wide. GPU-baked vectors share the
                                  # src_hash+model gate, so they are accepted as-is (no VPS re-embed) and
                                  # pushed via deploy.sh --push-vectors.
uv run src/build.py           # build ./dist from ./data (overlay wins over the 2022 CSV; a data/eu
                          #  overlay makes /eu/<cis> a full converted page instead of a stub). Does NOT
                          #  bake vectors anymore: the embed service / embed-rcp.py write .vec.json
                          #  directly; build.py only prunes orphan .vec.json when a slug is dropped.
uv run src/build.py --shell-key   # what a full build would stamp into dist/rcp/.shell-key now;
                          #  deploy.sh --quick refuses when the VPS's stamp differs.
uv run src/build.py --assets-only  # rewrite ONLY the static assets (home, /a-propos, /status
                          #  pages, JS, CSS, app-version.js, changelog.json; still runs the changelog
                          #  gate) in under a second, no drug page touched. Used by `deploy.sh --quick`
                          #  (local, gitignored), which also skips the overlay down-sync, excludes
                          #  dist/rcp + dist/eu from the mirror, but still rebuilds the refresh + embed
                          #  images. Only valid when nothing that RENDERS a drug page changed; note any
                          #  build.py edit busts `_global_key`, so the next full build re-renders all.
uv run src/refresh-service.py # optional: run the refresh API on :8460 (behind Caddy /api/*)
                          # runs TWO perpetual frequency-ordered crawlers (ANSM + EMA /eu/) +
                          #  on-demand button/auto refreshes for both; pings the embed service
                          #  (EMBED_NOTIFY_URL) after each fresh overlay so it re-embeds the page
                          # knobs (env): REFRESH_RATE_SECONDS (default 120), REFRESH_CRAWL (1=on) +
                          # REFRESH_CRAWL_TTL_DAYS (365); EMA lane REFRESH_EMA_CRAWL (1=on) +
                          # REFRESH_EMA_RATE_SECONDS (300) + REFRESH_EMA_CRAWL_TTL_DAYS (180);
                          # REFRESH_LOG_LEVEL (INFO, health never logged);
                          # GET /api/stats returns crawl counters + crawl (ANSM) + crawl_eu gauges
                          #  (INTERNAL, blocked at the edge); GET /api/summary is the curated
                          #  PUBLIC view the /status page consumes (proxied, rate-limited)
uv run src/embed-service.py   # optional: warm SERVER-SIDE embedder on :8461 (behind Caddy /api/sem/*)
                          # keeps the ONNX encoder warm; embeds the reader's query on request
                          #  (no browser model) + (re-)embeds each CRAWLED page's sections in the
                          #  background, writing dist/<rcp|eu>/<slug>.vec.json (never the 2022 baseline)
                          # knobs (env): EMBED_ENABLE (backlog sweep 1=on), EMBED_INTRA_THREADS (-2),
                          # EMBED_BACKLOG_RATE_SECONDS (0), EMBED_RECONCILE_SECONDS (60), EMBED_QUEUE_MAX
                          #  (500), EMBED_MAX_CONCURRENT_QUERIES (8, bounds encode CPU; per-IP rate limit
                          #  belongs at the proxy), EMBED_MIN/MAX_QUERY_CHARS (5/400), EMBED_QUERY_CACHE
                          #  (256) + EMBED_QUERY_CACHE_TTL_SECONDS (60, bounds query-data retention; 0=off),
                          #  EMBED_MODEL_DIR, EMBED_OUT_DIM (MRL width, 1024; change re-embeds all),
                          #  EMBED_BATCH_SIZE (8, bounds a page batch's peak RAM) + EMBED_CPU_ARENA
                          #  (0: return each batch's memory to the system, see the memory note),
                          #  EMBED_VEC_QUANT (passage quant, int8|binary; change re-embeds all),
                          #  REFRESH_TRIGGER_URL (baseline auto-crawl), EMBED_LOG_LEVEL;
                          # GET /api/sem/stats (INTERNAL, blocked at the edge) + GET
                          #  /api/sem/summary (curated PUBLIC view for /status); query
                          #  CONTENT is never logged (privacy)
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
  `SEM_RATE_EVENTS`/`SEM_RATE_WINDOW` (env, read by Caddy). The `@br_fallback` route (a non-brotli client asking for a br-only `/rcp/`/`/eu/` page, which the refresh service decompresses) has its own per-IP zone too, `PLAIN_RATE_EVENTS`/`PLAIN_RATE_WINDOW` (default 60/1m), since the catalog is enumerable via the sitemap. All three routes share one Caddyfile snippet, `(api_limits)` (`import api_limits <zone> <events> <window>`), which applies the per-IP `rate_limit` zone plus a
  `request_body { max_size 16KB }` (reject oversized POSTs at the edge); add a new service route through it rather than copying the blocks. The two
  **detailed** stats endpoints (`/api/stats`, `/api/sem/stats`, which expose raw queue
  internals + host RAM) are **blocked** (`respond 404`, exact-path `handle` before the
  wildcards) so they are reachable only from inside the docker network, not publicly.
  The **public** `/status` page instead consumes the curated `/api/summary` +
  `/api/sem/summary` endpoints (progress %, counts, ETA, uptime; NO host internals),
  which fall through to the wildcard proxies and are rate-limited like any other API
  call (see the /status bullet in the architecture section). These edge limits COMPLEMENT
  the refresh service's global hourly scrape ceiling (`REFRESH_DEMAND_HOURLY_MAX`,
  which caps the aggregate) and its per-CIS min-interval floor; keep all three.
  The `order rate_limit before reverse_proxy` global option is required (the plugin
  directive is non-standard). `web.Dockerfile` pins both the Caddy release
  (`CADDY_VERSION`, 2.11.7) and the plugin (`caddy-ratelimit`, at a commit past v0.1.0
  for `ipv6_prefix 64`: every zone counts an IPv6 visitor per /64), so a rebuild is
  reproducible; bump them together on purpose. A web image predating that commit
  refuses the Caddyfile, so the first deploy after it needs `--rebuild-web`. The refresh/embed images pin their
  Python base (`python:3.14.9-slim`) and their direct pip deps the same way. **The `web` image is ONLY that binary**:
  the Caddyfile, `entrypoint.sh` and `../dist` are all bind mounts, so a Caddyfile
  change needs a `restart`/`up --force-recreate`, NOT a rebuild. Rebuild it (`up
  --build web`, or `deploy.sh --rebuild-web`) only after a `web.Dockerfile` change or
  to pick up a newer Caddy/plugin. That distinction matters because the xcaddy build
  is the ONE step needing outbound internet on the deploy host (Go module proxy), and
  it fails on a VPS whose Docker bridge advertises IPv6 without a route ("dial tcp
  [2a00:…]:443: network is unreachable"); rebuilding it on every deploy also re-ran
  silently whenever upstream published a new `caddy:2-builder-alpine` (cache bust).
  So `deploy.sh` builds `refresh embed` explicitly and leaves `web` alone by default;
  `web.Dockerfile`'s header documents the two escape hatches (`network: host` on the
  build, or build elsewhere and `docker save | ssh … docker load`). Keep the contract
  in sync across
  `docker/web.Dockerfile`, the global block + the `(api_limits)` snippet + the `/api/*`, `/api/sem/*` and `@br_fallback`
  `handle`s in `docker/Caddyfile`, the `web` `build:` in `docker/docker-compose.yml`,
  and the rate-limit knobs in `docker/env.example`.
- The container posture shared by all three services (`restart`, `env_file`, `read_only`, `cap_drop`, `security_opt`, `logging`) lives once in the `x-hardened` YAML anchor of `docker/docker-compose.yml`, merged into each with `<<: *hardened` (healthcheck cadence likewise via `x-healthcheck-timing`); a new service should merge it too. The container runs `read_only: true`, `cap_drop: ALL`,
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
- A strict CSP (`default-src 'self'`, plus `form-action 'self'; object-src 'none'`, which do not inherit from it) and a `Permissions-Policy` denying every device API (camera, microphone, geolocation, ...: no page uses one) are set in the Caddyfile. The site uses no
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
  `src/ema_pdf.py` / `src/refresh-service.py` / `src/bdpm.py` / `src/svc_http.py` / `src/rcp.html` (COPYd
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
  `src/onnx_embed.py` / `src/embed-service.py` / `src/svc_http.py` / `src/rcp.html` (COPYd into `/app/src/`)
  and pip-installs `onnxruntime` + `tokenizers` (pinned: onnxruntime 1.31.0, whose int8 CPU vectors were
  bit-identical to 1.30.0's on 2026-10-22, so the bump re-embedded nothing; the
  PEP 723 headers of `embed-service.py`/`onnx_embed.py` pin the same version, and
  `embed-rcp.py`'s `onnxruntime-gpu` MUST match it, so pre-baked and served vectors
  come from the same runtime: bump all of them together)
  (NOT torch, so ~300 Mo not ~2 Go). It is fully optional: `up web refresh` omits it
  and the search box degrades to "indisponible". PRIVACY: it embeds the reader's query
  same-origin but logs NO query content (counts/latency only) and drops it right after
  encoding; the operator *could* in principle observe queries where the old browser
  design made that impossible, a deliberate trade for dropping the 120 Mo download.

- **Every container has a hard RAM ceiling and a kill priority** (`mem_limit` =
  `memswap_limit`, plus `oom_score_adj` in `docker/docker-compose.yml`): web 256m / -500,
  refresh 1536m / 500, embed 2g / 800, the sizes overridable as `WEB_MEM_LIMIT` /
  `REFRESH_MEM_LIMIT` / `EMBED_MEM_LIMIT` in `docker/.env` (compose interpolation, like
  `REFRESH_UID`). Why: the VPS has no swap, and when RAM ran out it did not OOM-kill
  anything, it thrashed (the kernel evicts executable pages, sshd included) until nothing
  answered. A container now dies inside its own cgroup and `restart:` brings it back.
  `oom_score_adj` is host-global, not relative to this compose file. Each also has a `pids_limit` (threads count: web 128, refresh 256, embed 256, sized for onnxruntime's per-core threads plus one thread per in-flight request) so a runaway cannot exhaust the host PID table. The embed service
  is the one that grows: onnxruntime's CPU arena kept every batch's peak forever (3.8 GB
  resident at batch 32 measured locally), so the service now runs with the arena OFF
  (`Encoder(cpu_arena=False)`, `EMBED_CPU_ARENA`, ~630 MB between pages) and a batch of 8
  (`EMBED_BATCH_SIZE`, peak ~1.05 GB against 2.3 GB at 32, ~25% slower page embedding).
  The offline `embed-rcp.py` keeps the arena (speed, and it is not long-lived). `batch_size` bounds the row count, not the cost: a batch is also capped at `onnx_embed.BATCH_CHAR_BUDGET` (rows x longest-row-chars squared, via `_batch_bounds`), because one page's 46 linearised table rows of ~3300 chars peaked at 3.6 GB in a batch of 8 and OOM-killed the embed container in a restart loop that embedded nothing for days (1.3 GB with the budget). The int8 graph quantises activations per batch, so the split moves those long rows' vectors slightly (cosine ~0.97); nothing re-embeds over it. Keep in
  sync across the three services in `docker/docker-compose.yml`, the memory section +
  the two embed knobs in `docker/env.example`, `cpu_arena` in `onnx_embed.Encoder`, and
  `--batch-size`/`--cpu-arena`/`BATCH_DEFAULT` in `embed-service.py`.

## Gotchas

- **A JavaScript syntax error does not fail anything: the page loads and its script silently never runs** (dead buttons, missing dynamic text). The sister site psychiatheque (served by `../justelesdocs`) shipped exactly that on 2026-10-01 (a stray `});` left by a removed listener) past its own `node --check`, because `node --check <path>` on a `.js` file guesses the module type and the guess passed the broken file. `scripts/check_js.mjs` parses every tracked `src/*.js` as a classic script with `vm.Script` instead, and runs from `.githooks/pre-push` (activate per clone: `git config core.hooksPath .githooks`) and from `deploy.sh`'s pre-flight, even under `DEPLOY_ALLOW_DIRTY=1`. It is a quick check, never worth skipping. Added by Claude Code.
- **The `embed` service is shared with the sibling project `../justelesrecos`, which ships no encoder of its own** (one copy of a ~1 GB model is all the VPS has room for). It reaches this one by container name over a Docker network called `justeles-embed`, declared `external: true` in both compose files, so nothing is published on the host. Removing that `networks:` block, or renaming the `justelesrcp-embed` container, takes the other site's search down with a 502 on every query while every container stays up and healthy. Two host-based routes were tried first and cannot work: a port published on 127.0.0.1 is unreachable from another container, and compose's `ports:` accepts an IP and never a hostname, so `host.docker.internal` there is rejected at parse time.
- `docker-compose.yml` sets `name: justelesrcp` for the same class of reason: without it Compose names the project after the directory holding the compose file, which is `docker/` here AND in `../justelesrecos`, and since both projects call their Caddy service `web`, deploying either one replaced the other's web container.
- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

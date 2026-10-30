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

**Ending a message about deploying:** whenever a reply tells the user that a change needs deploying (or how), its LAST line must be the exact command to run, with the right `deploy.sh` arguments, e.g. "Deploy with `git push && ./deploy.sh`". Pick the mode from what changed: plain `./deploy.sh` (full build, needed whenever `src/rcp.html`, `build.py` or a script tag on drug pages changed), `--quick` (only static assets, home/a-propos/status, JS/CSS not affecting the drug-page shell; it refuses otherwise), `--rebuild` (also re-crawl the whole catalog after a render change), `--rebuild-web` (after a `docker/web.Dockerfile` change), `--re-embed` / `--push-vectors` (after a chunking or model change, see the embed bullets in [docs/architecture/semantic-search.md](docs/architecture/semantic-search.md)). Say so explicitly even when it seems obvious. Added by Claude Code.

**Versioning:** the project version is `__version__` in `build.py` (single
source of truth, printed at build start). There are no git tags; bump it
patch/minor per change. A bump MUST come with that version's user-facing release
notes in `docs/changelog/<version>/changelog.md` (same commit): the build refuses
to run without them, see the release-notes bullet in [docs/architecture/release-notes-and-status.md](docs/architecture/release-notes-and-status.md). The version is NOT baked into page HTML: `build.py`
writes `dist/app-version.js` (`window.__APP_VERSION__`) and `src/app-init.js`
injects it into every `[data-app-version]` slot (RCP sidebar, About page, home
and browse footers). This keeps page content independent of the version so a
version-only bump does not invalidate the incremental-build cache (see [docs/architecture/build-and-rendering.md](docs/architecture/build-and-rendering.md)).

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
this file and in docs/architecture/ the scripts are still referred to by their bare name (`build.py`, etc.);
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
                             JSON-LD (see docs/architecture/serving-and-seo.md). Origin = SITE_URL constant
dist/index.html a-propos.html style.css search.js
dist/status.html status.js  noindex live ops dashboard (crawl/embed/query stats via
                            the curated /api/summary + /api/sem/summary endpoints)
dist/logo.svg               site logo (SVG favicon on every page + README header)
dist/og.png                 raster social card for og:image / twitter:image (1200x630)
dist/manifest.webmanifest icon-192.png icon-512.png icon-maskable-192.png icon-maskable-512.png apple-touch-icon.png  makes the site installable as an app
dist/app-config.js app-init.js dev-banner.js dock.js app-version.js rcp-semsearch.js  (runtime client assets)
dist/rcp/<cis>-<slug>.vec.json   OPTIONAL per-page section vectors for server-side
dist/eu/<cis>-<slug>.vec.json    per-drug semantic search, written DIRECTLY by
                             embed-service.py (runtime) or embed-rcp.py (offline
                             pre-bake) for CRAWLED pages only; build.py only prunes
                             orphans. Absent unless one of them has run
dist/.build-manifest.json    incremental-build cache (per-CIS input hashes)
+ a .br precompressed sibling for every other text file (Caddy serves it; no .gz);
  dist/rcp + dist/eu pages and .vec.json are stored as the .br ONLY (no plain copy)
dist/eu/img/<hash>.png|jpg|gif   /eu/ page figures, one file per distinct image (see
dist/rcp/img/<hash>.png|jpg|gif  docs/architecture/eu-pages.md; /rcp/ ones come from scrape-rcp's
                             inline_images), not precompressed
```

Docker lives under `docker/` (compose, Caddyfile, entrypoint.sh, env.example).
Run compose with `-f docker/docker-compose.yml`; paths inside are relative to it
(`../dist` is the web root).

Key facts that aren't obvious from a single file. Each area's full notes (the contracts, the "keep in sync across" lists, the history of past bugs) live in `docs/architecture/<area>.md`: read the relevant one before changing that area. When you add or change a feature, document it THERE, and only add to this file a short pointer or an invariant that every session must know. This file used to be ~1700 lines; keep it short.

- **Search and browse** ([docs/architecture/search.md](docs/architecture/search.md)): A-Z browse pages are static and crawlable; search is 100% client-side over `search-index.json` (row flags `sub`/`ret`/`aip`/`eu`); the dropdown and the shareable `/?q=` results page share `search()`/`resultItem()`. No search API and no full-text search over RCP content, by design.
- **Build pipeline and RCP rendering** ([docs/architecture/build-and-rendering.md](docs/architecture/build-and-rendering.md)): names from `CIS_bdpm.txt`; cross-drug backlinks (`bdpm.py` is shared with the scraper and must stay pure stdlib; links only target CIS that render a page); the `Amm*` class hooks shared with `style.css`; the 2022 mojibake repair and the ANSM HTML quirks fixed in `_parse_clean`; the incremental build (`_global_key`, `_record_hash`, failed renders keep their old page); ANSM overlays from `scrape-rcp.py` win over the frozen 2022 CSV, and a zero-byte overlay means delisted (archived page).
- **Drug page chrome** ([docs/architecture/drug-page.md](docs/architecture/drug-page.md)): the Sommaire (ToC) and the sticky dock (`src/dock.js` owns every offset through `--chrome-off`; never give `.toc` or `.semsearch` their own `position: sticky`). CRITICAL: only top-level headings get the `sec-N` ids, deeper ones get `sub-N`, because the semantic-search vectors anchor to `sec-N`. Also the freshness banner (`data-rcp-ansm` headline; `data-rcp-asof` drives the refresh and must stay), the official source button and the "En savoir plus" pill row (top and bottom).
- **Refresh service** ([docs/architecture/refresh-service.md](docs/architecture/refresh-service.md)): optional runtime component behind `/api/*`; imports `scrape-rcp.py`/`build.py`/`scrape-ema.py` by path, so those must stay import-safe; on-demand and crawler lanes for ANSM and EMA with separate rate limits, a catalog-wide hourly budget, SIGHUP forced re-crawl (`deploy.sh --rebuild`); the archived/delisted contract (`build.rcp_archived` is the single test). Never call `asof_of()` while holding `self._lock` (deadlock).
- **Release notes, /a-propos, /status** ([docs/architecture/release-notes-and-status.md](docs/architecture/release-notes-and-status.md)): every `__version__` bump needs `docs/changelog/<version>/changelog.md` (the build refuses otherwise); `src/changelog.js` owns the generic modal frame `window.jlrcpModal`; `/status` reads only the curated public `/api/summary` + `/api/sem/summary`.
- **EU-authorization pages and EMA conversion** ([docs/architecture/eu-pages.md](docs/architecture/eu-pages.md)): ~15% of CIS have an empty RCP; the centrally-authorized ones get an `/eu/` page, a stub or a full SmPC converted from the EMA PDF (`ema_pdf.py`, `scrape-ema.py`); overlays are shared by authorization group (`resolve_eu`); figures go to content-addressed `dist/<eu|rcp>/img/`; every EMA URL passes the SSRF guard `bdpm.is_ema_pdf_url`; nothing is fetched from the EMA at build time.
- **Per-drug semantic search** ([docs/architecture/semantic-search.md](docs/architecture/semantic-search.md)): server-side embed service behind `/api/sem/*`, crawled overlays only, writes `.vec.json` sidecars; `build.section_chunks` is the single segmentation source; a segmentation change bumps `_CHUNK_FORMAT_VERSION` (keep the old one in `_CHUNK_FORMAT_LEGACY`); changing `RUNTIME_MODEL`, `EMBED_OUT_DIM` or `EMBED_VEC_QUANT` re-embeds the catalog; the reader's query is never logged nor sent to analytics. Each chunk records its blocks (`"b"`, `build._BlockIndex`, which MUST number blocks exactly like `sectionBlocks()`/`collectLeaves()` in `rcp-semsearch.js`) and the browser highlights those instead of searching for the text; `uv run src/audit-locate.py --sample N` checks every chunk lands on its passage (deploy.sh runs it). Added by Claude Code.
- **Serving, AI text, SEO** ([docs/architecture/serving-and-seo.md](docs/architecture/serving-and-seo.md)): `dist/rcp` + `dist/eu` pages and `.vec.json` are stored as `.br` ONLY, so Python goes through `write_served`/`read_served`/`served_pages`/... and never `read_text`/`glob("*.html")` there; never bring the `.gz` siblings back; `/llms.txt` + `/md/` are generated per request, never stored; all SEO derives from the one `SITE_URL` constant and every drug page is filled through `_fill_page`.
- **Client-side scripts** ([docs/architecture/frontend.md](docs/architecture/frontend.md)): shared helpers in `src/util.js`, loaded first by every page (changing a page's script tags needs a FULL build; `--quick` refuses when the drug-page shell changed); runtime config injected via `/app-config.js` (keys synced with `docker/entrypoint.sh`); analytics never see the semantic-search query; the guided tour `src/tour.js`.

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
                                  #  are not inlined yet (see docs/architecture/eu-pages.md)
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
uv run src/audit-locate.py --sample 100  # check every semantic-search chunk of N random built pages
                          #  lands on its passage, not its section heading (replays the browser's
                          #  locateByBlocks on html5lib-parsed served pages; exit 1 on failure;
                          #  --all, --only CIS). deploy.sh runs it after the build (DEPLOY_LOCATE_SAMPLE)
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
                          #  (0: return each batch's memory to the system, see docs/architecture/deployment.md),
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
`.build-manifest.json` note in [docs/architecture/build-and-rendering.md](docs/architecture/build-and-rendering.md)), so a rebuild on unchanged data is fast.
Restart is not needed (Caddy reads the mounted dir live), but a
`docker compose -f docker/docker-compose.yml restart web` is harmless.

## Deployment / hardening notes

Full notes: [docs/architecture/deployment.md](docs/architecture/deployment.md). The invariants to never break:

- Caddy listens on :8459 plain HTTP behind your own TLS proxy. The `web` image is a custom xcaddy build with the rate-limit plugin; rebuild it only after a `docker/web.Dockerfile` change (`--rebuild-web`).
- The detailed `/api/stats` + `/api/sem/stats` stay blocked at the edge; new service routes go through the `(api_limits)` Caddyfile snippet.
- Every container is `read_only`, `cap_drop: ALL`, `no-new-privileges` (the `x-hardened` anchor) with a RAM ceiling; `web` keeps `cap_add: [NET_BIND_SERVICE]`. Need a writable path? Add a tmpfs, never drop read-only.
- Strict CSP (`script-src 'self'` + the optional umami origin): no external scripts, fonts or CDNs, no inline scripts or handlers, no eval; set styles via classes or CSSOM.
- `docker/.env` reaches containers via `env_file` only; no `environment:` block.
- Single-file bind mounts (both scrape manifests, `CIS_bdpm.txt`, the backlink files) must exist as files before `up`.
- Never re-add `*.vec.json` to the deploy rsync mirror (the embed service writes them on the VPS).

## Gotchas

- **A JavaScript syntax error does not fail anything: the page loads and its script silently never runs** (dead buttons, missing dynamic text). The sister site psychiatheque (served by `../justelesdocs`) shipped exactly that on 2026-10-01 (a stray `});` left by a removed listener) past its own `node --check`, because `node --check <path>` on a `.js` file guesses the module type and the guess passed the broken file. `scripts/check_js.mjs` parses every tracked `src/*.js` as a classic script with `vm.Script` instead, and runs from `.githooks/pre-push` (activate per clone: `git config core.hooksPath .githooks`) and from `deploy.sh`'s pre-flight, even under `DEPLOY_ALLOW_DIRTY=1`. It is a quick check, never worth skipping. Added by Claude Code.
- **The `embed` service is shared with the sibling project `../justelesrecos`, which ships no encoder of its own** (one copy of a ~1 GB model is all the VPS has room for). It reaches this one by container name over a Docker network called `justeles-embed`, declared `external: true` in both compose files, so nothing is published on the host. Removing that `networks:` block, or renaming the `justelesrcp-embed` container, takes the other site's search down with a 502 on every query while every container stays up and healthy. Two host-based routes were tried first and cannot work: a port published on 127.0.0.1 is unreachable from another container, and compose's `ports:` accepts an IP and never a hostname, so `host.docker.internal` there is rejected at parse time.
- `docker-compose.yml` sets `name: justelesrcp` for the same class of reason: without it Compose names the project after the directory holding the compose file, which is `docker/` here AND in `../justelesrecos`, and since both projects call their Caddy service `web`, deploying either one replaced the other's web container.
- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

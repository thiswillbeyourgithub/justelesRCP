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
      | build.py
      v
dist/rcp/<cis>-<slug>.html   one cleaned page per drug (slug from drug name),
                             with a sidebar table of contents (ToC) of sections
dist/search-index.json       [{cis,name,slug}] consumed by client-side search
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
- **The ANSM HTML keeps its `Amm*` CSS class hooks** (e.g. `AmmAnnexeTitre1`,
  `AmmDenomination`). `clean_rcp()` strips decoration (BackToTop images,
  inline `font-*` styles, scripts) but preserves those classes; `style.css`
  restyles them. If you change class names in one place, change both.
- **Each RCP page has a sidebar table of contents.** `clean_rcp()` assigns
  `id="sec-N"` to every top-level `AmmAnnexeTitre1` heading and returns the
  section list; `render_record()` emits a `<details class="toc">` of jump links
  (plus the version slot) into `{{TOC}}`. It is a sticky sidebar on wide screens
  and a collapsible block on phones (see `.rcp-layout`/`.toc` in `style.css`).
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
  A single worker thread serialises every outbound ANSM fetch behind a GLOBAL rate
  limit (`REFRESH_RATE_SECONDS` + jitter) and a per-CIS min-interval floor
  (`REFRESH_MIN_INTERVAL_SECONDS`, default 1h), so repeat clicks and many visitors
  on one stale page collapse to a single fetch; a bounded queue (`REFRESH_QUEUE_MAX`)
  sheds load as "busy". Endpoints: `GET /api/health`, `GET /api/status/<cis>`
  (asof + pending), `POST /api/refresh/<cis>` (returns fresh|queued|busy). It is
  same-origin, so the strict `connect-src 'self'` CSP covers the button's fetches.
  If the service is absent, `/api/*` just 502s and the button degrades gracefully,
  so static-only deploys omit it entirely. Both `build.py` and `scrape-rcp.py`
  guard `__main__`, so importing them must stay import-safe (no side effects at
  module load); the refresh service depends on that.
- **Every RCP page shows a freshness banner ("Informations à jour au …").**
  `build.py` bakes the *absolute* as-of date into the page as
  `data-rcp-asof="YYYY-MM-DD"` (`_asof_html`): `BASELINE_DATE` (`2022-05-02`) for
  a baseline CSV cell, or the scrape date for an overlay file (from
  `.scrape-manifest.json`'s `last_fetch`, else the overlay file's mtime; see
  `_load_scrape_dates`/`_overlay_date`). Baking the absolute date (not the age)
  keeps pages cacheable; `src/app-init.js` rewrites it client-side to a relative
  age ("il y a X mois") and adds a `.stale` warning class when the data is older
  than a year. The `asof` value is folded into each CIS's build-cache hash
  (`_record_hash`), so a re-scrape that changes the date re-renders the page.
  Keep the `data-rcp-asof` contract in sync between `_asof_html`, the
  `{{ASOF}}` slot in `src/rcp.html`, `.rcp-asof` in `style.css`, and the
  enhancer in `src/app-init.js`.
- **`/a-propos` is a static About page** (`src/a-propos.html`, shipped as a
  static asset): what the site is, the author, a privacy/hosting note, and a
  direct link to the GitHub repo. (`SOURCE_URL` still drives the separate "Code
  source" link in the DEV banner, `src/dev-banner.js`.)
- **~15% of CIS have an empty RCP field** in the source and are skipped (no page,
  not in the index). This is expected, not an error.
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
uv run scrape-rcp.py --limit 60   # refresh N RCPs from live ANSM into data/rcp/ overlay
                                  # env: RCP_OVERLAY_GZIP (gzip overlays, default on),
                                  # RCP_SCRAPE_RATE_SECONDS (base gap between fetches);
                                  # logs a progress bar + ETA and the trigger (user/timer)
uv run build.py           # build ./dist from ./data (overlay wins over the 2022 CSV)
uv run refresh-service.py # optional: run the on-demand refresh API on :8460 (behind Caddy /api/*)
cp docker/env.example docker/.env                      # optional: umami analytics / DEV banner / refresh knobs
docker compose -f docker/docker-compose.yml up -d      # serve ./dist on :8459 + refresh service (read-only, hardened)
docker compose -f docker/docker-compose.yml up --build # after changing Caddyfile/compose/refresh.Dockerfile
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
  it is only reachable through Caddy's `/api/*` proxy. Its ONLY writable mounts are
  the three narrow paths it must write (`data/rcp`, `dist/rcp`, and the
  `data/.scrape-manifest.json` file); everything else, including the CIS->name map,
  is mounted read-only. It runs as `${REFRESH_UID:-1000}:${REFRESH_GID:-1000}` so
  the overlays and rebuilt pages it writes stay owned by the host user (clean
  ownership + Syncthing). The manifest is bind-mounted as a single file, so it must
  exist on the host before first `up` (run `scrape-rcp.py` once, or
  `touch data/.scrape-manifest.json`), else Docker creates a directory in its place.
  Runtime knobs come from `docker/.env` (`env_file`) as `REFRESH_*` / `RCP_OVERLAY_GZIP`.

## Gotchas

- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

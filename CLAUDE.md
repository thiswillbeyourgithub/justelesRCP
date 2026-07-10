# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

justelesRCP is a fast, ad-free static site serving the **RCP** (résumés des
caractéristiques du produit) of medicines sold in France, sourced from the ANSM
BDPM public dataset. It exists to be a lightweight alternative to slow, for-profit
sites like vidal.fr. The whole thing is precomputed to static files; there is no
application server at runtime.

**Language convention:** the website (page text, UI strings) is in French; the
code, comments, and developer docs are in English. `README.md` is the French
readme and `README.en.md` is its English translation. They are cross-linked and
MUST be kept in sync: whenever you edit one, update the other accordingly.

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
dist/rcp/<cis>-<slug>.html   one cleaned page per drug (slug from drug name)
dist/search-index.json       [{cis,name,slug}] consumed by client-side search
dist/browse/index.html       A-Z landing (letter grid with counts)
dist/browse/<letter>.html    alphabetical drug list per letter ('#' -> num.html)
dist/index.html style.css search.js
dist/app-config.js app-init.js dev-banner.js   (runtime-config client assets)
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
uv run build.py           # build ./dist from ./data
cp docker/env.example docker/.env                      # optional: umami analytics / DEV banner
docker compose -f docker/docker-compose.yml up -d      # serve ./dist on :8459 (read-only, hardened)
docker compose -f docker/docker-compose.yml up --build # after changing Caddyfile/compose
```

To rebuild after a data refresh: re-run `download-data.sh` then `uv run build.py`;
`dist/` is wiped and regenerated each run. Restart is not needed (Caddy reads the
mounted dir live), but a `docker compose -f docker/docker-compose.yml restart web` is harmless.

## Deployment / hardening notes

- Caddy listens on **:8459 plain HTTP**; TLS is expected to be terminated by an
  upstream reverse proxy you already run. There is no ACME/TLS config here.
- The container runs `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, with tmpfs for Caddy's scratch dirs (`/tmp`, `/config`,
  `/data`, and `/gen` for the rendered `app-config.js`). Keep it that way; if
  Caddy needs a new writable path, add a tmpfs mount rather than dropping
  read-only.
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

## Gotchas

- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

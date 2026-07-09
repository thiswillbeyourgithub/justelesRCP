# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

justelesRCP is a fast, ad-free static site serving the **RCP** (résumés des
caractéristiques du produit) of medicines sold in France, sourced from the ANSM
BDPM public dataset. It exists to be a lightweight alternative to slow, for-profit
sites like vidal.fr. The whole thing is precomputed to static files; there is no
application server at runtime.

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
dist/index.html style.css search.js
+ .gz and .br precompressed siblings for every text file (Caddy serves these)
```

Key facts that aren't obvious from a single file:

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

## Commands

```bash
./download-data.sh        # fetch data/CIS_RCP.csv + data/CIS_bdpm.txt (see TODOs in it)
uv run build.py           # build ./dist from ./data
docker compose up -d      # serve ./dist on :8080 (read-only, hardened)
docker compose up --build # after changing Caddyfile/compose
```

To rebuild after a data refresh: re-run `download-data.sh` then `uv run build.py`;
`dist/` is wiped and regenerated each run. Restart is not needed (Caddy reads the
mounted dir live), but a `docker compose restart web` is harmless.

## Deployment / hardening notes

- Caddy listens on **:8080 plain HTTP**; TLS is expected to be terminated by an
  upstream reverse proxy you already run. There is no ACME/TLS config here.
- The container runs `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, with tmpfs for Caddy's scratch dirs. Keep it that way; if
  Caddy needs a new writable path, add a tmpfs mount rather than dropping
  read-only.
- A strict CSP (`default-src 'self'`) is set in the Caddyfile. The site uses no
  external fonts, scripts, or CDNs by design; keep it self-contained so the CSP
  holds.

## Gotchas

- `data/` and `dist/` are gitignored and large (~1GB source). Never commit them.
- `build.py` raises the csv field-size limit because single RCP HTML blobs can be
  megabytes; don't remove that.
- Slugs are ASCII-folded and capped at 80 chars; the CIS prefix guarantees
  uniqueness even if two drugs share a name.

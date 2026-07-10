<!-- English version. Version française : README.md
     IMPORTANT: README.md (FR) and README.en.md (EN) must stay in sync.
     When you edit one, update the other accordingly. -->

# justelesRCP

*Lire ceci en [français](README.md).*

**Just the summaries of product characteristics.** A fast, ad-free, no-account
static site serving the RCP (résumés des caractéristiques du produit, i.e. the
summaries of product characteristics) of medicines sold in France, from the
public ANSM / BDPM dataset. Built as a lightweight alternative to slow,
for-profit medicine sites.

- No application server, no database: every page is a precomputed static file.
- Client-side instant search over ~15,600 medicines, plus crawlable A-Z browse
  pages.
- Precompressed (brotli + gzip), served by a hardened, read-only Caddy
  container.
- Privacy-respecting analytics (umami: no cookies, no ad tracking, honours "Do
  Not Track"). Hosted in France.

> [!WARNING]
> **The RCP content is not up to date.** The latest dataset upload on
> data.gouv.fr dates from **2 May 2022**, so pages reflect medicine information
> that may be outdated. Always check an authoritative, current source before
> relying on any information here.

## Quick start

```bash
./download-data.sh                                  # fetch source datasets into ./data (gitignored)
uv run build.py                                     # render ./dist from ./data
cp docker/env.example docker/.env                   # optional: configure analytics
docker compose -f docker/docker-compose.yml up -d   # serve on http://localhost:8459
```

Put your own TLS reverse proxy in front of port 8459.

Optional runtime config (privacy-friendly [umami](https://umami.is) analytics and
a "work in progress" banner) lives in `docker/.env`; see `docker/env.example`.
Leave it empty for zero tracking. Nothing is loaded from a CDN; a strict CSP only
opens up your own umami origin when you set `ANALYTICS_URL`.

## How it works

`build.py` reads the ANSM RCP dump (`data/CIS_RCP.csv`:
`Code_CIS <TAB> RCP_html`), cleans and restyles each document, then writes:

- `dist/rcp/<cis>-<slug>.html`: one cleaned page per medicine, with a sidebar
  table of contents ("Sommaire") to jump between sections
- `dist/search-index.json`: consumed by the client-side search
- `dist/a-propos.html`: the "About" page
- `style.css`, `search.js`, and a `.gz`/`.br` sibling for every text file

The build is **incremental**: a per-medicine cache (`dist/.build-manifest.json`)
skips re-parsing and re-compressing unchanged documents, which greatly speeds up
redeploys after a plain data refresh. The version number is not baked into page
HTML (it is served at runtime via `app-version.js`), so a version-only bump does
not invalidate the cache.

See [CLAUDE.md](CLAUDE.md) for the detailed architecture.

## Data source

[Base de données publique des médicaments (BDPM)](https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante),
ANSM. The most recent upload dates from **2 May 2022**, so the content is old and
may no longer be accurate. This site is not affiliated with the ANSM or any
authority and does not replace professional medical advice.

## Credits

Built with the help of [Claude Code](https://claude.com/claude-code).

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
- Cross-drug backlinks: each RCP page automatically links the drug and substance
  names it mentions (e.g. "oméprazole", "carbamazépine") to those drugs' own
  pages, with a "Médicaments liés" (related medicines) box at the foot. Only
  substances that have an RCP page in the dataset are linked (never a dead
  link). These links are added by justelesRCP and are not part of the official
  ANSM text.
- Precompressed (brotli + gzip), served by a hardened, read-only Caddy
  container.
- Privacy-respecting analytics (umami: no cookies, no ad tracking). Hosted in
  France.

> [!CAUTION]
> **Early-stage prototype.** This site is at an early stage: some features are
> missing, bugs are likely, and the data may be incomplete or outdated. Do not
> use it as your only source for a medical decision; always check an
> authoritative, up-to-date source and consult a healthcare professional.

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

## Keeping RCPs up to date

The `CIS_RCP.csv` dump is frozen (2 May 2022) and is the only bulk *HTML* export
that exists: the official BDPM download is refreshed daily but ships metadata
only, never the RCP body. To refresh RCPs without giving up the static
architecture, `scrape-rcp.py` fetches drug pages from the live ANSM site in the
background and writes a per-medicine overlay file (`data/rcp/<cis>.html.gz`,
gzipped by default) that `build.py` prefers over the 2022 dump. Nothing dynamic
runs at serve time.

Every RCP page headlines ANSM's own revision date for that RCP ("Informations à
jour au …", read from the RCP body), plus a small "Version vérifiée par
justelesRCP le …" line for when we last checked the copy against ANSM. A warning
appears only when *our copy* has not been re-checked in over a year, not because
ANSM's text is old: an RCP ANSM last revised in 2021 but never changed is still
the current official text, so its age alone is not staleness.

```bash
uv run scrape-rcp.py --limit 60   # refresh 60 drugs (most-read first)
uv run build.py                    # rebuild (incremental: only changes)
```

A CIS refreshed less than `--ttl-days` ago (30 by default) is skipped. Priority
ordering comes from a JSONL frequency list (`--frequency`, default
`data/drugs_frequency.jsonl`) where each line is
`{"term": "<drug or substance name>", "score": <higher = sooner>}`; terms are
matched to each drug's name (accent-insensitive), and a drug no term matches gets
the 25th-percentile score so it stays at middling priority. Ideal as a `cron`
job. See the script header for all options (`--all` for a one-time full scan,
`--only` for specific CIS).

Two knobs are also driven by environment variables, handy for a `cron`:
`RCP_OVERLAY_GZIP` (gzipped overlays, on by default; `--no-gzip` for raw HTML,
`build.py` reads either transparently) and `RCP_SCRAPE_RATE_SECONDS` (base delay
between requests, with a random jitter added). The script logs a progress bar with
a time estimate and tags each page as manually requested (`--only`, "user") or
coming from the automatic queue ("timer").

### Refresh an RCP on demand (optional service)

Every RCP page has a "Rafraîchir maintenant" (refresh now) button, and a page whose
copy we last captured more than a year ago refreshes itself on load. Both call a small companion
service (`refresh-service.py`, `POST /api/refresh/<cis>`) that fetches the live ANSM
page, writes the overlay and rebuilds that one page. It is **the only dynamic part**
of the project: it runs in a separate, hardened container (read-only except three
narrow paths) so the web server itself can stay fully read-only, and a global rate
limit plus a per-drug floor (many visitors on the same page trigger a single fetch)
keep it gentle on the ANSM site. It is entirely **optional**: `docker compose ... up`
starts it, but `docker compose ... up web` leaves it out; without it, `/api/*`
returns an error and the button simply reports it as unavailable, the site staying
100% static. Tuning (`REFRESH_*`) lives in `docker/.env`; see `docker/env.example`.

## Data source and licence

The data comes from the
[Base de données publique des médicaments (BDPM)](https://base-donnees-publique.medicaments.gouv.fr/telechargement),
published by the ANSM. It is **open data** under the
[Licence Ouverte / Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/),
and justelesRCP reuses it **in compliance with that licence**: it cites the source
and its date, does not distort the data, and does not imply any official status.
The RCP baseline dates from **2 May 2022** (the most recent bulk upload), so the
content can be old and may no longer be accurate; some pages are progressively
refreshed. This reuse confers no official status and implies no endorsement by the
ANSM, HAS or UNCAM. This site is not affiliated with any authority and does not
replace professional medical advice.

To keep the RCPs current, justelesRCP occasionally fetches some pages directly from
the public ANSM website. These requests are infrequent, rate-limited and carry a
contact address, so as not to burden that public service; no visitor personal data
is sent in the process.

## Credits

Built with the help of [Claude Code](https://claude.com/claude-code).

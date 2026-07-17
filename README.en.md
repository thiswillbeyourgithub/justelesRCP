<!-- English version. Version française : README.md
     IMPORTANT: README.md (FR) and README.en.md (EN) must stay in sync.
     When you edit one, update the other accordingly. -->

# justelesRCP

### 🌐 Live site: **[justelesrcp.olicorne.org](https://justelesrcp.olicorne.org)**

*Lire ceci en [français](README.md).*

**Just the summaries of product characteristics.** A fast, ad-free, no-account
static site serving the RCP (résumés des caractéristiques du produit, i.e. the
summaries of product characteristics) of medicines sold in France, from the
public ANSM / BDPM dataset. Built as a lightweight alternative to slow,
for-profit medicine sites.

- No application server, no database: every page is a precomputed static file.
- Client-side instant search over ~15,600 medicines, plus crawlable A-Z browse
  pages.
- Semantic (embeddings) search within a given RCP page: type a natural-language
  question ("can I take it while pregnant?") and the page highlights the most
  relevant passages, with previous/next navigation. The query is embedded by a
  small self-hosted service (no ~120 MB model is downloaded to your browser
  anymore); it only covers pages that have been refreshed from the official
  source, never the frozen 2022 baseline. Optional, see below.
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
> **In-development prototype.** Some features are missing and bugs are likely.

## How it works

`build.py` reads the ANSM RCP dump (`data/CIS_RCP.csv`:
`Code_CIS <TAB> RCP_html`), cleans and restyles each document, then writes:

- `dist/rcp/<cis>-<slug>.html`: one cleaned page per medicine, with a heading
  (drug / presentation name) at the top and a collapsible
  table of contents ("Sommaire") to jump between sections and their
  subsections (4.1, 4.2, …)
- `dist/eu/<cis>-<slug>.html`: for centrally-authorized medicines (whose RCP is
  published by the EMA, not the ANSM, e.g. Abilify). When the EMA
  product-information PDF has been fetched (`scrape-ema.py`, converted to clean
  HTML by `ema_pdf.py`), it shows the **full converted RCP**, with a direct link to
  the official EMA PDF at the top of the page; otherwise a lightweight landing page
  that points to the official RCP on the EMA site and to any equivalent generics
  available here. One product (all its presentations, e.g. Abilify Maintena 300 mg
  and 400 mg) shares a single EMA PDF: as soon as one presentation is fetched, all
  of them show the full RCP. And **every `/eu/` page has a button** to fetch the RCP
  from the EMA on demand, without waiting for the background refresh. Either way they
  stay findable through the search. If the EMA PDF
  is temporarily unavailable, the text is recovered via the Internet Archive (a note
  says so on the page; the official link still points at the EMA)
- `dist/search-index.json`: consumed by the client-side search
- `dist/a-propos.html`: the "About" page
- `style.css`, `search.js`, and a `.gz`/`.br` sibling for every text file

The build is **incremental**: a per-medicine cache (`dist/.build-manifest.json`)
skips re-parsing and re-compressing unchanged documents, which greatly speeds up
redeploys after a plain data refresh. The version number is not baked into page
HTML (it is served at runtime via `app-version.js`), so a version-only bump does
not invalidate the cache.

See [CLAUDE.md](CLAUDE.md) for the detailed architecture.

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

At the top of every page a row of pill buttons links out to external sources:
**BDPM** (the drug's official record, by CIS code), then **HAS**, **EMA** and
**Vidal** (a full-text search on the drug's active substance). None of these are
fetched at build time; they are plain search links.

```bash
uv run scrape-rcp.py --limit 60   # refresh 60 drugs (most-read first)
uv run scrape-ema.py --limit 60   # fetch + convert EMA PDFs for centrally-authorized
                                  # drugs into full /eu/ pages (optional)
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
page, writes the overlay and rebuilds that one page. It is one of only two dynamic
parts of the project (the other is the semantic-search embed service, below): it runs
in a separate, hardened container (read-only except three
narrow paths) so the web server itself can stay fully read-only, and per-lane rate
limits plus a per-drug floor (many visitors on the same page trigger a single fetch)
keep it gentle on the ANSM site. It is entirely **optional**: `docker compose ... up`
starts it, but `docker compose ... up web` leaves it out; without it, `/api/*`
returns an error and the button simply reports it as unavailable, the site staying
100% static. In the background, a **crawler** continuously walks every page in
frequency (sold-units) order and refreshes any whose copy is older than the
staleness threshold (`REFRESH_CRAWL_TTL_DAYS`, 12 months by default), then idles
until the oldest one crosses it again. The crawler and the "Rafraîchir maintenant"
clicks run on **two separate worker lanes with their own rate limits**, so a click is
scraped almost at once on its fast lane (`REFRESH_DEMAND_RATE_SECONDS`, ~5 s) instead
of waiting behind the crawler's slow trickle (`REFRESH_RATE_SECONDS`, ~2 min); both
lanes stay serial and gentle. It keeps crawl statistics by trigger (button /
automatic / crawler) available at `GET /api/stats`. Tuning (`REFRESH_*`) lives in
`docker/.env`; see `docker/env.example`.

To force a full fresh sweep of the crawler (e.g. after a rendering change) without
deleting the existing copies (which would blank pages until they are re-fetched),
send `SIGHUP` to the refresh container (`deploy.sh --rebuild` does this after the
deploy): the crawler re-sweeps the whole catalog ignoring the staleness threshold,
one pass, while the site keeps serving the current pages until each is re-fetched.

The same service also handles the `/eu/` pages of centrally-authorized drugs, through
a **separate EMA lane** (a "Rafraîchir" button on a `/eu/` page plus a second crawler)
that fetches the EMA PDF, converts it and rebuilds the page. This lane has its own
knobs (`REFRESH_EMA_CRAWL`, `REFRESH_EMA_RATE_SECONDS` at 300 s by default since the
EMA is stricter, `REFRESH_EMA_CRAWL_TTL_DAYS` at 180 days) and its own manifest,
independent of the ANSM lane.

## Per-drug semantic search (optional)

Every RCP page (and every full `/eu/` page converted from the EMA) has a
"Rechercher dans ce RCP" box: type a natural-language question ("can I take it
while pregnant?", "effects on the liver") and the box ranks the closest passages
within that one drug, highlights the matching paragraph and lets you step through
the hits with previous/next. Tables (posology, etc.) are indexed row by row so each
row stays retrievable. Ranking is **hybrid**: semantic closeness is combined with a
lexical bonus whenever your own words (even approximately: plurals, typos) appear in a
section, pushing up passages that answer you both by meaning and by wording.

The query is embedded **server-side** by a small, hardened, read-only companion
service (`embed-service.py`) that keeps a multilingual model
(`Xenova/multilingual-e5-small`, int8 ONNX, ~120 MB) warm. The browser downloads
**no model anymore** (the old design pulled ~120 MB per visitor); it only sends the
short query text to our own same-origin `/api/sem/embed` and ranks the returned
vector against the page's section vectors locally. The trade-off is a little
privacy: the query text now transits our server, but it is **never logged** (only
counts and latency) and dropped right after encoding (the small in-memory cache that
avoids recomputing repeated queries is keyed by a hash of the text, not the text
itself), on a read-only container. The strict CSP goes back to `default-src 'self'`
with no WebAssembly relaxation.

Semantic search only covers pages that have been **refreshed from the official
source** (the ANSM re-scrape or the EMA PDF), never the frozen 2022 baseline. The
embed service embeds each page as it is crawled; searching a page that has never
been crawled triggers a one-off refresh first, then indexes it.

It is **entirely optional**: leave the embed service out (`docker compose ... up web
refresh`) and the box simply shows "indisponible", the rest of the site unchanged.
To enable it:

```bash
./download-model.sh               # fetch the ONNX model + tokenizer into ./models (~120 MB, gitignored)
docker compose -f docker/docker-compose.yml up -d --build   # starts the embed service (mounts ./models read-only)
uv run embed-rcp.py --limit 60    # OPTIONAL: pre-bake vectors offline to warm the backlog (needs build.py first)
```

Embedding is incremental via a content hash: a page is re-embedded only when its
text or the model changed, so a no-op refresh re-embeds nothing. The offline
`embed-rcp.py` and the live service share the exact same encoder and staleness
check, so their vectors never disagree.

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

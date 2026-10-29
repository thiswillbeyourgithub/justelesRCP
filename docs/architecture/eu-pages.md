# EU-authorization pages (/eu/) and EMA SmPC conversion

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

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
  `_render_stub`, primed per worker by `_init_stub_worker`, which picks the full page
  (`render_eu_page`) or the stub (`render_eu_stub`), so a change to how a
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
  and reads the capture date from the PDF `ModDate` (else `CreationDate`). The
  overlay keeps the base64 (self-contained), but the SERVED page does not:
  `render_eu_page` passes the page through `_externalize_images`, which writes each
  figure to `dist/eu/img/<sha256[:20]>.png|jpg` and links it (`loading="lazy"`).
  Content-addressed, so every sibling presentation of a product (same SmPC, same
  figures) shares one file; inline, the figures were ~1.4 GB of the ~1.9 GB of
  `/eu/` HTML, stored once per sibling and again in each precompressed copy, for
  ~0.45 GB of distinct images. The SAME store serves `/rcp/` pages (`IMG_LANES`,
  `img_dir(lane)`, `_externalize_images(page, lane)`): ANSM RCPs link their figures
  ANSM-relative (`/images/R.../image001.png`, e.g. DEPAMIDE's black-triangle
  "surveillance supplémentaire" pictogram), which 404 here, so `scrape-rcp.py`'s
  `inline_images` downloads each one at SCRAPE time off `ANSM_HOSTS` (the dead
  `prod-bdm.ansm.integra.fr` host the 2022 baseline links is rewritten to the public
  one, `ansm_image_url`) and bakes it into the overlay as a `data:` URI; an image that
  cannot be fetched keeps its URL and `_parse_clean` DROPS every non-`data:` `<img>`
  (keeping its tail text), so a reader never sees a broken icon + alt text.
  `scrape-rcp.py --missing-images` re-scrapes just the overlays still carrying a
  remote `<img>` (`overlays_missing_images`). `main()` prunes `/rcp/img/` with
  `_prune_images("rcp", since=started)`. `build_stubs`' `_prune` ends with
  `_prune_images("eu", ...)`, which deletes the figures no `/eu/` page links (it scans the
  pages, not a manifest, so figures the refresh service wrote count too; and it spares any figure modified since `build_stubs` started, since the refresh service writes figures before their page, and `_externalize_images` bumps a reused figure's date for the same reason). The
  Caddyfile serves `/eu/img/*` as `immutable` (a hash URL never changes content)
  and everything else `no-cache`. The refresh container writes them through its
  existing `dist/eu` and `dist/rcp` mounts. `/eu/img/` and `/rcp/img/` are excluded from
  the `@br_only`/`@br_fallback` routes. Keep in sync across `IMG_LANES`/`img_dir`/
  `_externalize_images`/`_prune_images`/`render_eu_page`/`render_record`/the `<img>`
  drop in `_parse_clean` (build.py), `inline_images`/`ansm_image_url`/`get_on_hosts`/
  `overlays_missing_images` (scrape-rcp.py), the `@page_img` header rule in
  `docker/Caddyfile`, `test_eu_figures_become_shared_files_and_orphans_are_pruned`,
  `test_rcp_images_unreachable_dropped_inlined_served_from_rcp_img` and
  `test_rcp_images_are_inlined_from_the_public_ansm_host` (test_ema_seed.py).
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
  harvest. A harvested link always wins (never overwritten), and a later ANSM
  re-fetch of a seeded CIS keeps the seeded link when the page links none
  (`scrape_one` carries the previous entry's `ema_pdf` over). `--dry-run` reports the
  plan and writes nothing. It lazily imports `build.py` (needs `brotli`); pure
  helpers are covered by `test_ema_seed.py`. Keep in sync across
  `seed_ema_links`/`_parse_ema_documents`/`_ema_pi_index`/`_match_brand`/
  `EMA_EPAR_JSON_URL` (scrape-ema.py) and `load_cap_meta`/`auth_groups`/
  `_brand_root`/`resolve_eu` (build.py). **SSRF guard:** every EMA link is
  scraped off third-party HTML and fetched server-side, so `bdpm.is_ema_pdf_url`
  (https on `ema.europa.eu` or a subdomain, parsed host, NOT a substring; the path is
  not required to end in `.pdf`, real links include `..._fr.pdf-0` and `/fr/media/N`)
  gates it everywhere: `extract_ema_pdf`, `_ema_pi_index`, `_load_ema_links`, `_eu_pdf`,
  and `_fetch_pdf`, which follows redirects by hand (`scrape-rcp.py`'s `get_on_hosts`, also used for ANSM images) so EVERY hop is
  checked; the Wayback path is allowed only on `bdpm.WAYBACK_HOSTS`. Bodies are
  streamed and abandoned past `EMA_PDF_MAX_BYTES` (60 MB), and `ema_pdf.convert`
  refuses a PDF over `MAX_PAGES` (400) before parsing (untrusted input). **Internet
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

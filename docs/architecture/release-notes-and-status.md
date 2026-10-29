# Release notes popup, /a-propos and /status

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

- **Release notes are authored per version and shown as a "Quoi de neuf ?" popup.**
  One directory per release, `docs/changelog/<version>/changelog.md` (so a version's
  notes land in the SAME commit as the `__version__` bump), written for a casual
  reader (a nurse, a patient), NOT a developer. `docs/changelog/README.md` is the
  authoring guide. The grammar is deliberately tiny and validated
  (`parse_changelog` in build.py): a `# <version> - <YYYY-MM-DD>` title whose version
  MUST match the directory name (the date is the release date the popup shows), then
  ONLY the four categories `## New features` / `## Improvements` / `## Bug fixes` /
  `## Documentation` (rendered in that fixed order, whatever the file order), then
  bullets. Each bullet is BILINGUAL: the `- ` line is English, the `fr:` line under
  it is the French the site actually shows (both required, per the language
  convention), and it may end with the commit sha(s) it came from in brackets
  (`[47a9986, 224f699]`), which the popup turns into GitHub commit links.
  `load_changelog()` compiles every note into `dist/changelog.json`
  (`{commit_url, categories, releases[]}`, newest-first) via
  `write_changelog()`, and is ALSO the release GATE: it is called at the TOP of
  `main()` and `SystemExit`s when the current `__version__` has no notes, so a bump
  can never ship silently and a malformed note fails in a second instead of after a
  full rebuild. Not every historical version needs an entry (the notes start at
  0.37.0, the guided tour); only the current one is mandatory.
  `src/changelog.js` (loaded on EVERY page, after `app-version.js`) decides when to
  show them: it keeps the last version it showed in `localStorage['jlrcp_changelog_seen']`
  and, when `window.__APP_VERSION__` is newer, fetches `changelog.json` and opens a
  modal with EVERY release since the stored one, then stores the new version. A
  FIRST-time visitor sees nothing (no "since" to show, and the tour greets them
  instead): the version is stored silently. It also stands down while a tour runs,
  WITHOUT storing, so the notes survive to the next visit: `src/tour.js` publishes
  `window.__TOUR_ACTIVE__` when it actually starts one and clears it in `endTour`,
  and `changelog.js` reads THAT (never `jlrcp_tour_seen`, which the tour only writes
  on the home/quetiapine pages: sniffing it suppressed the popup FOREVER for the
  reader who lands straight on a drug page from a search engine). To make that flag
  order-proof, `changelog.js` boots on `DOMContentLoaded`, which fires after every
  deferred script has run, so the `<script>` tag order does not matter.
  It fetches `changelog.json` only when it has something to show. The
  modal body is the only scrolling part (a long history never outgrows the viewport),
  each release headed by its version + French-formatted date, and a **"Tout afficher"**
  button expands the "since" view into the full history. `[data-changelog]` elements
  open it in full-history mode: the "Nouveautés" section of `/a-propos` and the home
  footer link. CSP-safe (same-origin script, no inline handlers, no `innerHTML`).
  Its modal frame (backdrop, card, scrolling body, footer, close cross, Escape/focus
  handling) is generic and exported as `window.jlrcpModal.open({title, label, sub, cls})`
  -> `{card, body, foot, ok, close}`; the `/status` backlog popup reuses it rather than
  duplicating a modal, so a change to the frame goes in `frame()` there. `frame()` is
  built on the bare `window.jlrcpModal.layer(cls)` (one overlay at a time, backdrop
  click + Escape close, `body.changelog-open` freezes the page, focus restored on
  close), which `src/lightbox.js` uses directly for its full-size image overlay.
  Keep the contract in sync across `docs/changelog/*/changelog.md` +
  `docs/changelog/README.md`, `parse_changelog`/`load_changelog`/`write_changelog`/
  `CHANGELOG_DIR`/`CHANGELOG_CATEGORIES`/`COMMIT_URL`/`_version_key` + the `main()`
  gate + the `changelog.js` entry in `static_assets` (build.py), `src/changelog.js`,
  the `<script src="/changelog.js">` tag in all five `src/*.html`, the
  `[data-changelog]` buttons in `src/a-propos.html` + `src/index.html`,
  `.changelog-*` in `style.css`, `setActive`/`__TOUR_ACTIVE__` in `src/tour.js`, the
  changelog skip in `app-init.js`'s delegated click tracker (its own events would
  otherwise be double-counted, exactly like `.semsearch`), and `test_changelog_*` in
  `src/test_embed.py`. Note the served JSON carries only the FRENCH half of each
  bullet + the French category labels: the English lines stay in the markdown for
  developers, so they are not downloaded by every reader.
- **`/a-propos` is a static About page** (`src/a-propos.html`, shipped as a
  static asset): what the site is, the author, a privacy/hosting note, and a
  direct link to the GitHub repo. (`SOURCE_URL` still drives the separate "Code
  source" link in the DEV banner, `src/dev-banner.js`.)
- **`/status` is a live operational dashboard** (`src/status.html` + `src/status.js`,
  both shipped as static assets, styled by `.status-*` in `style.css`). It is a plain
  static page whose JS fetches the TWO curated PUBLIC summary endpoints and renders
  them: `GET /api/summary` (refresh service `public_summary()`: per-lane crawl progress
  %, pages still due, sweep ETA, TTL, idle/active; refresh outcomes by trigger/result;
  on-demand queue; uptime) and `GET /api/sem/summary` (embed service `public_summary()`:
  pages + user queries embedded since boot, throughput, live embed backlog, an
  `indexing` block (sustained pages/min + per-page cadence: mean compute time +, when
  the sweep is on, its `backlog_rate` throttle), and a `crawl_gap` gauge = crawled
  overlays vs those still awaiting a vector, from the last reconcile scan, with a
  `backlog_eta_seconds` to clear it, so the page shows whether embedding trails the
  crawler). A dedicated "Indexation vs exploration" card correlates the TWO summaries
  client-side: the embedder's pages/min vs the crawler's page-production rate (each
  lane_view now carries `rate_seconds`+`pages_per_min`; only enabled, non-idle lanes
  count as producing), yielding the indexing/crawl ratio and a catch-up ETA
  (`backlog / (indexing_rate - crawl_rate)`), worst-case assuming every explored page
  must be re-indexed. Both derive
  from each service's existing `stats()` (single source of truth, no counter
  duplication) but OMIT the host-internal figures (raw queue internals, RSS/peak/model
  RAM) that the DETAILED `/api/stats` + `/api/sem/stats` keep for the operator and that
  Caddy blocks at the edge (see the hardening note). The summary routes are NOT blocked:
  they fall through to the `/api/*` + `/api/sem/*` proxies and are rate-limited like any
  API call; the query POST is same-origin so `connect-src 'self'` covers the page's
  fetches. It is CSP-safe (same-origin script, no inline handlers/eval; bar widths set
  via CSSOM), auto-refreshes every 15 s only while the tab is visible
  (a tick is skipped while the previous one is still out, every fetch times out, and the
  popup's async loads carry a generation token so a stale answer never paints over a
  newer view), and degrades to a
  per-card "indisponible" if a service is down (the static site is unaffected). It is
  `noindex` (thin, ever-changing: `_static_page_head` prepends the robots meta for
  `status.html`) and deliberately kept OUT of the sitemap; it is discoverable via the
  home + `/a-propos` footers. Both services track a monotonic `_started` for uptime and
  the counters are per-process ("since last reboot"). Because a restart wipes those, the embed summary also carries a `health` block from `_CrashGuard` (embed-service.py), a small `dist/rcp/.embed-state.json` that outlives the process: boot timestamps (`restarts_24h`), the page being embedded right now (so a boot that finds one still in flight knows the previous process died during it; after `_CRASH_QUARANTINE` (2) deaths on the same content hash that page is quarantined, skipped by the sweep and listed on /status, until its overlay changes), plus `running_seconds`. A SIGTERM (`docker stop`, a deploy) clears the in-flight mark before exiting (`_clean_stop`), so a restart is never blamed on the page being embedded. A deploy's mirror rsync wipes the file, which is fine. Before the first reconcile scan of a boot, `crawl_gap` is `{scan_pending: true}` and /status shows "vérification en cours" / "mesure en cours": it used to read the missing figure as 0 and show "à jour" during the Oct 2026 OOM restart loop. The reconcile sweep enqueues stale pages in RANDOM order (the queue holds `EMBED_QUEUE_MAX`, so a fixed order offered the same first 500 every pass and one killer page blocked all the rest). Added by Claude Code.
  **The backlog popup.** The embed card has a "Voir le détail de la file d'indexation"
  button opening a `window.jlrcpModal` table (built in `src/status.js`) fed by `GET
  /api/sem/backlog` (`Embedder.backlog_detail`): every page the service knows is not
  embedded (running, failed, queued, or in the last scan's `stale_cis`, capped at
  `_BACKLOG_LIST_MAX`), with its state, overlay file, rendered page, last error +
  timestamp + attempts (`_failures`, set by the worker), and for the first
  `_BACKLOG_DEEP` rows WHY it is stale (`_describe`: `missing` no vectors, `content`
  overlay changed, `config` model/width/quant changed, `touch` vectors current but older
  than the overlay so the worker will just bump the date, `no-page`, `no-overlay`) plus
  its chunk/token counts (`_chunk_stats`, cached per content hash; tokenising only, no
  inference). A row's "Aperçu" fetches `GET /api/sem/chunks/<cis>` (`chunks_detail`: the
  exact passages the encoder sees, heading prefix included, each with its token count).
  It also lists the last `_RECENT_MAX` worker results. Both routes are public (page text
  stats and errors only, never query data) and rate-limited like any `/api/sem/*` call;
  a deep row costs ~1 s of CPU on a long page, once per content hash. Both are CPU-bound, so at most
  `_DETAIL_SLOTS` (2) run at once (`_send_detail`, a non-blocking acquire: 503 past it),
  a backlog answer is reused for `_BACKLOG_TTL` (10 s) and a chunk preview is cached per
  content hash (`_chunks_cache`, via the same `_lru` helper as `_chunk_stats`). Keep the contract
  in sync across
  `public_summary`/`_started` + the `/api/summary` route (refresh-service.py),
  `public_summary`/`_started`/`_last_scan` (set in `_scan_and_enqueue`) +
  `backlog_detail`/`chunks_detail`/`_describe`/`_chunk_stats`/`_failures`/`_recent` + the
  `/api/sem/summary`, `/api/sem/backlog`, `/api/sem/chunks/<cis>` routes
  (embed-service.py), `src/status.html`, `src/status.js`,
  `.status-*` in `style.css`, `_STATIC_META`/`_static_page_head`/`static_assets`/
  `head_pages` (build.py), the footer links in `src/index.html` + `src/a-propos.html`,
  and the blocked-vs-public comment in `docker/Caddyfile`.

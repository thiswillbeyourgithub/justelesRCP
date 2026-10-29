# Search and browse

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

- **Browse pages are server-navigable (SEO), search is client-side.** The A-Z
  browse pages under `/browse/` are plain static links (crawlable, no JS), built
  by `write_browse()` in `build.py`. Names are bucketed by accent-folded first
  letter; non-alpha names go under `#` (`/browse/num`).
- **Search is 100% client-side.** `src/search.js` fetches `search-index.json`
  (~15k entries) once and does substring matching in the browser, against BOTH the
  brand name AND the active-substance/DCI string (the optional `sub` field, from the
  same cleaned `CIS_COMPO` map `load_substances` builds), so a search on the substance
  (e.g. "acétylcystéine" -> HIDONAC) surfaces every brand carrying it; a name hit ranks
  above a substance-only hit, and each result shows its DCI under the name
  (`.result-sub`). A row for a drug delisted from BDPM carries `ret:1` (see the
  archived bullet) and `search.js` appends a `.result-retired` " [RETIRÉ]" to its name.
  A **parallel import** (AIP, ~245 CIS, procedure "Autorisation d'importation parallèle") has no RCP and renders no page, but its ANSM page names the reference spécialité (`p.text-aip` notice); `scrape-rcp.py`'s `extract_aip_ref` stores that CIS as `aip_ref` in the manifest (kept across re-fetches and VPS merges with `ema_pdf`, via `HARVESTED_KEYS`), and `build.aip_search_rows` adds a row `{cis, name, slug: <reference's /rcp/ slug>, aip: <reference name>}` so searching the import opens the RCP that applies to it, labelled "Importation parallèle : RCP de …" (`.result-aip`) by `search.js`. No page of its own (it would duplicate the reference's text). AIPs are pageless so the refresh crawler never re-fetches them: refresh `aip_ref` with `scrape-rcp.py --force --only <cis>...`. Added by Claude Code.
  There is no search API. Full-text search over RCP *content* is
  intentionally NOT supported (that was the tradeoff for a zero-runtime static
  architecture). Keep the `sub`/`ret`/`aip` contract in sync across the search-index
  enrichment (build.py), `search.js`, and `.result-sub`/`.result-name`/
  `.result-retired`/`.result-aip` in `style.css`.
  **Matching and grouping (client-side only, no index change).** Every query word must appear in the name or the `sub` string, in any order ("olanzapine injectable" finds ZYPADHERA). Presentations whose names differ ONLY by standalone numbers (`dosageKey`; a number glued to letters like "B12" is part of the name) fold into ONE row titled by `describeGroup` ("ZYPADHERA 210/300/405 mg, ...", or "10 mg/5 ml | 20 mg/10 ml" when several numbers vary), with a "N présentations" toggle (`.results-group`, `.result-count`) that unfolds its rows (`.results-variants`); AIP rows never group. A group ranks at its best member, so `search()` always scans the whole index (~40 ms worst case on one-letter queries). The dropdown shows 15 GROUPS and an "Afficher plus de résultats…" row (`.results-more`) adds 15 per press; `PAGE_MAX` also counts groups, while the page title counts drugs. Arrow keys walk only the visible links (`visibleLinks`). Added by Claude Code.
  **Two views, one matcher: the dropdown and the shareable results page.** The
  as-you-type dropdown (`#results`, top 15) is the quick-pick list; the RESULTS PAGE
  (`#searchpage` in `src/index.html`, up to `PAGE_MAX` = 300 rows + an exact match
  count) is the linkable "every sertraline drug" view, so an outside page can link
  to `/?q=sertraline` instead of a single drug (`?query=` is accepted as an alias,
  `q` is what we write). It is NOT a separate build output: it is a hidden `<section>`
  on the home page that `search.js` fills, so the site stays static and the SEO story
  is unchanged (the home canonical still points at `/`, so a query URL is not a
  duplicate page). Both views render rows through the SAME `resultItem()` and rank
  through the SAME `search(term, limit, full)` (`full` scans the whole index for the
  exact count; the dropdown stops early), so they can never drift. It shows on
  arrival at a `?q=` URL, and on **Enter with no arrow-selected suggestion**
  (arrow-selecting a drug and pressing Enter still opens that drug); `showPage()`
  pushes the `/?q=…` URL so the view is bookmarkable and Back works (a `popstate`
  handler restores it), sets the document title to "<term> - Recherche -
  justelesRCP", blurs the input (mobile keyboard) and adds `body.searching` (which
  tightens the hero). Arriving on a `?q=`/`?query=` URL ALSO suppresses the tour's
  first-visit auto-popup in `src/tour.js` (`boot()`'s `shared`/`auto`): someone
  following a shared result list must not get a modal over it; an explicit `?tour=1`
  still runs. The `WebSite`/`SearchAction` JSON-LD `urlTemplate` `/?q={search_term_string}`
  (build.py `_website_jsonld`) now resolves to this page. Keep the results-page
  contract in sync across `search.js` (`urlTerm`/`search`/`resultItem`/`renderPage`/
  `showPage`), the `#searchpage` markup in `src/index.html`, `.searchpage*` +
  `.home.searching` in `style.css`, and the auto-start gate in `src/tour.js`.

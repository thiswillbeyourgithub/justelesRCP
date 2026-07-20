// Per-drug semantic search: a collapsible "Recherche sémantique dans ce RCP" box that lets
// the reader ask a natural-language question ("puis-je le prendre enceinte ?") and
// jumps to the most relevant passages of THIS drug's text, highlighting them with
// a prev/next navigator.
//
// The query encoder now runs SERVER-SIDE (embed-service.py, behind Caddy's
// same-origin /api/sem/* proxy), so the browser downloads NO model: the old design
// pulled a ~120 Mo model per visitor, which was the feature's worst wart. The
// trade-off is that the query text now transits our server; it is never logged and
// dropped right after encoding (see the service + docs). Everything stays
// same-origin, so the strict CSP (default-src 'self') holds with no relaxation.
//
// Analytics (umami, optional): we emit two PRIVACY-SAFE usage events via app-init's
// guarded window.trackEvent - "recherche-rcp" once per opened search session and
// "recherche-rcp-nav" per prev/next click - carrying ONLY coarse counts, NEVER the
// reader's query text (which stays between the browser and the same-origin embed
// service). The generic click tracker in app-init.js explicitly skips .semsearch so
// no result snippet (page text) leaks as an event label either.
//
// Two halves:
//   1. Per-page vectors: the embed service bakes dist/<rcp|eu>/<slug>.vec.json (int8,
//      one vector per section chunk) as each page is CRAWLED (never the frozen 2022
//      baseline). Fetched lazily on first open.
//   2. Query vector: POST /api/sem/embed {q} -> {q: base64-int8, dim}. The client
//      dequantises both sides with the same decodeVec and cosine-ranks locally, so
//      the server holds no per-reader state and returns only a tiny vector. Ranking is
//      HYBRID: the cosine is blended with a client-side lexical bonus (a fuzzy word
//      match of the query against each section's own words), see rank()/lexicalScore.
//
// On a not-yet-embedded page the box asks the server to embed it right away
// (POST /api/sem/page/<cis>), polls until ready, then searches. A baseline-only page
// is auto-crawled first (the server orchestrates it via the refresh service). If the
// embed service is absent, every call 502s and the box degrades to an "indisponible"
// note, exactly like the refresh button. The box is created in JS (not baked) so
// no-JS pages show no dead control; styling is via the .semsearch* classes (an inline
// style="" would trip the strict style-src CSP).
//
// Note on responsiveness ("KV / prefix cache"): e5-small is a *bidirectional*
// encoder, so there is no incremental prefix-KV cache to exploit (changing the tail
// of the query re-encodes every token). Snappiness as the reader types comes instead
// from a debounce, an AbortController that cancels the superseded in-flight request,
// the server-side query LRU (repeated/edited queries recompute nothing), and the
// ~15 ms encode.
(function () {
  "use strict";
  const main = document.querySelector(".rcp[data-cis]");
  if (!main) return; // not an RCP / full-EU page
  if (document.querySelector(".rcp-stub")) return; // a stub has no captured text yet
  const cis = (main.getAttribute("data-cis") || "").trim();
  if (!/^\d{8}$/.test(cis)) return;

  // Must match the server's EMBED_MIN_QUERY_CHARS / EMBED_MAX_QUERY_CHARS (5 / 400);
  // gated here too so we never fire a request the server would 400.
  const MIN_CHARS = 5;
  const MAX_CHARS = 400;
  const MAX_RESULTS = 25; // hard cap on distinct passages surfaced per query
  const MAX_SUBQUERIES = 25; // a "//"-split query fans out into at most this many (test)
  // HYBRID ranking. We start from the semantic (cosine) score of every section chunk,
  // then ADD a small lexical bonus whenever the reader's own words fuzzily match the
  // words in that section (see lexicalScore). So a passage that is both semantically
  // close AND literally mentions the query terms floats to the top, while a purely
  // semantic match still ranks on its own merit. e5's query/passage cosines sit in a
  // narrow high band, so a low absolute floor keeps every plausible section and lets the
  // lexical bonus plus the MAX_RESULTS cap do the pruning.
  //   SEM_FLOOR     - minimum cosine (0..1) for a chunk to be a candidate at all ("at
  //                   least 50 % similarity"); below it the chunk is dropped as noise.
  //   KEYWORD_BOOST - most a full keyword match can add to the cosine. Kept small so it
  //                   re-orders within the semantic band without overriding it.
  // Scores are the int8-dequantised dot product of ~unit-norm vectors, i.e. the cosine.
  const SEM_FLOOR = 0.5;
  const KEYWORD_BOOST = 0.15;
  const MIN_TERM_LEN = 3; // ignore shorter query/section words (stopword-ish noise)
  const POLL_MS = 1500; // gap between page-status polls while indexing
  const POLL_MAX = 90; // give up after ~POLL_MS * POLL_MAX (crawl + embed can be slow)

  // Page is served extensionless (Caddy try_files) or as .html; its vectors live
  // next to it as <slug>.vec.json (written by the embed service / embed-rcp.py).
  const vecUrl = location.pathname.replace(/\.html$/, "") + ".vec.json";
  const pageUrl = "/api/sem/page/" + cis;

  // --- UI (created here, class-styled only) ---------------------------------
  const box = document.createElement("details");
  box.className = "semsearch";
  const summary = document.createElement("summary");
  const summaryText = document.createElement("span");
  summaryText.textContent = "Recherche sémantique dans ce RCP";
  // A "?" that makes clear this is a MEANING-based search, not a keyword field. It
  // hover-tooltips (title) AND toggles a visible one-liner on tap (works on touch,
  // where native tooltips never show). preventDefault + stopPropagation so clicking
  // it does NOT open/close the <details> it sits inside.
  const help = document.createElement("button");
  help.type = "button";
  help.className = "semsearch-help";
  help.textContent = "?";
  help.setAttribute("aria-label", "À propos de la recherche sémantique");
  const HELP_TEXT =
    "Ce n'est pas une recherche par mot-clé : posez une question ou décrivez ce " +
    "que vous cherchez (ex. « puis-je le prendre enceinte ? »). Les passages les " +
    "plus proches par le sens sont classés.";
  help.title = HELP_TEXT;
  // The help lives INSIDE the <summary> (a <span>, valid there; a <p> is not) so it
  // shows whether the box is collapsed or open: everything else in a <details> is
  // hidden while collapsed, which is the box's default state. Its own click is
  // swallowed so reading it doesn't toggle the box.
  const helpText = document.createElement("span");
  helpText.className = "semsearch-help-text";
  helpText.hidden = true;
  helpText.textContent = HELP_TEXT;
  helpText.addEventListener("click", function (ev) {
    ev.stopPropagation();
  });
  summary.append(summaryText, help, helpText);
  const panel = document.createElement("div");
  panel.className = "semsearch-panel";
  help.addEventListener("click", function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    helpText.hidden = !helpText.hidden;
  });
  const input = document.createElement("input");
  input.type = "search";
  input.className = "semsearch-input";
  input.placeholder = "ex. : « Impact des repas sur la biodisponibilité »";
  input.setAttribute("enterkeyhint", "search");
  input.setAttribute("aria-label", "Recherche sémantique dans ce RCP");
  input.setAttribute("minlength", String(MIN_CHARS));
  input.setAttribute("maxlength", String(MAX_CHARS));
  const status = document.createElement("p");
  status.className = "semsearch-status";
  const nav = document.createElement("div");
  nav.className = "semsearch-nav";
  nav.hidden = true;
  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "semsearch-prev";
  prevBtn.textContent = "‹ Précédent";
  const counter = document.createElement("span");
  counter.className = "semsearch-counter";
  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "semsearch-next";
  nextBtn.textContent = "Suivant ›";
  nav.append(prevBtn, counter, nextBtn);
  const results = document.createElement("ol");
  results.className = "semsearch-results";
  panel.append(input, status, nav, results);
  box.append(summary, panel);
  // Sit right AFTER the Sommaire (ToC), so the vertical order reads
  // pills -> Sommaire -> search. Pages with no headings have no `.toc`: fall back
  // to just after the freshness card, else after the CIS line, else prepend.
  const anchor =
    main.querySelector(".toc") ||
    document.querySelector("[data-rcp-asof]") ||
    main.querySelector(".cis");
  if (anchor && anchor.parentNode) anchor.after(box);
  else main.prepend(box);

  // --- state ----------------------------------------------------------------
  let phase = "idle"; // idle | preparing | ready | error
  let chunks = null; // [{sec, snippet, vec: Float32Array}]
  let pageDim = 0; // dim of this page's section vectors, to guard a model/dim mismatch
  let readyPromise = null;
  let queryController = null; // aborts a superseded /api/sem/embed
  let hits = []; // [{sec, snippet, score, cosine, lexical, el}]
  let current = -1;
  let highlighted = []; // elements currently tagged, for cleanup
  let lastRanked = ""; // the query behind the current hit list (Enter = next vs search)
  let usageTracked = false; // fire the "used" analytics event once per open session

  function setStatus(text) {
    status.textContent = text || "";
  }

  // Privacy-safe analytics: forward to app-init's guarded tracker (a no-op when
  // metrics are off / Do-Not-Track / umami absent). NEVER pass the query text here.
  function track(name, data) {
    if (typeof window.trackEvent === "function") window.trackEvent(name, data);
  }

  // base64(signed int8 bytes) -> dequantised Float32Array (mirror of build.py
  // quantize_int8: v = q / 127). Int8 is stored two's-complement in each byte.
  function decodeVec(b64) {
    const bin = atob(b64);
    const out = new Float32Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
      let byte = bin.charCodeAt(i);
      if (byte > 127) byte -= 256;
      out[i] = byte / 127;
    }
    return out;
  }

  function norm(s) {
    return (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  // Collapse whitespace runs + trim, PRESERVING case (unlike norm(), which lowercases
  // for matching). Used for the text we display to the reader.
  function collapseWs(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  // --- hybrid lexical matching ----------------------------------------------
  // A compact, accent-folded French stoplist so ubiquitous words don't inflate the
  // lexical bonus uniformly across sections. Kept deliberately small: it only needs the
  // words frequent enough to match almost everywhere and thus tell sections apart poorly.
  const STOP = new Set(
    (
      "les des une aux avec sans pour dans par sur que qui quoi dont mais donc car " +
      "est sont etre ete avoir plus moins tres peut puis lors chez vers cette ces mon " +
      "mes son ses nos vos leur leurs quel quelle quels quelles comment quand pourquoi " +
      "ainsi cela ceci celui celle vous nous ils elles"
    ).split(" ")
  );

  // Accent-fold + lowercase a string and split it into word tokens >= MIN_TERM_LEN.
  function foldTokens(s) {
    return (s || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((w) => w.length >= MIN_TERM_LEN);
  }

  // The reader's query as a deduped list of meaningful (non-stopword) folded terms.
  function queryTerms(q) {
    const seen = new Set();
    const out = [];
    for (const w of foldTokens(q)) {
      if (STOP.has(w) || seen.has(w)) continue;
      seen.add(w);
      out.push(w);
    }
    return out;
  }

  // Folded word bag of one rendered section, cached (the DOM is static). Returns
  // {set, list}: the set for O(1) exact hits, the deduped list to scan for fuzzy/prefix
  // matches. We read the SAME blocks locate()/sectionBlocks walk, so a lexical match
  // reflects the section body the reader would actually scroll to.
  const secWordsCache = new Map();
  function getSecWords(secId) {
    let cached = secWordsCache.get(secId);
    if (cached) return cached;
    const head = document.getElementById(secId);
    const set = new Set();
    if (head) {
      const parts = [];
      for (const el of sectionBlocks(head)) parts.push(el.textContent);
      for (const w of foldTokens(parts.join(" "))) set.add(w);
    }
    cached = { set: set, list: Array.from(set) };
    secWordsCache.set(secId, cached);
    return cached;
  }

  // Bounded Levenshtein: true iff the edit distance(a, b) <= max. Two-row DP with an
  // early exit as soon as a whole row exceeds max, so it stays cheap on short words.
  function levLE(a, b, max) {
    const la = a.length;
    const lb = b.length;
    if (Math.abs(la - lb) > max) return false;
    let prev = new Array(lb + 1);
    for (let j = 0; j <= lb; j++) prev[j] = j;
    for (let i = 1; i <= la; i++) {
      const cur = new Array(lb + 1);
      cur[0] = i;
      let best = i;
      const ca = a.charCodeAt(i - 1);
      for (let j = 1; j <= lb; j++) {
        const cost = ca === b.charCodeAt(j - 1) ? 0 : 1;
        const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        cur[j] = v;
        if (v < best) best = v;
      }
      if (best > max) return false;
      prev = cur;
    }
    return prev[lb] <= max;
  }

  // How well one query term matches a section's words: 1 exact, 0.85 prefix/inflection
  // (plural, conjugation), 0.7 fuzzy (within a small edit distance, i.e. a typo), else 0.
  function termCredit(term, words) {
    if (words.set.has(term)) return 1;
    const maxD = term.length <= 5 ? 1 : 2;
    let best = 0;
    for (const w of words.list) {
      if (
        w.length >= 4 &&
        term.length >= 4 &&
        (w.startsWith(term) || term.startsWith(w))
      ) {
        best = 0.85;
        break; // nothing non-exact beats a prefix match
      }
      if (Math.abs(w.length - term.length) <= maxD && levLE(term, w, maxD)) {
        if (best < 0.7) best = 0.7;
      }
    }
    return best;
  }

  // Lexical bonus in [0, 1] for a section: the mean per-term credit, so a query whose
  // every word is present scores 1 and a query with no lexical overlap scores 0.
  function lexicalScore(secId, terms) {
    if (!terms.length) return 0;
    const words = getSecWords(secId);
    if (!words.list.length) return 0;
    let sum = 0;
    for (const t of terms) sum += termCredit(t, words);
    return sum / terms.length;
  }

  // --- readiness state machine ----------------------------------------------
  // Ensure this page is embedded, then load its vectors. Memoised so repeated opens/
  // queries reuse the one in-flight (or finished) run.
  function ensureReady() {
    if (readyPromise) return readyPromise;
    phase = "preparing";
    readyPromise = prepare()
      .then(() => {
        phase = "ready";
        setStatus("");
        // A query typed while we were indexing runs as soon as we are ready.
        if (input.value.trim().length >= MIN_CHARS) runSearch();
      })
      .catch((err) => {
        phase = "error";
        readyPromise = null; // allow a retry on the next open/keystroke
        setStatus(err && err.userMessage
          ? err.userMessage
          : "Recherche sémantique indisponible pour le moment.");
        throw err;
      });
    return readyPromise;
  }

  function fail(userMessage) {
    const e = new Error(userMessage);
    e.userMessage = userMessage;
    return e;
  }

  async function prepare() {
    setStatus("Préparation de la recherche…");
    let res;
    try {
      res = await fetch(pageUrl + "?src=user", { method: "POST" });
    } catch (_) {
      throw fail("Recherche indisponible (service d'indexation absent).");
    }
    if (res.status === 429) {
      throw fail("Service d'indexation occupé, réessayez dans un instant.");
    }
    if (res.status >= 500) {
      throw fail("Recherche indisponible (service d'indexation absent).");
    }
    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      /* keep default */
    }
    const st = data.status;
    if (st === "unavailable") {
      throw fail("Recherche sémantique indisponible pour cette page.");
    }
    if (st === "fresh") {
      // already embedded: straight to the vectors.
    } else if (st === "crawling") {
      setStatus("Récupération de la page puis indexation en cours…");
      await pollEmbedded();
    } else {
      // queued (or "queued" alias for a dup already in flight)
      setStatus("Indexation de cette page en cours…");
      await pollEmbedded();
    }
    await loadVectors();
  }

  async function pollEmbedded() {
    for (let i = 0; i < POLL_MAX; i++) {
      await sleep(POLL_MS);
      let r;
      try {
        r = await fetch(pageUrl);
        if (!r.ok) continue;
        const s = await r.json();
        if (s.embedded) return;
      } catch (_) {
        // transient: keep polling until the budget runs out
      }
    }
    throw fail("L'indexation prend trop de temps, réessayez plus tard.");
  }

  async function loadVectors() {
    setStatus("Chargement de l'index…");
    let res;
    try {
      res = await fetch(vecUrl);
    } catch (_) {
      throw fail("Recherche indisponible (index introuvable).");
    }
    if (!res.ok) throw fail("Recherche sémantique pas encore disponible pour cette page.");
    const data = await res.json();
    chunks = (data.chunks || []).map((c) => ({
      sec: c.sec,
      snippet: c.snippet,
      vec: decodeVec(c.q),
    }));
    if (!chunks.length) throw fail("Aucun contenu indexé pour cette page.");
    pageDim = chunks[0].vec.length; // query vectors must match this dim (same model)
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // --- query + ranking ------------------------------------------------------
  async function runSearch() {
    const raw = input.value.trim();
    if (raw.length < MIN_CHARS) {
      clearHits();
      setStatus(raw.length ? "Tapez au moins " + MIN_CHARS + " caractères." : "");
      return;
    }
    if (phase !== "ready") {
      ensureReady().catch(() => {}); // status set by the state machine
      return;
    }
    // A "//" splits the query into equal-weight sub-queries whose hybrid scores are
    // averaged per chunk (an unadvertised experiment; a plain query is simply one
    // sub-query). Strip each, drop the too-short ones (the server would 400 them),
    // dedupe so a repeat can't skew the average, and cap the count.
    let subs = raw
      .split("//")
      .map((s) => s.trim())
      .filter((s) => s.length >= MIN_CHARS);
    subs = Array.from(new Set(subs)).slice(0, MAX_SUBQUERIES);
    if (!subs.length) {
      setStatus("Tapez au moins " + MIN_CHARS + " caractères.");
      return;
    }
    // Cancel any still-in-flight query: only the latest keystroke matters.
    if (queryController) queryController.abort();
    const controller = new AbortController();
    queryController = controller;
    setStatus("Recherche…");
    let queries;
    try {
      // Embed every sub-query in parallel under the SAME controller, so a newer
      // keystroke aborts them all. A sub-query the server rejects yields null and is
      // dropped; the rest still rank.
      queries = await Promise.all(
        subs.map(async (sub) => {
          const res = await fetch("/api/sem/embed", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ q: sub.slice(0, MAX_CHARS) }),
            signal: controller.signal,
          });
          if (!res.ok) return null;
          const data = await res.json();
          return { vec: decodeVec(data.q), terms: queryTerms(sub) };
        })
      );
    } catch (err) {
      if (err && err.name === "AbortError") return; // superseded, ignore
      setStatus("Recherche indisponible (service d'indexation absent).");
      return;
    }
    if (controller !== queryController) return; // a newer query already superseded us
    queries = queries.filter(Boolean);
    if (!queries.length) {
      setStatus("Erreur lors de l'encodage de la requête.");
      return;
    }
    // The query encoder and this page's section vectors must share a dimension (same
    // model). If any disagree, the page was embedded by a different model and needs
    // re-indexing: surface it instead of silently ranking a truncated dot product.
    if (pageDim && queries.some((query) => query.vec.length !== pageDim)) {
      clearHits();
      setStatus("Index de cette page à mettre à jour, réessayez plus tard.");
      return;
    }
    rank(queries);
  }

  function rank(queries) {
    lastRanked = input.value.trim(); // so Enter can tell "next hit" from "new search"
    // Score every chunk. For each sub-query: semantic cosine (== dot product, both
    // int8-dequantised and ~unit-norm) PLUS a lexical bonus when that sub-query's words
    // fuzzily match this section's words. We AVERAGE both the cosine and the lexical
    // bonus across sub-queries (equal weights), so a plain single query is unchanged and
    // an "a//b" query surfaces chunks close to BOTH. Keep the (averaged) raw cosine too:
    // it, not the hybrid score, is what the SEM_FLOOR candidate gate tests, so a keyword
    // match re-orders candidates without inventing them out of unrelated sections.
    const scored = chunks.map((c) => {
      const v = c.vec;
      let cosSum = 0;
      let lexSum = 0;
      for (const query of queries) {
        const qv = query.vec;
        const n = Math.min(v.length, qv.length);
        let dot = 0;
        for (let i = 0; i < n; i++) dot += v[i] * qv[i];
        cosSum += dot;
        lexSum += lexicalScore(c.sec, query.terms);
      }
      const cosine = cosSum / queries.length;
      const lexical = lexSum / queries.length;
      return {
        sec: c.sec,
        snippet: c.snippet,
        cosine: cosine,
        lexical: lexical,
        score: cosine + KEYWORD_BOOST * lexical,
      };
    });
    scored.sort((a, b) => b.score - a.score);
    // Resolve each to a DOM block, deduping by target element (Set compares by
    // identity) so two chunks in one paragraph count once; keep up to MAX_RESULTS
    // distinct passages that clear the semantic floor.
    clearHits();
    const seen = new Set();
    for (const s of scored) {
      if (hits.length >= MAX_RESULTS) break;
      // Gate on the raw cosine, and `continue` (not `break`): the list is sorted by the
      // HYBRID score, so a lower-cosine item can sit above a higher-cosine one.
      if (s.cosine < SEM_FLOOR) continue;
      const el = locate(s.sec, s.snippet);
      if (!el || seen.has(el)) continue;
      seen.add(el);
      hits.push({
        sec: s.sec,
        snippet: s.snippet,
        score: s.score,
        cosine: s.cosine,
        lexical: s.lexical,
        el: el,
      });
    }
    renderHits();
    // "The semantic search was used": fire once per opened session, after a real
    // query has been encoded + ranked (fires even for 0 hits). Coarse count only,
    // no query text. Reset on box-close so a later fresh open counts again.
    if (!usageTracked) {
      usageTracked = true;
      track("recherche-rcp", { resultats: hits.length });
    }
    if (!hits.length) {
      setStatus("Aucun passage pertinent.");
      return;
    }
    // Do NOT auto-jump to the first hit: renderHits() has lightly highlighted every
    // ranked passage in place; let the reader choose which one to view (click a
    // result, or step with the prev/next navigator / Enter). `current` stays -1
    // until they pick, so the counter shows the total, not a position.
    counter.textContent = hits.length + (hits.length > 1 ? " résultats" : " résultat");
    setStatus("Cliquez un passage, ou naviguez avec ‹ ›.");
  }

  // Find the block element for a hit: the paragraph within section `secId` whose text
  // contains the chunk snippet, else the section heading as a fallback (e.g. a
  // linearised table-row chunk whose text isn't a contiguous DOM string).
  function locate(secId, snippet) {
    const head = document.getElementById(secId);
    if (!head) return null;
    const needle = norm(snippet).slice(0, 40);
    if (needle.length >= 8) {
      for (const el of sectionBlocks(head)) {
        if (norm(el.textContent).indexOf(needle) !== -1) return el;
      }
    }
    return head; // fallback: scroll to the section title
  }

  // The leaf-ish block elements belonging to a section: the heading's following
  // siblings up to the next section heading. Handles both the flat ANSM layout and
  // the /eu/ layout where headings can sit inside <details class="ema-annexe">.
  function sectionBlocks(head) {
    const out = [];
    const isHeading = (el) =>
      (el.id && /^sec-\d+$/.test(el.id)) ||
      (el.classList && el.classList.contains("AmmAnnexeTitre1"));
    let el = head.nextElementSibling;
    while (el && !isHeading(el)) {
      collectLeaves(el, out);
      el = el.nextElementSibling;
    }
    // If the heading has no following siblings (it is wrapped), fall back to its
    // parent's subtree minus the heading itself.
    if (!out.length && head.parentElement) {
      for (const child of head.parentElement.children) {
        if (child !== head && !isHeading(child)) collectLeaves(child, out);
      }
    }
    return out;
  }

  function collectLeaves(el, out) {
    if (el.nodeType !== 1) return;
    const blocks = el.matches("p, li, td, th, dd, dt, caption")
      ? [el]
      : el.querySelectorAll("p, li, td, th, dd, dt, caption");
    if (blocks.length) {
      for (const b of blocks) out.push(b);
    } else {
      out.push(el); // a bare text container (e.g. a stray <div>)
    }
  }

  // --- highlighting + navigation --------------------------------------------
  function clearHits() {
    for (const el of highlighted) {
      el.classList.remove("semsearch-hit", "semsearch-current");
    }
    highlighted = [];
    hits = [];
    current = -1;
    results.replaceChildren();
    nav.hidden = true;
  }

  // The text shown for a hit: the FULL on-page passage it resolved to (chunks are
  // small, so we show the whole paragraph, not a truncated excerpt). When locate() fell
  // back to the section heading (a linearised table-row / non-contiguous chunk has no
  // single paragraph, so hit.el IS the heading and carries the sec-N id), there is
  // nothing sensible to expand, so use the stored snippet.
  function displayText(hit) {
    if (hit.el && hit.el.id !== hit.sec) {
      const full = collapseWs(hit.el.textContent);
      if (full) return full;
    }
    return collapseWs(hit.snippet);
  }

  // The relevance badge: the blended hybrid score as a clamped percentage, small +
  // muted. Its tooltip spells out that the ranking is HYBRID, naming both halves (the
  // semantic embedding proximity AND the keyword match) so the number stays legible.
  function buildScore(hit) {
    const score = document.createElement("span");
    score.className = "semsearch-score";
    score.textContent = Math.round(Math.min(1, hit.score) * 100) + " %";
    score.title =
      "Score hybride : proximité sémantique " +
      Math.round(hit.cosine * 100) +
      " %" +
      (hit.lexical > 0
        ? ", correspondance de mots-clés " + Math.round(hit.lexical * 100) + " %"
        : ", aucun mot-clé commun");
    return score;
  }

  function renderHits() {
    results.replaceChildren();
    // A new ranking means the reader edited the query: start the list back at the top
    // (renderHits runs only on rank(), never on prev/next stepping, so this scrolls the
    // internal results list up on edit only, not while navigating hits).
    results.scrollTop = 0;
    for (const el of highlighted) el.classList.remove("semsearch-current");
    highlighted = [];
    let prevSec = null; // group consecutive hits that share a section heading
    hits.forEach((hit, i) => {
      hit.el.classList.add("semsearch-hit");
      highlighted.push(hit.el);
      const sameSection = hit.sec === prevSec;
      const li = document.createElement("li");
      if (sameSection) li.className = "semsearch-cont";
      const a = document.createElement("button");
      a.type = "button";
      a.className = "semsearch-hit-link";
      const head = document.getElementById(hit.sec);
      const title = document.createElement("strong");
      const name = document.createElement("span");
      name.className = "semsearch-hit-name";
      // Print the section heading ONCE per run of same-section hits; continuation
      // paragraphs leave it blank (a dashed divider separates them) but keep their own
      // score, which the empty flex-grow name still pushes to the right edge.
      name.textContent = sameSection ? "" : head ? head.textContent.trim() : hit.sec;
      title.append(name, buildScore(hit));
      const snip = document.createElement("span");
      snip.className = "semsearch-snippet";
      snip.textContent = displayText(hit);
      a.append(title, snip);
      // Clicking a result collapses the whole box first, THEN scrolls to the passage:
      // closing settles the layout (the panel above the content disappears) before the
      // smooth scroll, and it gets the results panel out of the reader's way so the hit
      // in the drug text is unobstructed. Prev/next nav call setCurrent() directly (they
      // must keep the box open to stay usable), so only a result CLICK collapses it.
      a.addEventListener("click", () => {
        box.open = false;
        setCurrent(i, true);
      });
      li.append(a);
      results.append(li);
      prevSec = hit.sec;
    });
    // When the cap bit, tell the reader at the very end of the list that more matches
    // exist, so they can refine. A plain non-clickable <li>: setCurrent()/nav index off
    // hits.length, so this trailing item is never selectable.
    if (hits.length >= MAX_RESULTS) {
      const capped = document.createElement("li");
      capped.className = "semsearch-capped";
      capped.textContent =
        MAX_RESULTS +
        " résultats affichés (maximum). Affinez votre requête pour d'autres résultats.";
      results.append(capped);
    }
    nav.hidden = hits.length === 0;
  }

  function setCurrent(i, scroll) {
    if (!hits.length) return;
    current = ((i % hits.length) + hits.length) % hits.length;
    hits.forEach((hit, j) => {
      hit.el.classList.toggle("semsearch-current", j === current);
    });
    [...results.children].forEach((li, j) => {
      li.classList.toggle("semsearch-current-item", j === current);
    });
    counter.textContent = current + 1 + " / " + hits.length;
    if (scroll) {
      const hit = hits[current];
      // Open any collapsed <details> ancestor so the target is actually visible.
      let p = hit.el.parentElement;
      while (p) {
        if (p.tagName === "DETAILS") p.open = true;
        p = p.parentElement;
      }
      hit.el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  prevBtn.addEventListener("click", () => {
    if (!hits.length) return;
    // From the unselected state (current = -1) "Précédent" goes to the LAST hit,
    // not second-to-last, which a bare current-1 would land on.
    setCurrent(current < 0 ? hits.length - 1 : current - 1, true);
    track("recherche-rcp-nav", { sens: "precedent" });
  });
  nextBtn.addEventListener("click", () => {
    if (!hits.length) return;
    setCurrent(current + 1, true);
    track("recherche-rcp-nav", { sens: "suivant" });
  });

  // --- input wiring ---------------------------------------------------------
  // Encoding is a round-trip, so search after a short typing pause, not per key.
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    clearTimeout(timer);
    // Enter steps to the next hit once results exist; otherwise it forces a search.
    if (hits.length && input.value.trim() === lastRanked) setCurrent(current + 1, true);
    else runSearch();
  });

  // Warm the readiness path the first time the reader opens the box (never on load).
  box.addEventListener("toggle", function warm() {
    if (box.open) {
      box.removeEventListener("toggle", warm);
      ensureReady().catch(() => {});
    }
  });

  // A fresh open is a new search session for the "used" event (see rank()).
  box.addEventListener("toggle", () => {
    if (!box.open) usageTracked = false;
  });
})();

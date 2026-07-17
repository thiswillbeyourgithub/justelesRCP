// Per-drug semantic search: a collapsible "Rechercher dans ce RCP" box that lets
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
//      the server holds no per-reader state and returns only a tiny vector.
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
  const TOP_K = 8; // distinct passages surfaced per query
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
  summary.textContent = "Rechercher dans ce RCP";
  const panel = document.createElement("div");
  panel.className = "semsearch-panel";
  const input = document.createElement("input");
  input.type = "search";
  input.className = "semsearch-input";
  input.placeholder = "ex. : « contraception et reproduction », « prise des repas »";
  input.setAttribute("enterkeyhint", "search");
  input.setAttribute("aria-label", "Rechercher dans ce RCP");
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
  const anchor = document.querySelector("[data-rcp-asof]") || main.querySelector(".cis");
  if (anchor && anchor.parentNode) anchor.after(box);
  else main.prepend(box);

  // --- state ----------------------------------------------------------------
  let phase = "idle"; // idle | preparing | ready | error
  let chunks = null; // [{sec, snippet, vec: Float32Array}]
  let pageDim = 0; // dim of this page's section vectors, to guard a model/dim mismatch
  let readyPromise = null;
  let queryController = null; // aborts a superseded /api/sem/embed
  let hits = []; // [{sec, snippet, score, el}]
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
    const q = input.value.trim();
    if (q.length < MIN_CHARS) {
      clearHits();
      setStatus(q.length ? "Tapez au moins " + MIN_CHARS + " caractères." : "");
      return;
    }
    if (phase !== "ready") {
      ensureReady().catch(() => {}); // status set by the state machine
      return;
    }
    // Cancel any still-in-flight query: only the latest keystroke matters.
    if (queryController) queryController.abort();
    const controller = new AbortController();
    queryController = controller;
    setStatus("Recherche…");
    let qv;
    try {
      const res = await fetch("/api/sem/embed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: q.slice(0, MAX_CHARS) }),
        signal: controller.signal,
      });
      if (!res.ok) {
        setStatus("Erreur lors de l'encodage de la requête.");
        return;
      }
      const data = await res.json();
      qv = decodeVec(data.q);
    } catch (err) {
      if (err && err.name === "AbortError") return; // superseded, ignore
      setStatus("Recherche indisponible (service d'indexation absent).");
      return;
    }
    if (controller !== queryController) return; // a newer query already superseded us
    // The query encoder and this page's section vectors must share a dimension (same
    // model). If they disagree, the page was embedded by a different model and needs
    // re-indexing: surface it instead of silently ranking a truncated dot product.
    if (pageDim && qv.length !== pageDim) {
      clearHits();
      setStatus("Index de cette page à mettre à jour, réessayez plus tard.");
      return;
    }
    rank(qv);
  }

  function rank(qv) {
    lastRanked = input.value.trim(); // so Enter can tell "next hit" from "new search"
    // cosine == dot product (both int8-dequantised, ~unit-norm).
    const scored = chunks.map((c) => {
      const v = c.vec;
      const n = Math.min(v.length, qv.length);
      let dot = 0;
      for (let i = 0; i < n; i++) dot += v[i] * qv[i];
      return { sec: c.sec, snippet: c.snippet, score: dot };
    });
    scored.sort((a, b) => b.score - a.score);
    // Resolve each to a DOM block, deduping by target element (Set compares by
    // identity) so two chunks in one paragraph count once; keep the best-scored
    // TOP_K distinct passages.
    clearHits();
    const seen = new Set();
    for (const s of scored) {
      if (hits.length >= TOP_K) break;
      const el = locate(s.sec, s.snippet);
      if (!el || seen.has(el)) continue;
      seen.add(el);
      hits.push({ sec: s.sec, snippet: s.snippet, score: s.score, el: el });
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

  function renderHits() {
    results.replaceChildren();
    for (const el of highlighted) el.classList.remove("semsearch-current");
    highlighted = [];
    hits.forEach((hit, i) => {
      hit.el.classList.add("semsearch-hit");
      highlighted.push(hit.el);
      const li = document.createElement("li");
      const a = document.createElement("button");
      a.type = "button";
      a.className = "semsearch-hit-link";
      const head = document.getElementById(hit.sec);
      const title = document.createElement("strong");
      title.textContent = head ? head.textContent.trim() : hit.sec;
      const snip = document.createElement("span");
      snip.className = "semsearch-snippet";
      snip.textContent = hit.snippet;
      a.append(title, snip);
      a.addEventListener("click", () => setCurrent(i, true));
      li.append(a);
      results.append(li);
    });
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

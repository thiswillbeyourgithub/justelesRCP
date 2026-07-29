// Client-side instant search over the prebuilt name index (~15k entries).
// No dependencies, no network beyond the one-time index fetch.
//
// Two views over the SAME index and the SAME matcher:
//   - the as-you-type dropdown (#results), a short list of quick picks;
//   - the full results page (#searchpage), shown when the URL carries a query
//     (/?q=sertraline, /?query=... accepted as an alias) or when the reader hits
//     Enter without having picked a dropdown entry. That URL is the shareable
//     "all the sertraline drugs" link.
(() => {
  const q = document.getElementById("q");
  const results = document.getElementById("results");
  const status = document.getElementById("status");
  const page = document.getElementById("searchpage");
  const pageTitle = document.getElementById("searchpage-title");
  const pageList = document.getElementById("searchpage-results");
  const pageNote = document.getElementById("searchpage-note");
  const DROPDOWN_MAX = 15;
  const PAGE_MAX = 300; // a very common substance can carry ~1k presentations
  const baseTitle = document.title; // restored when the results page is dismissed
  let index = [];
  let norm = []; // parallel array of normalized names for matching
  let normSub = []; // parallel array of normalized substance (DCI) strings, "" if none
  let active = -1;
  let pageTerm = ""; // term the results page currently shows ("" = page hidden)

  const normalize = (s) =>
    s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

  // Deep link: /?q=term (or ?query=term) shows the full results page, so another
  // page can link straight to a search (e.g. an EU-authorization stub linking to
  // its substance's generics) and a reader can share/bookmark a result list.
  const urlTerm = () => {
    const p = new URLSearchParams(location.search);
    return (p.get("q") || p.get("query") || "").trim();
  };

  fetch("/search-index.json")
    .then((r) => r.json())
    .then((data) => {
      index = data;
      norm = data.map((e) => normalize(e.name));
      normSub = data.map((e) => (e.sub ? normalize(e.sub) : ""));
      status.textContent = index.length.toLocaleString("fr") + " médicaments indexés.";
      if (pageTerm) renderPage();
      else if (q.value) render();
    })
    .catch(() => (status.textContent = "Erreur de chargement de l'index."));

  // Returns {hits, total}: `hits` are the top `limit` matches, `total` the number of
  // matching drugs. `full` scans the whole index (the results page needs an exact
  // count); the dropdown stops early since it only shows the first few.
  function search(term, limit, full) {
    const t = normalize(term).trim();
    if (!t) return { hits: [], total: 0 };
    const found = [];
    let total = 0;
    // Match the brand name OR the active substance (DCI), so a search on the substance
    // (e.g. "acetylcysteine") surfaces every brand carrying it, not just names.
    for (let i = 0; i < norm.length; i++) {
      if (!norm[i].includes(t) && !normSub[i].includes(t)) continue;
      total++;
      found.push(i);
      if (!full && found.length >= limit * 4) break;
    }
    // Name matches first (a brand hit beats a substance-only hit), then name-prefix,
    // then shorter names. Sorting indices lets the comparator reuse the already
    // normalized `norm` strings instead of re-folding each name per comparison.
    found.sort((a, b) => {
      const am = norm[a].includes(t) ? 0 : 1, bm = norm[b].includes(t) ? 0 : 1;
      const ap = norm[a].startsWith(t) ? 0 : 1, bp = norm[b].startsWith(t) ? 0 : 1;
      return am - bm || ap - bp || index[a].name.length - index[b].name.length;
    });
    return { hits: found.slice(0, limit).map((i) => index[i]), total };
  }

  // One result row, shared by the dropdown and the results page so the two never
  // drift apart (same link target, same [RETIRÉ] tag, same DCI subtitle).
  function resultItem(h) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    // Most hits are RCP pages (/rcp/); EU-authorization stubs (h.eu) live under
    // /eu/ so they stay out of the RCP link graph. Both resolve via Caddy.
    a.href = (h.eu ? "/eu/" : "/rcp/") + h.slug;
    const name = document.createElement("span");
    name.className = "result-name";
    name.textContent = h.name;
    // Delisted drug (zero-byte overlay -> build.py tagged the row ret:1): flag it
    // so the reader knows the page is our archived 2022 copy, not a live product.
    if (h.ret) {
      const tag = document.createElement("span");
      tag.className = "result-retired";
      tag.textContent = " [RETIRÉ]";
      name.appendChild(tag);
    }
    a.appendChild(name);
    // Show the active substance (DCI) under the brand so the reader sees what they
    // searched / can learn the substance to search exhaustively.
    if (h.sub) {
      const sub = document.createElement("span");
      sub.className = "result-sub";
      sub.textContent = h.sub;
      a.appendChild(sub);
    }
    li.appendChild(a);
    return li;
  }

  function render() {
    const { hits } = search(q.value, DROPDOWN_MAX, false);
    active = -1;
    results.innerHTML = "";
    for (const h of hits) results.appendChild(resultItem(h));
    results.classList.toggle("open", hits.length > 0);
  }

  function renderPage() {
    pageList.innerHTML = "";
    pageNote.textContent = "";
    if (!pageTerm) {
      page.hidden = true;
      return;
    }
    page.hidden = false;
    if (!index.length) {
      pageTitle.textContent = "Résultats pour « " + pageTerm + " »";
      pageNote.textContent = "Chargement de l'index…";
      return;
    }
    const { hits, total } = search(pageTerm, PAGE_MAX, true);
    pageTitle.textContent =
      total.toLocaleString("fr") +
      (total > 1 ? " médicaments pour « " : " médicament pour « ") +
      pageTerm +
      " »";
    if (!total) {
      pageTitle.textContent = "Aucun résultat pour « " + pageTerm + " »";
      pageNote.textContent =
        "Essayez le nom commercial ou la substance active (DCI), ou parcourez la liste A-Z.";
      return;
    }
    for (const h of hits) pageList.appendChild(resultItem(h));
    if (total > hits.length) {
      pageNote.textContent =
        "Seuls les " + hits.length + " premiers résultats sont affichés ; " +
        "précisez votre recherche pour en voir d'autres.";
    }
  }

  // Show (or hide, with an empty term) the results page. `push` writes the shareable
  // /?q=… URL into the history so the view is linkable and the back button works.
  function showPage(term, push) {
    pageTerm = term;
    if (push) {
      const url = term ? "/?q=" + encodeURIComponent(term) : "/";
      history.pushState({ q: term }, "", url);
    }
    document.title = term ? term + " - Recherche - justelesRCP" : baseTitle;
    document.body.classList.toggle("searching", !!term);
    results.classList.remove("open");
    active = -1;
    renderPage();
    if (term) {
      q.blur(); // let the on-screen keyboard close so the results are visible
      if (typeof window.trackEvent === "function") window.trackEvent("recherche-page");
    }
  }

  // Arriving on /?q=… : show the page straight away (no history entry to add).
  const initial = urlTerm();
  if (initial) {
    if (!q.value) q.value = initial;
    showPage(initial, false);
  }

  // Back/forward between the home page and a results URL.
  window.addEventListener("popstate", () => {
    const term = urlTerm();
    if (term && !q.value) q.value = term;
    showPage(term, false);
  });

  q.addEventListener("input", render);
  q.addEventListener("keydown", (e) => {
    const items = [...results.querySelectorAll("a")];
    if (e.key === "ArrowDown") {
      active = Math.min(active + 1, items.length - 1);
    } else if (e.key === "ArrowUp") {
      active = Math.max(active - 1, 0);
    } else if (e.key === "Enter") {
      // A drug picked with the arrow keys opens that drug; Enter on a freely typed
      // query opens the full results page instead (the shareable /?q=… view).
      if (active >= 0 && items[active]) items[active].click();
      else if (q.value.trim()) showPage(q.value.trim(), true);
      return;
    } else {
      return;
    }
    e.preventDefault();
    items.forEach((a, i) => a.classList.toggle("active", i === active));
    if (items[active]) items[active].scrollIntoView({ block: "nearest" });
  });
})();

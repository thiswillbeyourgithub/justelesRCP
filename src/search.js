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
  let hay = []; // parallel "name substance" haystacks the query words are matched in
  let groupKey = []; // parallel dosage-group keys (see dosageKey)
  let active = -1;
  let pageTerm = ""; // term the results page currently shows ("" = page hidden)

  const normalize = window.jlrcp.fold;

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
      hay = norm.map((n, i) => n + " " + normSub[i]);
      // A parallel import opens another drug's page (h.aip): never fold it in a group.
      groupKey = data.map((e, i) => (e.aip ? "aip:" + i : dosageKey(e.name)));
      status.textContent = index.length.toLocaleString("fr") + " médicaments indexés.";
      if (pageTerm) renderPage();
      else if (q.value) render();
    })
    .catch(() => (status.textContent = "Erreur de chargement de l'index."));

  // Dosage groups: presentations whose names differ ONLY by their numbers (ZYPADHERA
  // 210 mg / 300 mg / 405 mg, same form) are one row, "ZYPADHERA 210/300/405 mg, ...",
  // that unfolds into its presentations. A number glued to letters (VITAMINE B12) is
  // part of the name, not a dosage. Matches are [{start, end, text}] in name order.
  const NUM = /[a-zà-ÿ]*\d+(?:[.,]\d+)*/gi;
  function dosageSpans(name) {
    const out = [];
    for (const m of name.matchAll(NUM)) {
      if (/^\d/.test(m[0])) out.push({ start: m.index, end: m.index + m[0].length, text: m[0] });
    }
    return out;
  }
  function dosageKey(name) {
    return name.replace(NUM, (m) => (/^\d/.test(m) ? "#" : m));
  }
  // Sorts a group's rows by strength and builds its title: the first name with the
  // varying stretch (first to last number that differs) replaced by every row's own
  // stretch, joined by "/" when one number varies ("210/300/405 mg") and by " | " when
  // several do, where a "/" would be ambiguous ("10 mg/5 ml | 20 mg/10 ml").
  function describeGroup(rows) {
    const spans = new Map(rows.map((r) => [r, dosageSpans(r.name)]));
    const first = spans.get(rows[0]);
    const vary = first.map((_, j) => j)
      .filter((j) => rows.some((r) => spans.get(r)[j].text !== first[j].text));
    if (!vary.length) return { rows, title: rows[0].name }; // same name twice
    const a = vary[0], b = vary[vary.length - 1];
    const value = (r) => parseFloat(spans.get(r)[a].text.replace(",", "."));
    const sorted = rows.slice().sort((x, y) => value(x) - value(y));
    // Several varying numbers: each stretch keeps the unit after its last number, so
    // it reads "10 mg/5 ml | 20 mg/10 ml" rather than "10 mg/5 | 20 mg/10 ml".
    const end = (r) => {
      const e = spans.get(r)[b].end;
      const unit = vary.length > 1 ? r.name.slice(e).match(/^ [a-zµ%][a-zµ%.]*/i) : null;
      return e + (unit ? unit[0].length : 0);
    };
    const stretch = (r) => r.name.slice(spans.get(r)[a].start, end(r));
    const lead = sorted[0];
    const title = lead.name.slice(0, spans.get(lead)[a].start) +
      sorted.map(stretch).join(vary.length === 1 ? "/" : " | ") + lead.name.slice(end(lead));
    return { rows: sorted, title };
  }

  // Returns {groups, count, total}: `groups` are the top `limit` dosage groups (arrays
  // of index rows, see describeGroup), `count` the number of groups and `total` the
  // number of matching drugs. Always a full scan: a group must hold all its members.
  function search(term, limit) {
    const t = normalize(term).trim().replace(/\s+/g, " ");
    if (!t) return { groups: [], count: 0, total: 0 };
    // Every word must appear in the brand name OR the active substance (DCI), in any
    // order: a lone substance (e.g. "acetylcysteine") surfaces every brand carrying it,
    // and "olanzapine injectable" finds ZYPADHERA (substance OLANZAPINE, name "...
    // suspension injectable ..."), which no single substring of either field matches.
    const words = t.split(" ");
    const hasAll = (s) => words.every((w) => s.includes(w));
    const found = [];
    for (let i = 0; i < hay.length; i++) if (hasAll(hay[i])) found.push(i);
    // The whole term in the name first, then every word in the name (a brand hit beats
    // a substance-only hit), then name-prefix, then shorter names. Sorting indices lets
    // the comparator reuse the already normalized `norm` strings instead of re-folding
    // each name per comparison.
    const tier = (i) => (norm[i].includes(t) ? 0 : hasAll(norm[i]) ? 1 : 2);
    found.sort((a, b) => {
      const ap = norm[a].startsWith(t) ? 0 : 1, bp = norm[b].startsWith(t) ? 0 : 1;
      return tier(a) - tier(b) || ap - bp || index[a].name.length - index[b].name.length;
    });
    // A group ranks where its best-ranked member does.
    const byKey = new Map();
    const groups = [];
    for (const i of found) {
      let g = byKey.get(groupKey[i]);
      if (!g) byKey.set(groupKey[i], (g = [])), groups.push(g);
      g.push(index[i]);
    }
    return { groups: groups.slice(0, limit), count: groups.length, total: found.length };
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
    // Parallel import (build.py aip_search_rows): no RCP of its own, the row opens
    // its reference product's page (h.aip names it), so say so before the click.
    if (h.aip) {
      const aip = document.createElement("span");
      aip.className = "result-sub result-aip";
      aip.textContent = "Importation parallèle : RCP de " + h.aip;
      a.appendChild(aip);
    }
    if (h.sub) {
      const sub = document.createElement("span");
      sub.className = "result-sub";
      sub.textContent = h.sub;
      a.appendChild(sub);
    }
    li.appendChild(a);
    return li;
  }

  // One dosage group: a plain row when it holds one drug, else a toggle row (same
  // look, plus "N présentations") over a hidden list of its presentations' rows.
  function groupItem(rows) {
    if (rows.length === 1) return resultItem(rows[0]);
    const g = describeGroup(rows);
    const same = (k) => g.rows.every((r) => r[k] === g.rows[0][k]);
    const li = resultItem({
      name: g.title,
      sub: same("sub") ? g.rows[0].sub : "",
      ret: g.rows.every((r) => r.ret),
    });
    const a = li.firstChild;
    a.href = "#";
    a.className = "results-group";
    a.setAttribute("aria-expanded", "false");
    const count = document.createElement("span");
    count.className = "result-count";
    count.textContent = g.rows.length + " présentations";
    a.firstChild.after(count);
    const list = document.createElement("ul");
    list.className = "results-variants";
    list.hidden = true;
    for (const r of g.rows) list.appendChild(resultItem(r));
    li.appendChild(list);
    return li;
  }

  // Shared by the dropdown and the results page: unfold/fold a group, or (dropdown)
  // load more suggestions.
  function onListClick(e) {
    const group = e.target.closest(".results-group");
    if (group) {
      e.preventDefault();
      const list = group.nextElementSibling;
      list.hidden = !list.hidden;
      group.setAttribute("aria-expanded", String(!list.hidden));
    } else if (e.target.closest(".results-more")) {
      e.preventDefault();
      showMore();
    }
  }
  results.addEventListener("click", onListClick);
  pageList.addEventListener("click", onListClick);

  // The dropdown's links the reader can currently see (a folded group's are hidden).
  const visibleLinks = () =>
    [...results.querySelectorAll("a")].filter((a) => !a.closest("[hidden]"));

  // The dropdown grows by DROPDOWN_MAX groups per "Afficher plus" press, for a drug sold
  // in many strengths/forms whose exact name the reader does not remember. A new query
  // starts again from DROPDOWN_MAX.
  let shown = DROPDOWN_MAX;

  function render() {
    const { groups, count } = search(q.value, shown);
    results.innerHTML = "";
    for (const g of groups) results.appendChild(groupItem(g));
    if (count > groups.length) {
      const li = document.createElement("li");
      const more = document.createElement("a");
      more.href = "#";
      more.className = "results-more";
      more.textContent = "Afficher plus de résultats…";
      li.appendChild(more);
      results.appendChild(li);
    }
    results.classList.toggle("open", groups.length > 0);
  }

  function showMore() {
    const first = shown; // index of the first new row, where arrow navigation resumes
    shown += DROPDOWN_MAX;
    render(); // re-renders folded: the new rows start right after the first `shown`
    const items = visibleLinks();
    if (active >= 0) {
      active = Math.min(first, items.length - 1);
      items[active].classList.add("active");
    }
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
    const { groups, count, total } = search(pageTerm, PAGE_MAX);
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
    for (const g of groups) pageList.appendChild(groupItem(g));
    if (count > groups.length) {
      pageNote.textContent =
        "Seuls les " + groups.length + " premiers résultats sont affichés ; " +
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
    // Always mirror the URL (an empty term clears the field), so Back never leaves
    // the box showing a query that is not the one on screen.
    q.value = term;
    showPage(term, false);
  });

  q.addEventListener("input", () => {
    shown = DROPDOWN_MAX;
    active = -1;
    render();
  });
  q.addEventListener("keydown", (e) => {
    const items = visibleLinks();
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

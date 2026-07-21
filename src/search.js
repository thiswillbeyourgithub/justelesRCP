// Client-side instant search over the prebuilt name index (~15k entries).
// No dependencies, no network beyond the one-time index fetch.
(() => {
  const q = document.getElementById("q");
  const results = document.getElementById("results");
  const status = document.getElementById("status");
  let index = [];
  let norm = []; // parallel array of normalized names for matching
  let normSub = []; // parallel array of normalized substance (DCI) strings, "" if none
  let active = -1;

  // Deep link: /?q=term prefills the box so another page can link straight to a
  // search (e.g. an EU-authorization stub linking to its substance's generics).
  const initialQ = new URLSearchParams(location.search).get("q");
  if (initialQ && !q.value) q.value = initialQ;

  const normalize = (s) =>
    s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

  fetch("/search-index.json")
    .then((r) => r.json())
    .then((data) => {
      index = data;
      norm = data.map((e) => normalize(e.name));
      normSub = data.map((e) => (e.sub ? normalize(e.sub) : ""));
      status.textContent = index.length.toLocaleString("fr") + " médicaments indexés.";
      if (q.value) render();
    })
    .catch(() => (status.textContent = "Erreur de chargement de l'index."));

  function search(term) {
    const t = normalize(term).trim();
    if (!t) return [];
    const out = [];
    // Match the brand name OR the active substance (DCI), so a search on the substance
    // (e.g. "acetylcysteine") surfaces every brand carrying it, not just names.
    for (let i = 0; i < norm.length && out.length < 40; i++) {
      if (norm[i].includes(t) || normSub[i].includes(t)) out.push(index[i]);
    }
    // Name matches first (a brand hit beats a substance-only hit), then name-prefix,
    // then shorter names.
    out.sort((a, b) => {
      const an = normalize(a.name), bn = normalize(b.name);
      const am = an.includes(t) ? 0 : 1;
      const bm = bn.includes(t) ? 0 : 1;
      const ap = an.startsWith(t) ? 0 : 1;
      const bp = bn.startsWith(t) ? 0 : 1;
      return am - bm || ap - bp || a.name.length - b.name.length;
    });
    return out.slice(0, 15);
  }

  function render() {
    const hits = search(q.value);
    active = -1;
    results.innerHTML = "";
    for (const h of hits) {
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
      results.appendChild(li);
    }
    results.classList.toggle("open", hits.length > 0);
  }

  q.addEventListener("input", render);
  q.addEventListener("keydown", (e) => {
    const items = [...results.querySelectorAll("a")];
    if (e.key === "ArrowDown") {
      active = Math.min(active + 1, items.length - 1);
    } else if (e.key === "ArrowUp") {
      active = Math.max(active - 1, 0);
    } else if (e.key === "Enter" && items.length) {
      (items[active] || items[0]).click();
      return;
    } else {
      return;
    }
    e.preventDefault();
    items.forEach((a, i) => a.classList.toggle("active", i === active));
    if (items[active]) items[active].scrollIntoView({ block: "nearest" });
  });
})();

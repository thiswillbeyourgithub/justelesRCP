// Client-side instant search over the prebuilt name index (~15k entries).
// No dependencies, no network beyond the one-time index fetch.
(() => {
  const q = document.getElementById("q");
  const results = document.getElementById("results");
  const status = document.getElementById("status");
  let index = [];
  let norm = []; // parallel array of normalized names for matching
  let active = -1;

  const normalize = (s) =>
    s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

  fetch("/search-index.json")
    .then((r) => r.json())
    .then((data) => {
      index = data;
      norm = data.map((e) => normalize(e.name));
      status.textContent = index.length.toLocaleString("fr") + " médicaments indexés.";
      if (q.value) render();
    })
    .catch(() => (status.textContent = "Erreur de chargement de l'index."));

  function search(term) {
    const t = normalize(term).trim();
    if (!t) return [];
    const out = [];
    for (let i = 0; i < norm.length && out.length < 30; i++) {
      if (norm[i].includes(t)) out.push(index[i]);
    }
    // Prefix matches first.
    out.sort((a, b) => {
      const ap = normalize(a.name).startsWith(t) ? 0 : 1;
      const bp = normalize(b.name).startsWith(t) ? 0 : 1;
      return ap - bp || a.name.length - b.name.length;
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
      a.href = "/rcp/" + h.slug;
      a.textContent = h.name;
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

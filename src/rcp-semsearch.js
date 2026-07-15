// Per-drug semantic search: a collapsible "Rechercher dans ce RCP" box that lets
// the reader ask a natural-language question ("puis-je le prendre enceinte ?") and
// jumps to the most relevant sections of THIS drug's text.
//
// 100% client-side and same-origin, so the strict CSP holds. Two halves:
//   1. Per-page vectors: build.py bakes dist/<rcp|eu>/<slug>.vec.json (int8, one
//      vector per section chunk, produced offline by embed-rcp.py). Fetched lazily.
//   2. Query encoder: transformers.js + a self-hosted ONNX model, both vendored
//      under /vendor and /models and loaded ONLY when the reader first opens the
//      box (~120 Mo once, then cached by the browser), so normal browsing stays
//      instant. Needs `script-src 'wasm-unsafe-eval'` (see docker/Caddyfile).
//
// Everything degrades gracefully: no .vec.json (page not embedded yet) or a failed
// model load just shows an "indisponible" note and changes nothing else. The box is
// created in JS (not baked) so no-JS pages show no dead control; styling is via the
// .semsearch* classes (an inline style="" would trip the strict style-src CSP).
(function () {
  "use strict";
  const main = document.querySelector(".rcp[data-cis]");
  if (!main) return; // not an RCP / full-EU page
  if (document.querySelector(".rcp-stub")) return; // a stub has no captured text yet
  const cis = (main.getAttribute("data-cis") || "").trim();
  if (!/^\d{8}$/.test(cis)) return;

  // The ONNX build transformers.js loads. MUST correspond to embed-rcp.py's
  // DEFAULT_MODEL (same weights): the baked passage vectors and the query vector
  // have to come from the same model. Vendored under /models/<this id>/.
  const RUNTIME_MODEL = "Xenova/multilingual-e5-small";
  // Page is served extensionless (Caddy try_files) or as .html; the vectors live
  // next to it as <slug>.vec.json (build.write_vec_sidecars).
  const vecUrl = location.pathname.replace(/\.html$/, "") + ".vec.json";

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
  input.placeholder = "Ex. : puis-je le prendre pendant la grossesse ?";
  input.setAttribute("enterkeyhint", "search");
  input.setAttribute("aria-label", "Rechercher dans ce RCP");
  const status = document.createElement("p");
  status.className = "semsearch-status";
  const results = document.createElement("ol");
  results.className = "semsearch-results";
  panel.append(input, status, results);
  box.append(summary, panel);
  const anchor = document.querySelector("[data-rcp-asof]") || main.querySelector(".cis");
  if (anchor && anchor.parentNode) anchor.after(box);
  else main.prepend(box);

  // --- state ----------------------------------------------------------------
  let phase = "idle"; // idle | loading | ready | error
  let extractor = null;
  let chunks = null; // [{sec, snippet, vec: Float32Array}]
  let queryPrefix = "";
  let loadPromise = null;
  let lastQuery = "";

  function setStatus(text) {
    status.textContent = text || "";
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

  // Load the per-page vectors then the encoder. Idempotent + memoised so repeated
  // opens/queries reuse the one in-flight (or finished) load.
  function ensureLoaded() {
    if (loadPromise) return loadPromise;
    phase = "loading";
    loadPromise = (async () => {
      setStatus("Chargement de l'index…");
      const res = await fetch(vecUrl);
      if (!res.ok) throw new Error("novec");
      const data = await res.json();
      queryPrefix = data.query_prefix || "";
      chunks = (data.chunks || []).map((c) => ({
        sec: c.sec,
        snippet: c.snippet,
        vec: decodeVec(c.q),
      }));
      if (!chunks.length) throw new Error("empty");

      setStatus("Téléchargement du modèle (~120 Mo, une seule fois)…");
      const t = await import("/vendor/transformers.min.js");
      const env = t.env;
      // Fully offline + same-origin: no HF hub, no CDN wasm, no blob worker (so the
      // CSP only needs 'wasm-unsafe-eval', not worker-src blob:).
      env.allowRemoteModels = false;
      env.allowLocalModels = true;
      env.localModelPath = "/models/";
      env.useBrowserCache = true; // persists the model across pages/visits
      const wasm = env.backends && env.backends.onnx && env.backends.onnx.wasm;
      if (wasm) {
        wasm.wasmPaths = "/vendor/ort/";
        wasm.numThreads = 1;
        wasm.proxy = false;
      }
      extractor = await t.pipeline("feature-extraction", RUNTIME_MODEL, {
        quantized: true,
      });
      phase = "ready";
      setStatus("");
      return true;
    })().catch((err) => {
      phase = "error";
      setStatus(
        err && err.message === "novec"
          ? "Recherche sémantique pas encore disponible pour cette page."
          : "Recherche sémantique indisponible (modèle non chargé)."
      );
      throw err;
    });
    return loadPromise;
  }

  async function runSearch() {
    const q = input.value.trim();
    if (!q) {
      results.replaceChildren();
      setStatus("");
      return;
    }
    if (q === lastQuery && phase === "ready") return;
    lastQuery = q;
    try {
      await ensureLoaded();
    } catch (_) {
      return; // status already set
    }
    setStatus("Recherche…");
    let out;
    try {
      out = await extractor(queryPrefix + q, { pooling: "mean", normalize: true });
    } catch (_) {
      setStatus("Erreur lors de l'encodage de la requête.");
      return;
    }
    const qv = out.data; // Float32Array(dim), L2-normalised
    // cosine == dot product (both normalised); keep the best chunk per section.
    const bySec = new Map();
    for (const c of chunks) {
      const v = c.vec;
      const n = Math.min(v.length, qv.length);
      let dot = 0;
      for (let i = 0; i < n; i++) dot += v[i] * qv[i];
      const prev = bySec.get(c.sec);
      if (!prev || dot > prev.score) {
        bySec.set(c.sec, { sec: c.sec, snippet: c.snippet, score: dot });
      }
    }
    const top = [...bySec.values()]
      .sort((a, b) => b.score - a.score)
      .slice(0, 8);
    render(top);
    setStatus(top.length ? "" : "Aucun passage pertinent.");
  }

  function render(items) {
    results.replaceChildren();
    for (const it of items) {
      const secEl = document.getElementById(it.sec);
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#" + it.sec;
      a.className = "semsearch-hit-link";
      const title = document.createElement("strong");
      title.textContent = secEl ? secEl.textContent.trim() : it.sec;
      const snip = document.createElement("span");
      snip.className = "semsearch-snippet";
      snip.textContent = it.snippet;
      a.append(title, snip);
      a.addEventListener("click", () => flash(it.sec));
      li.append(a);
      results.append(li);
    }
  }

  // Briefly highlight the target section after a jump. Guarded: a page refreshed
  // with newer content than its baked vectors may lack this sec id.
  function flash(sec) {
    const el = document.getElementById(sec);
    if (!el) return;
    el.classList.add("semsearch-flash");
    setTimeout(() => el.classList.remove("semsearch-flash"), 2000);
  }

  // Encoding is ~100 ms, so search on Enter or after a typing pause, not per key.
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 450);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(timer);
      runSearch();
    }
  });
  // Warm the load the first time the reader opens the box (never on page load).
  box.addEventListener("toggle", function warm() {
    if (box.open) {
      box.removeEventListener("toggle", warm);
      ensureLoaded().catch(() => {});
    }
  });
})();

/* status.js - the /status dashboard.
 *
 * Fetches the two runtime services' PUBLIC, curated summary endpoints and renders a
 * live picture of the infrastructure:
 *   GET /api/summary      (refresh-service) -> crawl progress + refresh activity
 *   GET /api/sem/summary  (embed-service)   -> embedding progress + visitor queries
 *
 * Both are same-origin (covered by the strict `connect-src 'self'` CSP) and proxied by
 * Caddy (rate-limited). The detailed /api/stats + /api/sem/stats stay internal-only.
 *
 * CSP-safe: same-origin script, no inline handlers/eval/styles (widths are set through
 * CSSOM setters, everything else via classes + textContent). Degrades gracefully: if a
 * service is down its cards show "indisponible", the static site is unaffected.
 */
(function () {
  "use strict";

  var REFRESH_MS = 15000;      // poll cadence while the tab is visible
  var SUMMARY_URL = "/api/summary";
  var SEM_URL = "/api/sem/summary";

  // ---- tiny DOM + format helpers -----------------------------------------
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function setBody(id, node) {
    var host = document.getElementById(id);
    if (!host) return;
    host.textContent = "";
    host.appendChild(node);
  }

  function num(n) {
    if (typeof n !== "number" || !isFinite(n)) return "-";
    return Math.round(n).toLocaleString("fr-FR");
  }

  // A pages/min rate string, or null when unknown/zero. One decimal below 10 (small
  // crawl rates lose all meaning rounded to an integer), integer above.
  function pm(n) {
    if (typeof n !== "number" || !isFinite(n) || n <= 0) return null;
    return (n < 10 ? n.toFixed(1) : num(n)) + " pages/min";
  }

  // A short French duration: up to two units (j/h/min/s), rounded down.
  function dur(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "-";
    var s = Math.floor(seconds);
    if (s < 1) return "moins d'une seconde";
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600);  s -= h * 3600;
    var m = Math.floor(s / 60);    s -= m * 60;
    var parts = [];
    if (d) parts.push(d + " j");
    if (h) parts.push(h + " h");
    if (m) parts.push(m + " min");
    if (s && parts.length < 2) parts.push(s + " s");
    if (!parts.length) parts.push("0 s");
    return parts.slice(0, 2).join(" ");
  }

  // A metric row: "label" on the left, "value" (bold) on the right.
  function metric(label, value, cls) {
    var row = el("div", "status-metric" + (cls ? " " + cls : ""));
    row.appendChild(el("span", "status-metric-label", label));
    row.appendChild(el("span", "status-metric-value", value));
    return row;
  }

  // A labelled progress bar. pct is 0..100.
  function progress(pct) {
    pct = Math.max(0, Math.min(100, pct || 0));
    var bar = el("div", "status-bar");
    var fill = el("div", "status-bar-fill");
    fill.style.width = pct.toFixed(1) + "%";   // CSSOM setter (CSP-safe)
    bar.appendChild(fill);
    return bar;
  }

  function badge(text, kind) {
    return el("span", "status-badge status-badge-" + kind, text);
  }

  function note(text, kind) {
    return el("p", "status-note" + (kind ? " status-note-" + kind : ""), text);
  }

  // ---- section renderers --------------------------------------------------

  function renderLane(title, g) {
    var wrap = el("div", "status-lane");
    var head = el("div", "status-lane-head");
    head.appendChild(el("h3", "status-lane-title", title));
    if (!g.enabled) {
      head.appendChild(badge("désactivée", "off"));
      wrap.appendChild(head);
      return wrap;
    }
    head.appendChild(g.idle ? badge("à jour", "ok") : badge("exploration en cours", "run"));
    wrap.appendChild(head);
    wrap.appendChild(progress(g.pct));
    wrap.appendChild(metric("Pages à jour",
      num(g.done) + " / " + num(g.total) + " (" + (g.pct || 0).toFixed(1) + " %)"));
    wrap.appendChild(metric("Pages à ré-explorer", num(g.due)));
    if (g.due > 0) wrap.appendChild(metric("Fin du balayage estimée", "~ " + dur(g.eta_seconds)));
    if (g.forced > 0) wrap.appendChild(metric("Re-balayage forcé restant", num(g.forced)));
    wrap.appendChild(metric("Fraîcheur cible", g.ttl_days + " jours"));
    return wrap;
  }

  function renderCrawl(s) {
    var frag = document.createDocumentFragment();
    frag.appendChild(renderLane("RCP (ANSM)", s.crawl));
    frag.appendChild(renderLane("Autorisations européennes (EMA)", s.crawl_eu));
    setBody("body-crawl", frag);
  }

  function renderRefresh(s) {
    var frag = document.createDocumentFragment();
    var r = s.refreshes, sc = s.shortcircuits, od = s.ondemand;
    frag.appendChild(metric("Rafraîchissements terminés", num(r.done)));
    frag.appendChild(metric("· réussis (contenu)", num(r.ok)));
    frag.appendChild(metric("· vides / retirés de la BDPM", num(r.empty)));
    frag.appendChild(metric("· en erreur", num(r.error), r.error ? "warn" : ""));
    frag.appendChild(el("div", "status-sep"));
    frag.appendChild(metric("Déclenchés par un bouton", num(r.user)));
    frag.appendChild(metric("Déclenchés automatiquement (> 1 an)", num(r.auto)));
    frag.appendChild(metric("Déclenchés par l'explorateur", num(r.crawl)));
    frag.appendChild(el("div", "status-sep"));
    frag.appendChild(metric("Déjà à jour (ignorés)", num(sc.fresh)));
    frag.appendChild(metric("File pleine (reportés)", num(sc.busy)));
    frag.appendChild(metric("Quota horaire atteint", num(sc.budget)));
    frag.appendChild(el("div", "status-sep"));
    frag.appendChild(metric("File à la demande", num(od.queued) + " en attente, " + num(od.pending) + " en cours"));
    if (od.queued > 0) frag.appendChild(metric("Vidage estimé", "~ " + dur(od.eta_seconds)));
    setBody("body-refresh", frag);
  }

  function renderEmbed(s) {
    var frag = document.createDocumentFragment();
    if (!s.enabled) frag.appendChild(note("Indexation de fond désactivée sur ce serveur.", "off"));

    // "Is embedding behind the crawler?" - the headline gauge.
    var gap = s.crawl_gap;
    if (gap) {
      var head = el("div", "status-lane-head");
      head.appendChild(el("h3", "status-lane-title", "Avancement de l'indexation"));
      if (gap.awaiting_embed > 0) {
        head.appendChild(badge("en retard de " + num(gap.awaiting_embed) + " page(s)", "warn"));
      } else {
        head.appendChild(badge("à jour avec l'exploration", "ok"));
      }
      frag.appendChild(head);
      frag.appendChild(progress(gap.embedded_pct));
      frag.appendChild(metric("Pages indexées",
        num(gap.crawled_pages - gap.awaiting_embed) + " / " + num(gap.crawled_pages) +
        " (" + (gap.embedded_pct || 0).toFixed(1) + " %)"));
      frag.appendChild(metric("En attente d'indexation", num(gap.awaiting_embed),
        gap.awaiting_embed ? "warn" : ""));
      if (gap.awaiting_embed > 0 && gap.backlog_eta_seconds > 0)
        frag.appendChild(metric("Résorption du retard estimée", "~ " + dur(gap.backlog_eta_seconds)));
      frag.appendChild(metric("Dernière vérification", "il y a " + dur(gap.scan_age_seconds)));
      frag.appendChild(el("div", "status-sep"));
    }

    var b = s.backlog, p = s.pages;
    frag.appendChild(metric("File d'indexation",
      num(b.queue) + " en attente" + (b.running ? ", 1 en cours" : "")));
    frag.appendChild(el("div", "status-sep"));
    frag.appendChild(metric("Pages indexées (depuis le redémarrage)", num(p.embedded)));
    var idxRate = s.indexing ? pm(s.indexing.pages_per_min) : null;
    frag.appendChild(metric("Vitesse d'indexation", idxRate || "en cours de mesure"));
    frag.appendChild(metric("Débit moyen", num(p.mean_chars_per_s) + " caractères/s"));
    if (p.skipped) frag.appendChild(metric("Ignorées (déjà à jour)", num(p.skipped)));
    frag.appendChild(metric("Erreurs d'indexation", num(p.errors), p.errors ? "warn" : ""));
    setBody("body-embed", frag);
  }

  function renderQueries(s) {
    var frag = document.createDocumentFragment();
    var q = s.queries;
    frag.appendChild(metric("Recherches sémantiques traitées", num(q.embedded)));
    if (q.shed) frag.appendChild(metric("Refusées (surcharge)", num(q.shed), "warn"));
    frag.appendChild(metric("Pages explorées à la demande d'une recherche", num(q.crawl_triggered)));
    setBody("body-queries", frag);
  }

  // "Is indexing keeping up with crawling?" - the one gauge that needs BOTH services.
  // Indexing speed = the embedder's sustained pages/min. Crawl speed = the page
  // production rate = sum of the enabled lanes STILL SWEEPING (an idle/caught-up lane
  // adds no new work); when the refresh service is down no crawling happens, so its
  // production is treated as 0. The ratio + catch-up ETA assume the worst case that
  // every explored page must be re-indexed (true during a fresh seed / forced
  // re-crawl, which is exactly when indexing can fall behind).
  function renderCatchup(crawlS, embedS) {
    if (!embedS) return;  // embed service down: renderDown already filled body-catchup
    var frag = document.createDocumentFragment();
    var gap = embedS.crawl_gap;
    var idxPm = (embedS.indexing && embedS.indexing.pages_per_min) || 0;
    var backlog = gap ? gap.awaiting_embed : 0;

    var crawlPm = 0, crawlKnown = !!crawlS;
    if (crawlS) {
      [crawlS.crawl, crawlS.crawl_eu].forEach(function (g) {
        if (g && g.enabled && !g.idle) crawlPm += (g.pages_per_min || 0);
      });
    }
    var net = idxPm - crawlPm;  // pages/min the backlog shrinks by

    var head = el("div", "status-lane-head");
    head.appendChild(el("h3", "status-lane-title", "L'indexation rattrape-t-elle l'exploration ?"));
    if (backlog <= 0) head.appendChild(badge("à jour", "ok"));
    else if (!idxPm) head.appendChild(badge("mesure en cours", "run"));
    else if (net > 0) head.appendChild(badge("rattrapage en cours", "run"));
    else head.appendChild(badge("ne rattrape pas", "warn"));
    frag.appendChild(head);

    frag.appendChild(metric("Vitesse d'indexation", pm(idxPm) || "en cours de mesure"));
    frag.appendChild(metric("Vitesse d'exploration",
      crawlKnown ? (pm(crawlPm) || "à l'arrêt (tout est à jour)") : "inconnue (service arrêté)"));
    if (idxPm > 0 && crawlKnown && crawlPm > 0) {
      var ratio = idxPm / crawlPm;
      frag.appendChild(metric("Rapport indexation / exploration",
        (ratio < 10 ? ratio.toFixed(1) : num(ratio)) + " ×", ratio >= 1 ? "" : "warn"));
    }

    if (backlog > 0) {
      frag.appendChild(el("div", "status-sep"));
      frag.appendChild(metric("Pages en attente d'indexation", num(backlog), "warn"));
      if (!idxPm) {
        frag.appendChild(note("Vitesse d'indexation en cours de mesure…"));
      } else if (net > 0) {
        frag.appendChild(metric("Rattrapage complet estimé", "~ " + dur(backlog / net * 60)));
      } else {
        frag.appendChild(metric("Rattrapage complet estimé", "jamais au rythme actuel", "warn"));
        frag.appendChild(note("L'exploration produit de nouvelles pages au moins aussi vite " +
          "que l'indexation ne les traite.", "warn"));
      }
    } else {
      frag.appendChild(note("L'indexation est à jour avec l'exploration : chaque page " +
        "explorée est indexée peu après.", "ok"));
    }
    setBody("body-catchup", frag);
  }

  function renderDown(ids, msg) {
    ids.forEach(function (id) { setBody(id, note(msg, "off")); });
  }

  // ---- data fetch + tick --------------------------------------------------
  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  function tick() {
    var uptimes = [];
    var crawlS = null, embedS = null;
    var pRefresh = getJSON(SUMMARY_URL).then(function (s) {
      crawlS = s; renderCrawl(s); renderRefresh(s);
      if (typeof s.uptime_seconds === "number") uptimes.push(s.uptime_seconds);
    }).catch(function () {
      crawlS = null;
      renderDown(["body-crawl", "body-refresh"], "Service de rafraîchissement indisponible.");
    });
    var pEmbed = getJSON(SEM_URL).then(function (s) {
      embedS = s; renderEmbed(s); renderQueries(s);
      if (typeof s.uptime_seconds === "number") uptimes.push(s.uptime_seconds);
    }).catch(function () {
      embedS = null;
      renderDown(["body-embed", "body-catchup", "body-queries"], "Service d'indexation indisponible.");
    });

    Promise.all([pRefresh, pEmbed]).then(function () {
      // The catch-up gauge needs both summaries; render it once both have settled.
      renderCatchup(crawlS, embedS);
      var updated = document.getElementById("status-updated");
      if (!updated) return;
      var t = new Date().toLocaleTimeString("fr-FR");
      var up = uptimes.length ? "  ·  en service depuis " + dur(Math.max.apply(null, uptimes)) : "";
      updated.textContent = "Mis à jour à " + t + up + "  ·  actualisation automatique toutes les " +
        Math.round(REFRESH_MS / 1000) + " s";
    });
  }

  var timer = null;
  function start() { if (!timer) { tick(); timer = setInterval(tick, REFRESH_MS); } }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  // Poll only while the tab is visible (no wasted requests in a background tab).
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") start(); else stop();
  });
  if (document.visibilityState === "visible") start();
})();

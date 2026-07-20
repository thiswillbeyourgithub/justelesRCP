/*
 * tour.js - guided product tour for justelesRCP.
 *
 * A tiny, dependency-free step driver (dimming spotlight + floating card). It runs
 * across TWO pages and hands state over the navigation between them:
 *   HOME phase  (on the landing page, body.home): a welcome popup, then one step that
 *               demonstrates the drug search (auto-typing "olanzapine"), then it
 *               navigates to a concrete drug page.
 *   RCP phase   (on ONE specific quetiapine page): walks the reader through the
 *               freshness/refresh/source/"En savoir plus" card, the Sommaire, the
 *               semantic search (pre-filled with a real query whose top hit the reader
 *               clicks), and finally the "Medicaments lies" block.
 *
 * Triggers:
 *   - auto on the landing page for a first-time visitor (localStorage flag), OR
 *   - ?tour=1 on the landing page (always, even if already seen), OR
 *   - ?tour=rcp directly on the quetiapine page (jump into the RCP phase; also how the
 *     HOME phase resumes after navigating, via a sessionStorage handoff).
 *
 * CSP-safe: same-origin script, no inline handlers, no eval, no external assets. The
 * only cross-file coupling is reading the guarded window.trackEvent (analytics) global.
 * UI strings are French (site convention); code + comments are English.
 */
(function () {
  "use strict";

  var SEEN_KEY = "jlrcp_tour_seen";      // localStorage: suppress the auto-popup once seen
  var RESUME_KEY = "jlrcp_tour";         // sessionStorage: hand the tour across the nav
  var QUET_CIS = "60078765";             // the one drug page the RCP phase runs on
  var QUET_URL =
    "/rcp/60078765-quetiapine-accord-healthcare-lp-400-mg-comprime-a-liberation-prolongee";
  var SEM_QUERY = "Impact des repas sur la biodisponibilité"; // the demo semantic query
  var TARGET_SNIPPET = "repas riche en graisses";             // distinctive text of the demo hit
  var SEM_TIMEOUT_MS = 18000;            // give the embed service this long before offering "skip"

  // ---- small DOM helpers ---------------------------------------------------
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function track(name, data) {
    try { if (typeof window.trackEvent === "function") window.trackEvent(name, data || {}); }
    catch (e) { /* analytics must never break the tour */ }
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ---- overlay + card state ------------------------------------------------
  var spot = null;      // the spotlight (dimmer) element
  var card = null;      // the floating instruction card
  var backdrop = null;  // full-screen dim for centered modals
  var targets = [];     // elements the current step spotlights
  var raf = 0;          // throttles reposition on scroll/resize
  var perStepCleanup = null; // teardown for a step's ad-hoc listeners/observers

  function ensureOverlay() {
    if (spot) return;
    spot = el("div", "tour-spotlight");
    spot.style.display = "none";
    card = el("div", "tour-card");
    document.body.appendChild(spot);
    document.body.appendChild(card);
    window.addEventListener("scroll", scheduleReposition, true);
    window.addEventListener("resize", scheduleReposition);
    document.addEventListener("keydown", onKey, true);
  }

  function onKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); endTour(); }
  }

  function scheduleReposition() {
    if (raf) return;
    raf = window.requestAnimationFrame(function () { raf = 0; reposition(); });
  }

  function unionRect(els) {
    var r = null;
    for (var i = 0; i < els.length; i++) {
      if (!els[i]) continue;
      var b = els[i].getBoundingClientRect();
      if (b.width === 0 && b.height === 0) continue;
      if (!r) r = { top: b.top, left: b.left, right: b.right, bottom: b.bottom };
      else {
        r.top = Math.min(r.top, b.top);
        r.left = Math.min(r.left, b.left);
        r.right = Math.max(r.right, b.right);
        r.bottom = Math.max(r.bottom, b.bottom);
      }
    }
    if (!r) return null;
    return { top: r.top, left: r.left, width: r.right - r.left, height: r.bottom - r.top };
  }

  function reposition() {
    if (!spot) return;
    var rect = targets.length ? unionRect(targets) : null;
    if (rect) {
      var pad = 8;
      spot.style.display = "block";
      spot.style.top = (rect.top - pad) + "px";
      spot.style.left = (rect.left - pad) + "px";
      spot.style.width = (rect.width + pad * 2) + "px";
      spot.style.height = (rect.height + pad * 2) + "px";
      placeCard({ top: rect.top - pad, left: rect.left - pad,
                  width: rect.width + pad * 2, height: rect.height + pad * 2 });
    } else {
      spot.style.display = "none";
      centerCard();
    }
  }

  function placeCard(rect) {
    if (!card) return;
    var gap = 14, cw = card.offsetWidth, ch = card.offsetHeight;
    var vw = window.innerWidth, vh = window.innerHeight, top, left;
    if (rect.top + rect.height + gap + ch <= vh) top = rect.top + rect.height + gap;
    else if (rect.top - gap - ch >= 0) top = rect.top - gap - ch;
    else top = Math.max(gap, (vh - ch) / 2);
    left = rect.left + rect.width / 2 - cw / 2;
    left = Math.max(gap, Math.min(left, vw - cw - gap));
    card.style.top = Math.round(top) + "px";
    card.style.left = Math.round(left) + "px";
  }

  function centerCard() {
    if (!card) return;
    card.style.top = "50%";
    card.style.left = "50%";
    card.style.transform = "translate(-50%, -50%)";
  }

  // Build the card body from a step spec. buttons: [{label, primary, onClick}].
  function renderCard(spec) {
    if (perStepCleanup) { perStepCleanup(); perStepCleanup = null; }
    card.style.transform = "";
    card.innerHTML = "";
    var close = el("button", "tour-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Fermer la visite");
    close.addEventListener("click", endTour);
    card.appendChild(close);

    if (spec.step) {
      card.appendChild(el("p", "tour-step-num", "Étape " + spec.step + " / " + spec.total));
    }
    card.appendChild(el("h3", "tour-title", spec.title));
    card.appendChild(el("p", "tour-body", spec.body));

    var status = null;
    if (spec.status) {
      status = el("p", "tour-status", spec.status);
      card.appendChild(status);
    }

    var actions = el("div", "tour-actions");
    (spec.buttons || []).forEach(function (b) {
      var btn = el("button", "tour-btn" + (b.primary ? " primary" : " ghost"), b.label);
      btn.type = "button";
      btn.addEventListener("click", b.onClick);
      actions.appendChild(btn);
    });
    card.appendChild(actions);
    return { status: status, actions: actions };
  }

  // Show a step: spotlight `els`, scroll the first into view, render the card.
  function showStep(spec) {
    ensureOverlay();
    if (backdrop) { backdrop.remove(); backdrop = null; }
    targets = (spec.targets || []).filter(Boolean);
    var refs = renderCard(spec);
    if (targets.length) {
      try { targets[0].scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
    }
    reposition();
    // Redraw once the smooth-scroll / any <details> opening has settled.
    window.setTimeout(reposition, 380);
    if (spec.onEnter) spec.onEnter(refs);
    return refs;
  }

  // Centered modal (welcome / end): full backdrop, no spotlight.
  function showModal(spec) {
    ensureOverlay();
    spot.style.display = "none";
    targets = [];
    if (!backdrop) { backdrop = el("div", "tour-backdrop"); document.body.appendChild(backdrop); }
    renderCard(spec);
    centerCard();
  }

  function teardownDom() {
    if (raf) { window.cancelAnimationFrame(raf); raf = 0; }
    if (perStepCleanup) { perStepCleanup(); perStepCleanup = null; }
    window.removeEventListener("scroll", scheduleReposition, true);
    window.removeEventListener("resize", scheduleReposition);
    document.removeEventListener("keydown", onKey, true);
    if (spot) spot.remove();
    if (card) card.remove();
    if (backdrop) backdrop.remove();
    spot = card = backdrop = null;
    targets = [];
  }

  function markSeen() { try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {} }

  function endTour() {
    markSeen();
    try { sessionStorage.removeItem(RESUME_KEY); } catch (e) {}
    teardownDom();
    track("tour-fin", {});
  }

  // ==== HOME phase ==========================================================
  function startHome() {
    markSeen();
    track("tour-debut", {});
    showModal({
      title: "Bienvenue sur justelesRCP",
      body: "justelesRCP affiche les résumés des caractéristiques du produit (RCP) des " +
            "médicaments vendus en France : rapide, sans publicité, sans compte. " +
            "Voulez-vous une courte visite guidée ?",
      buttons: [
        { label: "Passer", onClick: endTour },
        { label: "Commencer la visite", primary: true, onClick: homeSearchStep },
      ],
    });
  }

  function homeSearchStep() {
    // Demonstrate the drug search live: type "olanzapine" so the results dropdown opens.
    var input = qs("#q");
    var box = qs(".searchbox") || input;
    if (input) {
      input.value = "olanzapine";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    showStep({
      step: 1, total: 5,
      title: "Rechercher un médicament",
      body: "Tapez le nom d'un médicament ici (par exemple « olanzapine ») : les " +
            "résultats apparaissent au fur et à mesure. Cliquez un résultat pour ouvrir sa " +
            "fiche. Continuons sur un exemple concret.",
      targets: [box, qs("#results")],
      buttons: [
        { label: "Passer", onClick: endTour },
        { label: "Suivant", primary: true, onClick: goToDrugPage },
      ],
    });
  }

  function goToDrugPage() {
    try { sessionStorage.setItem(RESUME_KEY, "rcp"); } catch (e) {}
    window.location.href = QUET_URL;
  }

  // ==== RCP phase ===========================================================
  function startRcp() {
    try { sessionStorage.removeItem(RESUME_KEY); } catch (e) {}
    track("tour-debut", { phase: "rcp" });
    stepAsof();
  }

  function openDetails(sel) {
    var d = qs(sel);
    if (d && d.tagName === "DETAILS") d.open = true;
    return d;
  }

  function closeDetails(sel) {
    var d = qs(sel);
    if (d && d.tagName === "DETAILS") d.open = false;
    return d;
  }

  function stepAsof() {
    showStep({
      step: 2, total: 5,
      title: "Fraîcheur, source et liens utiles",
      body: "En haut de chaque fiche : la date de mise à jour officielle (ANSM) et notre " +
            "date de vérification, le bouton « Rafraîchir maintenant », le lien vers " +
            "la source officielle, et les liens « En savoir plus » (BDPM, HAS, EMA, " +
            "CRAT, Vidal).",
      targets: [qs(".rcp-asof"), qs(".rcp-more")],
      buttons: [
        { label: "Passer", onClick: endTour },
        { label: "Suivant", primary: true, onClick: stepToc },
      ],
    });
  }

  function stepToc() {
    var toc = openDetails(".toc");
    showStep({
      step: 3, total: 5,
      title: "Le sommaire",
      body: "Le sommaire permet de sauter directement à n'importe quelle section du RCP.",
      targets: [toc],
      buttons: [
        { label: "Passer", onClick: endTour },
        { label: "Suivant", primary: true, onClick: stepSem },
      ],
    });
  }

  function stepSem() {
    var box = qs(".semsearch");
    if (!box) { stepXref(); return; } // embed feature absent: skip gracefully
    // The ToC and the search box are BOTH position:sticky at the same top offset, so an
    // open ToC (from step 3) collides with / hides the search box. Collapse it first.
    closeDetails(".toc");
    box.open = true; // fires the box's toggle -> ensureReady() warm-up
    var input = qs(".semsearch-input", box);
    var results = qs(".semsearch-results", box);

    var refs = showStep({
      step: 4, total: 5,
      title: "Recherche sémantique",
      body: "Posez une question en langage naturel, pas seulement des mots-clés. Ici : " +
            "« Impact des repas sur la biodisponibilité ». Cliquez le résultat mis " +
            "en évidence pour accéder au passage correspondant.",
      status: "Recherche en cours…",
      targets: [box],
      buttons: [{ label: "Passer", onClick: endTour }],
    });

    // Type the query and keep nudging the search until a hit renders (the embed service
    // may need a moment to warm up and embed the page).
    var advanced = false;
    function nudge() {
      if (input && input.value !== SEM_QUERY) input.value = SEM_QUERY;
      if (input) input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    nudge();
    var nudgeTimer = window.setInterval(nudge, 1600);

    function findHit() {
      if (!results) return null;
      var links = results.querySelectorAll(".semsearch-hit-link");
      for (var i = 0; i < links.length; i++) {
        if (links[i].textContent &&
            links[i].textContent.toLowerCase().indexOf(TARGET_SNIPPET) !== -1) return links[i];
      }
      return links.length ? links[0] : null; // fall back to the first hit
    }

    function onResultClick(ev) {
      if (advanced) return;
      var link = ev.target && ev.target.closest ? ev.target.closest(".semsearch-hit-link") : null;
      if (!link) return;
      advanced = true;
      cleanup();
      track("tour-clic-resultat", {});
      // The box's own handler collapses the box and scrolls to the passage; give the
      // reader a beat to register that jump, then advance to the final step.
      window.setTimeout(stepXref, 1100);
    }

    var obs = null;
    var spotted = false; // scroll the hit into view only once (avoid jitter on re-render)
    function highlight() {
      var hit = findHit();
      if (!hit) return false;
      // Results can re-render (the query embed completes and re-ranks); clear any stale
      // pulse and re-mark the current hit so the highlight survives a rebuild.
      var stale = results.querySelectorAll(".tour-pulse");
      for (var j = 0; j < stale.length; j++) if (stale[j] !== hit) stale[j].classList.remove("tour-pulse");
      hit.classList.add("tour-pulse");
      // Spotlight the SPECIFIC result (a small rect), not the whole box: this guarantees
      // the instruction card is placed above/below it and never covers the click target.
      targets = [hit];
      window.clearInterval(nudgeTimer);
      if (refs.status) refs.status.textContent = "Cliquez le résultat mis en évidence ↓";
      if (!spotted) {
        spotted = true;
        try { hit.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
      }
      reposition();
      return true;
    }
    if (results) {
      results.addEventListener("click", onResultClick, true);
      // Keep observing (do NOT disconnect on first hit): a later re-rank re-applies the pulse.
      obs = new MutationObserver(function () { highlight(); });
      obs.observe(results, { childList: true, subtree: true });
      highlight(); // in case results are already present
    }

    // Fallback: if nothing showed up in time, let the reader skip this step.
    var skipTimer = window.setTimeout(function () {
      if (advanced) return;
      if (refs.status) {
        refs.status.textContent =
          "La recherche sémantique nécessite le service dédié. Vous pouvez passer cette étape.";
      }
      var skip = el("button", "tour-btn primary", "Passer cette étape");
      skip.type = "button";
      skip.addEventListener("click", function () { advanced = true; cleanup(); stepXref(); });
      if (refs.actions) refs.actions.appendChild(skip);
    }, SEM_TIMEOUT_MS);

    function cleanup() {
      window.clearInterval(nudgeTimer);
      window.clearTimeout(skipTimer);
      if (obs) obs.disconnect();
      if (results) results.removeEventListener("click", onResultClick, true);
    }
    perStepCleanup = cleanup;
  }

  function stepXref() {
    closeDetails(".semsearch"); // collapse the search box (the "skip" path leaves it open)
    var xref = openDetails(".drug-xref-list");
    showStep({
      step: 5, total: 5,
      title: "Médicaments liés",
      body: xref
        ? "En bas de page, « Médicaments liés » regroupe les autres médicaments " +
          "cités dans ce texte, reliés automatiquement pour naviguer d'une fiche à l'autre."
        : "En bas des fiches concernées, « Médicaments liés » relie automatiquement " +
          "les autres médicaments cités dans le texte. (Absent ici : aucun n'est cité.)",
      targets: [xref],
      buttons: [{ label: "Terminer", primary: true, onClick: finish }],
    });
  }

  function finish() {
    showModal({
      title: "Visite terminée",
      body: "Voilà l'essentiel ! Bonne consultation. Vous pourrez relancer cette visite à " +
            "tout moment depuis le lien « Visite guidée » en bas de page.",
      buttons: [{ label: "Fermer", primary: true, onClick: endTour }],
    });
    track("tour-termine", {});
  }

  // ==== entry point =========================================================
  function boot() {
    var params;
    try { params = new URLSearchParams(window.location.search); } catch (e) { params = null; }
    var tourParam = params ? params.get("tour") : null;
    var main = qs("main.rcp[data-cis]");

    if (main && main.dataset.cis === QUET_CIS) {
      var resume = false;
      try { resume = sessionStorage.getItem(RESUME_KEY) === "rcp"; } catch (e) {}
      if (resume || tourParam === "rcp") startRcp();
      return;
    }

    if (document.body && document.body.classList.contains("home")) {
      var seen = null;
      try { seen = localStorage.getItem(SEEN_KEY); } catch (e) {}
      if (tourParam === "1" || !seen) startHome();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

/*
 * tour.js - guided product tour for justelesRCP.
 *
 * A tiny, dependency-free step driver (dimming spotlight + floating card, both smoothly
 * animated). It runs across TWO pages and hands state over the navigation between them:
 *   HOME phase  (on the landing page, body.home): a welcome popup, then TWO steps that
 *               demonstrate the drug search: (1) the search field, then (2) the name
 *               "quétiapine" typed out live, a specific result highlighted, and every
 *               click OUTSIDE the search box forbidden so the reader stays on rails. It
 *               then navigates to a concrete drug page.
 *   RCP phase   (on ONE specific quetiapine page): walks the reader through the
 *               freshness/refresh/source/"En savoir plus" card, the Sommaire, then the
 *               semantic search broken into its own steps (open it, watch the question
 *               type itself, click the highlighted result which scrolls to the passage,
 *               confirm), and finally the "Medicaments lies" block.
 *
 * Every step card carries a "Precedent" (back) button and a top-right close cross; there
 * is no separate "skip" button (the cross ends the tour). Back navigation just re-invokes
 * the previous step's function, which fully re-establishes its DOM state, so forward and
 * back share one code path. Crossing the page boundary backwards (first RCP step ->
 * home) is handled via the same sessionStorage handoff used going forward.
 *
 * Triggers:
 *   - auto on the landing page for a first-time visitor (localStorage flag), OR
 *   - ?tour=1 on the landing page (always, even if already seen), OR
 *   - ?tour=rcp directly on the quetiapine page (jump into the RCP phase; also how the
 *     HOME phase resumes after navigating, via a sessionStorage handoff).
 *
 * CSP-safe: same-origin script, no inline handlers, no eval, no external assets, and all
 * styling via classes / CSSOM property setters (no style="" attribute). The only
 * cross-file coupling is reading the guarded window.trackEvent (analytics) global. UI
 * strings are French (site convention); code + comments are English.
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
  var RCP_ARRIVAL_MS = 1400;             // pause on landing so the reader registers the new page
  var TOTAL = 9;                         // total numbered steps (2 home + 7 rcp)

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
  function fire(node, type) {
    if (node) node.dispatchEvent(new Event(type, { bubbles: true }));
  }

  // ---- overlay + card state ------------------------------------------------
  var spot = null;      // the spotlight (dimmer) element
  var card = null;      // the floating instruction card
  var backdrop = null;  // full-screen dim for centered modals
  var targets = [];     // elements the current step spotlights
  var raf = 0;          // throttles reposition on scroll/resize
  var perStepCleanup = null; // teardown for a step's ad-hoc listeners/observers/timers
  var revealPending = false; // while true, keep the spotlight faded out (mid step-scroll)
  var scrollGen = 0;         // invalidates a stale scroll-settle callback when the step changes

  function ensureOverlay() {
    if (spot) return;
    spot = el("div", "tour-spotlight");
    // Hidden via opacity (a class), NOT display:none, so the spotlight fades + glides
    // smoothly when it (re)appears instead of popping in. See .tour-spotlight in style.css.
    spot.classList.add("tour-hidden");
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

  // Run cb once the smooth-scroll started by showStep has settled: poll the scroll offset
  // per frame and fire when it stops changing for a few frames (or a hard cap, so it always
  // fires even if no scroll was needed or the browser janks). Used to hold the spotlight
  // hidden until the page is stationary, then fade it in.
  function afterScrollSettles(cb) {
    var lastY = window.pageYOffset || 0, stable = 0, frames = 0, moved = false;
    function tick() {
      if (!spot) return; // torn down
      var y = window.pageYOffset || 0;
      if (Math.abs(y - lastY) <= 1) { stable++; } else { moved = true; stable = 0; }
      lastY = y;
      frames++;
      // Settle once movement has stopped, or after a short grace when nothing moved at all
      // (target already in view), or a hard ~1s cap.
      if ((moved && stable >= 3) || (!moved && frames >= 4) || frames >= 60) { cb(); return; }
      window.requestAnimationFrame(tick);
    }
    window.requestAnimationFrame(tick);
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
      // Position the spotlight even while it is still hidden (revealPending), so that once
      // the scroll settles it just fades in at its final resting place instead of gliding
      // across the page. Lift the fade-out only when the step is no longer mid-scroll.
      spot.style.top = (rect.top - pad) + "px";
      spot.style.left = (rect.left - pad) + "px";
      spot.style.width = (rect.width + pad * 2) + "px";
      spot.style.height = (rect.height + pad * 2) + "px";
      if (!revealPending) spot.classList.remove("tour-hidden");
      placeCard({ top: rect.top - pad, left: rect.left - pad,
                  width: rect.width + pad * 2, height: rect.height + pad * 2 });
    } else {
      spot.classList.add("tour-hidden");
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

  // Centered modal placement. Positioned in pixels (NOT a translate transform) so the
  // per-step enter animation is free to use `transform` without fighting the centering.
  function centerCard() {
    if (!card) return;
    var cw = card.offsetWidth, ch = card.offsetHeight;
    card.style.top = Math.max(8, (window.innerHeight - ch) / 2) + "px";
    card.style.left = Math.max(8, (window.innerWidth - cw) / 2) + "px";
  }

  // Replace the single per-step cleanup with a fresh collector; returns an `add(fn)` used
  // to register each teardown (typing timers, observers, click guards). The whole batch
  // runs at the next renderCard() (i.e. when the step changes) or at teardown.
  function setCleanup() {
    var fns = [];
    perStepCleanup = function () {
      fns.forEach(function (f) { try { f(); } catch (e) {} });
    };
    return function add(fn) { fns.push(fn); };
  }

  // Build the card body from a step spec. Adds a "Precedent" ghost button first when
  // spec.back is given, then the spec.buttons (forward actions). Every card also gets a
  // close cross. buttons: [{label, primary, onClick}].
  function renderCard(spec) {
    if (perStepCleanup) { perStepCleanup(); perStepCleanup = null; }
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
    if (spec.back) {
      var back = el("button", "tour-btn ghost", "‹ Précédent");
      back.type = "button";
      back.addEventListener("click", spec.back);
      actions.appendChild(back);
    }
    (spec.buttons || []).forEach(function (b) {
      var btn = el("button", "tour-btn" + (b.primary ? " primary" : " ghost"), b.label);
      btn.type = "button";
      btn.addEventListener("click", b.onClick);
      actions.appendChild(btn);
    });
    card.appendChild(actions);

    // Re-trigger the enter animation on every render (fade + slight rise), so each step
    // change is smoothly animated. Force a reflow between remove and add.
    card.classList.remove("tour-anim");
    void card.offsetWidth;
    card.classList.add("tour-anim");
    return { status: status, actions: actions };
  }

  // Show a step: spotlight `els`, scroll the first into view, render the card.
  function showStep(spec) {
    ensureOverlay();
    if (backdrop) { backdrop.remove(); backdrop = null; }
    var gen = ++scrollGen; // any pending scroll-settle callback from the previous step is now stale
    targets = (spec.targets || []).filter(Boolean);
    var refs = renderCard(spec);
    if (targets.length) {
      // Keep the spotlight faded out while we scroll the target into view, and reveal it
      // only once the scroll has settled: the rect fading in on a stationary page reads as
      // smoother + more intentional than chasing the target across a moving one. The card is
      // placed right away (reposition below) so it is readable during the scroll.
      revealPending = true;
      spot.classList.add("tour-hidden");
      reposition();
      try { targets[0].scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
      afterScrollSettles(function () {
        if (gen !== scrollGen) return; // superseded by a newer step
        revealPending = false;
        reposition();
      });
    } else {
      revealPending = false;
      reposition();
    }
    if (spec.onEnter) spec.onEnter(refs);
    return refs;
  }

  // Centered modal (welcome / end): full backdrop, no spotlight.
  function showModal(spec) {
    ensureOverlay();
    ++scrollGen;               // invalidate any pending scroll-settle reveal from a prior step
    revealPending = false;
    spot.classList.add("tour-hidden");
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

  // Type `text` into an input one character at a time, dispatching an "input" event per
  // keystroke (so the page's own search wiring reacts live). Returns a cancel function.
  function typeInto(input, text, delay, done) {
    if (!input) { if (done) done(); return function () {}; }
    var i = 0;
    input.value = "";
    fire(input, "input");
    var timer = window.setInterval(function () {
      i++;
      input.value = text.slice(0, i);
      fire(input, "input");
      if (i >= text.length) {
        window.clearInterval(timer);
        timer = 0;
        if (done) done();
      }
    }, delay || 90);
    return function cancel() { if (timer) window.clearInterval(timer); };
  }

  // ==== HOME phase ==========================================================
  function startHome() {
    markSeen();
    track("tour-debut", {});
    welcomeModal();
  }

  function welcomeModal() {
    showModal({
      title: "Bienvenue sur justelesRCP",
      body: "justelesRCP affiche les résumés des caractéristiques du produit (RCP) des " +
            "médicaments vendus en France : rapide, sans publicité, sans compte. " +
            "Voulez-vous une courte visite guidée ?",
      buttons: [
        { label: "Non merci", onClick: endTour },
        { label: "Commencer la visite", primary: true, onClick: homeSearchBox },
      ],
    });
  }

  // Step 1: the empty search field.
  function homeSearchBox() {
    var input = qs("#q");
    var box = qs(".searchbox") || input;
    if (input) {
      input.value = "";
      fire(input, "input");
      // Do NOT focus the field: on mobile that pops up the on-screen keyboard, which covers
      // the tour. The demo types into it programmatically (no focus needed); blur it in case
      // the landing page's autofocus already grabbed it.
      try { input.blur(); } catch (e) {}
    }
    showStep({
      step: 1, total: TOTAL,
      title: "Le champ de recherche",
      body: "Tout commence ici : saisissez le nom d'un médicament pour ouvrir sa fiche. " +
            "Regardons ce que ça donne.",
      targets: [box],
      back: welcomeModal,
      buttons: [{ label: "Suivant", primary: true, onClick: homeSearch }],
    });
    // Step 1 is "look at the field", not "use it": forbid interacting with anything
    // (including the field itself) so a stray tap can't focus it (popping the keyboard)
    // or click a link and derail the tour. Step 2 is where the typing demo happens.
    var add = setCleanup();
    function block(ev) {
      if (card && card.contains(ev.target)) return; // the card's own buttons keep working
      ev.preventDefault();
      ev.stopPropagation();
    }
    document.addEventListener("click", block, true);
    add(function () { document.removeEventListener("click", block, true); });
    if (box) {
      // Focus fires on pointerdown (before click), so preventDefault THERE stops the field
      // from being focused/selected. Scoped to the box, so the rest of the page still scrolls.
      function noFocus(ev) { ev.preventDefault(); }
      box.addEventListener("mousedown", noFocus, true);
      box.addEventListener("touchstart", noFocus, { capture: true, passive: false });
      add(function () {
        box.removeEventListener("mousedown", noFocus, true);
        box.removeEventListener("touchstart", noFocus, true);
      });
    }
    if (input) {
      // Belt and suspenders: if anything still focuses the field (tab key, autofocus race),
      // blur it back out so the keyboard never comes up during this step.
      function reblur() { try { input.blur(); } catch (e) {} }
      input.addEventListener("focus", reblur);
      add(function () { input.removeEventListener("focus", reblur); });
    }
  }

  // Step 2: type "quétiapine" live, highlight a result, and forbid clicking anywhere
  // except the search box / a result / the tour card.
  function homeSearch() {
    var input = qs("#q");
    var box = qs(".searchbox") || input;
    var results = qs("#results");
    showStep({
      step: 2, total: TOTAL,
      title: "Rechercher un médicament",
      body: "Tapez le nom d'un médicament (ici « quétiapine ») : les résultats " +
            "apparaissent au fur et à mesure. Cliquez le résultat mis en évidence " +
            "pour ouvrir une fiche. Nous continuons sur un exemple concret.",
      targets: [box, results],
      back: homeSearchBox,
      buttons: [{ label: "Continuer sur la quétiapine", primary: true, onClick: goToDrugPage }],
    });
    var add = setCleanup();

    function pulseFirst() {
      if (!results) return;
      var links = results.querySelectorAll("a");
      if (!links.length) return;
      for (var i = 0; i < links.length; i++) links[i].classList.toggle("tour-pulse", i === 0);
      targets = [box, links[0]];
      reposition();
    }
    // Type the name gradually; results render progressively as each input event fires.
    add(typeInto(input, "quétiapine", 95, pulseFirst));
    // Keep the first result pulsing even as the list re-renders while typing / after the
    // index finishes loading.
    if (results) {
      var obs = new MutationObserver(pulseFirst);
      obs.observe(results, { childList: true });
      add(function () { obs.disconnect(); });
      pulseFirst();
    }
    // Forbid clicking elsewhere. A click on a result continues the tour on the concrete
    // example page (the fixed quétiapine example, NOT whichever result was clicked, which
    // could drop off the tour rails).
    function guard(ev) {
      var t = ev.target;
      if (card && card.contains(t)) return;                 // card buttons work normally
      var link = t && t.closest ? t.closest("#results a") : null;
      if (link) { ev.preventDefault(); ev.stopPropagation(); goToDrugPage(); return; }
      if (box && box.contains(t)) return;                   // interacting with the box is fine
      ev.preventDefault(); ev.stopPropagation();            // block everything else
    }
    document.addEventListener("click", guard, true);
    add(function () { document.removeEventListener("click", guard, true); });
  }

  function goToDrugPage() {
    try { sessionStorage.setItem(RESUME_KEY, "rcp"); } catch (e) {}
    window.location.href = QUET_URL;
  }

  // Back from the first RCP step: hop to the landing page and resume the search demo.
  function backToHomeSearch() {
    try { sessionStorage.setItem(RESUME_KEY, "home"); } catch (e) {}
    window.location.href = "/";
  }

  // ==== RCP phase ===========================================================
  function startRcp() {
    try { sessionStorage.removeItem(RESUME_KEY); } catch (e) {}
    track("tour-debut", { phase: "rcp" });
    // Hold off on the first card: the reader just navigated here from the search, so
    // give them a beat to see they are now on a concrete drug page before the tour
    // dims it. ("Precedent" back into step 3 calls stepAsof() directly, with no delay.)
    window.setTimeout(stepAsof, RCP_ARRIVAL_MS);
  }

  // Step 3: freshness / source / "En savoir plus".
  function stepAsof() {
    showStep({
      step: 3, total: TOTAL,
      title: "Fraîcheur, source et liens utiles",
      body: "En haut de chaque fiche : la date de mise à jour officielle (ANSM) et notre " +
            "date de vérification, le bouton « Rafraîchir maintenant », le lien vers " +
            "la source officielle, et les liens « En savoir plus » (BDPM, HAS, EMA, " +
            "CRAT, Vidal).",
      targets: [qs(".rcp-asof"), qs(".rcp-more")],
      back: backToHomeSearch,
      buttons: [{ label: "Suivant", primary: true, onClick: stepToc }],
    });
  }

  // Step 4: the table of contents.
  function stepToc() {
    var toc = openDetails(".toc");
    showStep({
      step: 4, total: TOTAL,
      title: "Le sommaire",
      body: "Le sommaire permet de sauter directement à n'importe quelle section du RCP.",
      targets: [toc],
      back: stepAsof,
      buttons: [{ label: "Suivant", primary: true, onClick: stepSemOpen }],
    });
  }

  // The ToC and the search box are BOTH position:sticky at the same top offset, so an
  // open ToC collides with / hides the search box. Every semantic step closes the ToC and
  // opens the search box, so it works whether reached forward or via "Precedent".
  function openSemBox() {
    closeDetails(".toc");
    var box = qs(".semsearch");
    if (box) box.open = true; // fires the box's toggle -> ensureReady() warm-up
    return box;
  }

  // Step 5: reveal the semantic search box (starts the embed warm-up).
  function stepSemOpen() {
    var box = openSemBox();
    if (!box) { stepXref(); return; } // embed feature absent: skip gracefully
    showStep({
      step: 5, total: TOTAL,
      title: "Recherche sémantique",
      body: "Cette fiche se laisse interroger en langage naturel : posez une question et " +
            "les passages les plus proches par le sens sont classés. Ouvrons-la.",
      targets: [box],
      back: stepToc,
      buttons: [{ label: "Suivant", primary: true, onClick: stepSemType }],
    });
  }

  // Step 6: type the example question, letter by letter.
  function stepSemType() {
    var box = openSemBox();
    if (!box) { stepXref(); return; }
    var input = qs(".semsearch-input", box);
    showStep({
      step: 6, total: TOTAL,
      title: "Posez votre question",
      body: "Exemple : « " + SEM_QUERY + " ». Regardez la question s'écrire, puis nous " +
            "repérerons le passage le plus pertinent.",
      targets: [box],
      back: stepSemOpen,
      buttons: [{ label: "Suivant", primary: true, onClick: stepSemPick }],
    });
    var add = setCleanup();
    add(typeInto(input, SEM_QUERY, 55, null));
  }

  // Step 7: highlight the relevant result; a click scrolls to the passage, then advances.
  function stepSemPick() {
    var box = openSemBox();
    if (!box) { stepXref(); return; }
    var input = qs(".semsearch-input", box);
    var results = qs(".semsearch-results", box);
    var refs = showStep({
      step: 7, total: TOTAL,
      title: "Le passage le plus pertinent",
      body: "Nous avons repéré le résultat le plus utile pour cette question. Cliquez le " +
            "résultat mis en évidence pour aller droit au passage dans le texte.",
      status: "Recherche en cours…",
      targets: [box],
      back: stepSemType,
      buttons: [],
    });
    var add = setCleanup();
    var advanced = false;

    // Keep nudging the full query in until a hit renders (the embed service may still be
    // warming up / the reader may have clicked "Suivant" mid-typing).
    function nudge() {
      if (input && input.value !== SEM_QUERY) input.value = SEM_QUERY;
      fire(input, "input");
    }
    nudge();
    var nudgeTimer = window.setInterval(nudge, 1600);
    add(function () { window.clearInterval(nudgeTimer); });

    function findHit() {
      if (!results) return null;
      var links = results.querySelectorAll(".semsearch-hit-link");
      for (var i = 0; i < links.length; i++) {
        if (links[i].textContent &&
            links[i].textContent.toLowerCase().indexOf(TARGET_SNIPPET) !== -1) return links[i];
      }
      return links.length ? links[0] : null; // fall back to the first hit
    }

    var spotted = false; // scroll the hit into view only once (avoid jitter on re-render)
    function highlight() {
      if (advanced) return false;
      var hit = findHit();
      if (!hit) return false;
      var stale = results.querySelectorAll(".tour-pulse");
      for (var j = 0; j < stale.length; j++) if (stale[j] !== hit) stale[j].classList.remove("tour-pulse");
      hit.classList.add("tour-pulse");
      // Spotlight the SPECIFIC result (a small rect), not the whole box, so the card sits
      // above/below it and never covers the click target.
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

    function onResultClick(ev) {
      if (advanced) return;
      var link = ev.target && ev.target.closest ? ev.target.closest(".semsearch-hit-link") : null;
      if (!link) return;
      advanced = true;
      window.clearInterval(nudgeTimer);
      track("tour-clic-resultat", {});
      // The box's own handler collapses the box and scrolls to the passage; give the
      // reader a beat to register that jump, then advance to the confirmation step.
      window.setTimeout(stepPassage, 1000);
    }

    if (results) {
      results.addEventListener("click", onResultClick, true);
      add(function () { results.removeEventListener("click", onResultClick, true); });
      // Keep observing (do NOT disconnect on first hit): a later re-rank re-applies the pulse.
      var obs = new MutationObserver(highlight);
      obs.observe(results, { childList: true, subtree: true });
      add(function () { obs.disconnect(); });
      highlight(); // in case results are already present
    }

    // Fallback: if nothing showed up in time, let the reader skip straight past the
    // semantic demo (there is no passage to confirm, so go to the last step).
    var skipTimer = window.setTimeout(function () {
      if (advanced) return;
      if (refs.status) {
        refs.status.textContent =
          "La recherche sémantique nécessite le service dédié. Vous pouvez passer cette étape.";
      }
      var skip = el("button", "tour-btn primary", "Passer cette étape");
      skip.type = "button";
      skip.addEventListener("click", function () { advanced = true; stepXref(); });
      if (refs.actions) refs.actions.appendChild(skip);
    }, SEM_TIMEOUT_MS);
    add(function () { window.clearTimeout(skipTimer); });
  }

  // Step 8: confirm the reader landed on the highlighted passage (after the click).
  function stepPassage() {
    closeDetails(".semsearch"); // ensure the box is collapsed (the click already did it)
    var hit = qs(".semsearch-current") || qs(".semsearch-hit");
    if (!hit) { stepXref(); return; } // nothing was clicked/served: move on
    try { hit.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
    showStep({
      step: 8, total: TOTAL,
      title: "Vous y êtes",
      body: "Le passage le plus pertinent est mis en évidence dans le texte de la fiche. " +
            "Vous pouvez encore naviguer entre les passages avec ‹ › dans la recherche. " +
            "Terminons la visite.",
      targets: [hit],
      back: stepSemPick,
      buttons: [{ label: "Suivant", primary: true, onClick: stepXref }],
    });
  }

  // Back target for the last step: return to wherever the semantic demo left off.
  function xrefBack() {
    if (qs(".semsearch-hit")) { stepPassage(); return; }
    if (qs(".semsearch")) { stepSemOpen(); return; }
    stepToc();
  }

  // Step 9: cross-drug backlinks.
  function stepXref() {
    closeDetails(".semsearch"); // collapse the search box (the "skip" path leaves it open)
    var xref = openDetails(".drug-xref-list");
    showStep({
      step: 9, total: TOTAL,
      title: "Médicaments liés",
      body: xref
        ? "En bas de page, « Médicaments liés » regroupe les autres médicaments " +
          "cités dans ce texte, reliés automatiquement pour naviguer d'une fiche à l'autre."
        : "En bas des fiches concernées, « Médicaments liés » relie automatiquement " +
          "les autres médicaments cités dans le texte. (Absent ici : aucun n'est cité.)",
      targets: [xref],
      back: xrefBack,
      buttons: [{ label: "Terminer", primary: true, onClick: finish }],
    });
  }

  function finish() {
    showModal({
      title: "Visite terminée",
      body: "Voilà l'essentiel ! Bonne visite. Vous pourrez relancer cette visite à " +
            "tout moment depuis le lien « Visite guidée » en bas de page.",
      back: stepXref,
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
      var back = false;
      try { back = sessionStorage.getItem(RESUME_KEY) === "home"; } catch (e) {}
      var seen = null;
      try { seen = localStorage.getItem(SEEN_KEY); } catch (e) {}
      // The landing page autofocuses #q, which pops the on-screen keyboard on mobile.
      // When a tour is about to run, blur the field right away so the keyboard does not
      // cover the tour (the demo types into it programmatically, no focus needed). This
      // catches the "?tour=1" navigation, which reloads the page and re-fires autofocus.
      if (back || tourParam === "1" || !seen) {
        var q = qs("#q");
        if (q) { try { q.blur(); } catch (e) {} }
      }
      if (back) {
        try { sessionStorage.removeItem(RESUME_KEY); } catch (e) {}
        homeSearch(); // resumed by "Precedent" from the first RCP step
        return;
      }
      if (tourParam === "1" || !seen) startHome();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

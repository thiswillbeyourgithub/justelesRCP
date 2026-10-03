/*
 * changelog.js - the "Quoi de neuf ?" release-notes popup.
 *
 * The notes are authored per release in docs/changelog/<version>/changelog.md and
 * compiled by build.py into /changelog.json; this file only decides WHEN to show them
 * and renders them.
 *
 * Auto-open rule: the browser remembers the last version whose notes it showed
 * (localStorage jlrcp_changelog_seen). On a later visit, if the site's version
 * (window.__APP_VERSION__, from app-version.js) is newer, the popup opens with EVERY
 * release since the stored one, then stores the new version. A FIRST-time visitor sees
 * nothing (there is no "since" to show): the current version is stored silently, and
 * the tour is what greets them. It is also suppressed while a tour runs.
 *
 * Manual open: any [data-changelog] element (the /a-propos link, the home footer)
 * opens it showing ALL releases. The popup itself has a "Tout afficher" button that
 * expands a "since" view into the full history.
 *
 * CSP-safe: same-origin script, no inline handlers, no eval, no innerHTML (text is set
 * with textContent), styling via classes only. UI strings are French (site convention),
 * code + comments English. Loaded on every page, but it fetches changelog.json ONLY
 * when it actually has something to show.
 */
(function () {
  "use strict";

  var SEEN_KEY = "jlrcp_changelog_seen"; // last version whose notes were shown here
  var data = null; // the parsed changelog.json, fetched at most once
  var overlay = null; // the open popup, if any

  // Storage degrades to "never auto-open" in private mode (jlrcp.lsGet -> null).
  var J = window.jlrcp, el = J.el, track = J.track, lsGet = J.lsGet, lsSet = J.lsSet;

  // "0.9.0" < "0.10.0": compare numerically, component by component.
  function cmp(a, b) {
    var x = String(a).split("."), y = String(b).split("."), i;
    for (i = 0; i < 3; i++) {
      var d = (parseInt(x[i], 10) || 0) - (parseInt(y[i], 10) || 0);
      if (d) return d < 0 ? -1 : 1;
    }
    return 0;
  }

  // ---- rendering -----------------------------------------------------------
  function renderRelease(rel) {
    var box = el("section", "changelog-release");
    // .changelog-version is a flex row, so the bare version text is already an
    // item next to the date; it needs no wrapper span of its own.
    var h = el("h3", "changelog-version", "Version " + rel.version);
    h.appendChild(el("span", "changelog-date", J.frDate(rel.date) || rel.date));
    box.appendChild(h);
    (rel.sections || []).forEach(function (sec) {
      // The French category labels ride in the JSON (build.py CHANGELOG_CATEGORIES),
      // so this file keeps no copy of that table.
      box.appendChild(el("h4", "changelog-cat", (data.categories || {})[sec.key] || sec.key));
      var ul = el("ul", "changelog-items");
      (sec.items || []).forEach(function (it) {
        var li = el("li", null, it.fr);
        (it.commits || []).forEach(function (sha) {
          var a = el("a", "changelog-sha", sha.slice(0, 7));
          a.href = (data.commit_url || "") + sha;
          a.target = "_blank";
          a.rel = "noopener";
          a.title = "Voir le code de ce changement sur GitHub";
          li.appendChild(document.createTextNode(" "));
          li.appendChild(a);
        });
        ul.appendChild(li);
      });
      box.appendChild(ul);
    });
    return box;
  }

  // `since` = show only releases newer than that version (null = show everything).
  // Built into a fragment and inserted once, so the "Tout afficher" expansion (which
  // refills an already-displayed, scrolling body) costs one insertion, not one per release.
  function fill(body, since) {
    var frag = document.createDocumentFragment();
    (data.releases || []).forEach(function (rel) {
      if (!since || cmp(rel.version, since) > 0) frag.appendChild(renderRelease(rel));
    });
    body.textContent = "";
    body.appendChild(frag);
  }

  var prevFocus = null; // element focused before the overlay opened, restored on close

  function close() {
    if (!overlay) return;
    document.removeEventListener("keydown", onKey, true);
    document.body.classList.remove("changelog-open");
    overlay.remove();
    overlay = null;
    if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) {} }
    prevFocus = null;
  }

  function onKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
  }

  // The bare full-screen LAYER every popup shares: one overlay at a time, a click on
  // the overlay itself (not on its content) closes it, Escape closes it, the page is
  // frozen behind it and focus returns to where it was on close. The caller fills
  // the returned node. lightbox.js uses it directly; frame() below builds on it.
  function layer(cls) {
    close();
    prevFocus = document.activeElement;
    overlay = el("div", cls);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) close(); // click outside the content dismisses it
    });
    document.body.appendChild(overlay);
    document.body.classList.add("changelog-open"); // freeze the page behind the overlay
    document.addEventListener("keydown", onKey, true);
    return overlay;
  }

  // The popup FRAME, independent of what it shows: a layer() holding a titled card
  // with a close cross, a scrolling body and a footer with a "Fermer" button. Exposed
  // as window.jlrcpModal so another page script can open its own popup in the same
  // frame (status.js's backlog table) instead of keeping a second copy of this logic.
  // opts: {title, label, sub, cls}; returns {card, body, foot, ok, close}, the caller
  // fills body (and foot).
  function frame(opts) {
    var host = layer("changelog-overlay");
    var card = el("div", "changelog-modal" + (opts.cls ? " " + opts.cls : ""));
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-label", opts.label || opts.title);

    var head = el("div", "changelog-head");
    head.appendChild(el("h2", "changelog-title", opts.title));
    var x = el("button", "changelog-close", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Fermer");
    x.addEventListener("click", close);
    head.appendChild(x);
    card.appendChild(head);
    if (opts.sub) card.appendChild(el("p", "changelog-sub", opts.sub));

    var body = el("div", "changelog-body"); // the scrollable part
    card.appendChild(body);

    var foot = el("div", "changelog-foot");
    var ok = el("button", "changelog-ok", "Fermer");
    ok.type = "button";
    ok.addEventListener("click", close);
    foot.appendChild(ok);
    card.appendChild(foot);

    host.appendChild(card);
    ok.focus();
    return { card: card, body: body, foot: foot, ok: ok, close: close };
  }
  window.jlrcpModal = { open: frame, layer: layer, close: close };

  function open(since) {
    var m = frame({
      title: since ? "Quoi de neuf ?" : "Journal des versions",
      label: "Quoi de neuf",
      sub: since ? "Nouveautés depuis votre dernière visite (version " + since + ")." : null
    });
    fill(m.body, since);
    if (since) {
      var all = el("button", "changelog-all", "Tout afficher");
      all.type = "button";
      all.addEventListener("click", function () {
        fill(m.body, null);
        m.body.scrollTop = 0;
        m.foot.removeChild(all);
        track("changelog-tout-afficher", {});
      });
      m.foot.insertBefore(all, m.ok);
    }
    track("changelog-ouvert", { depuis: since || "tout" });
  }

  function load(then) {
    if (data) { then(); return; }
    fetch("/changelog.json")
      .then(function (r) { return r.json(); })
      .then(function (d) { data = d; then(); })
      .catch(function () { /* no notes served: stay silent */ });
  }

  // ---- entry point ---------------------------------------------------------
  function boot() {
    // Manual openers ([data-changelog] in /a-propos + the home footer) always work,
    // even when there is nothing new to auto-show.
    var openers = document.querySelectorAll("[data-changelog]");
    Array.prototype.forEach.call(openers, function (node) {
      node.addEventListener("click", function (ev) {
        ev.preventDefault();
        load(function () { open(null); });
      });
    });

    var cur = window.__APP_VERSION__;
    if (!cur) return; // app-version.js missing (dev): nothing to compare against
    var seen = lsGet(SEEN_KEY);
    if (!seen) { lsSet(SEEN_KEY, cur); return; } // first visit: store, stay quiet
    if (cmp(seen, cur) >= 0) return; // up to date (or a rollback): nothing to say
    // Don't stack a popup on top of the guided tour: tour.js publishes this flag when
    // it actually starts one (WITHOUT storing our version, so the notes survive to the
    // next visit). We ask whether a tour is running now, not whether one was ever seen.
    if (window.__TOUR_ACTIVE__) return;
    load(function () {
      lsSet(SEEN_KEY, cur);
      open(seen);
    });
  }

  // Boot on DOMContentLoaded, never inline: deferred scripts all run BEFORE that event,
  // so tour.js has set (or not set) __TOUR_ACTIVE__ by the time we read it, whatever the
  // order of the <script> tags on the page.
  if (document.readyState === "complete") {
    boot();
  } else {
    document.addEventListener("DOMContentLoaded", boot);
  }
})();

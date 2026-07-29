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
  var TOUR_SEEN_KEY = "jlrcp_tour_seen"; // absent = first visit, the tour will run
  var data = null; // the parsed changelog.json, fetched at most once
  var overlay = null; // the open popup, if any

  function ls(get, key, val) {
    try {
      return get ? localStorage.getItem(key) : localStorage.setItem(key, val);
    } catch (e) {
      return null; // private mode / storage disabled: degrade to "never auto-open"
    }
  }
  function track(name, extra) {
    try {
      if (typeof window.trackEvent === "function") window.trackEvent(name, extra || {});
    } catch (e) { /* analytics must never break the popup */ }
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // "0.9.0" < "0.10.0": compare numerically, component by component.
  function cmp(a, b) {
    var x = String(a).split("."), y = String(b).split("."), i;
    for (i = 0; i < 3; i++) {
      var d = (parseInt(x[i], 10) || 0) - (parseInt(y[i], 10) || 0);
      if (d) return d < 0 ? -1 : 1;
    }
    return 0;
  }

  function frDate(iso) {
    try {
      var d = new Date(iso + "T12:00:00");
      return d.toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
    } catch (e) {
      return iso;
    }
  }

  // ---- rendering -----------------------------------------------------------
  function renderRelease(rel) {
    var box = el("section", "changelog-release");
    var h = el("h3", "changelog-version");
    h.appendChild(el("span", "changelog-num", "Version " + rel.version));
    h.appendChild(el("span", "changelog-date", frDate(rel.date)));
    box.appendChild(h);
    (rel.sections || []).forEach(function (sec) {
      var cat = (data.categories || {})[sec.key] || {};
      box.appendChild(el("h4", "changelog-cat", cat.fr || cat.en || sec.key));
      var ul = el("ul", "changelog-items");
      (sec.items || []).forEach(function (it) {
        var li = el("li", null, it.fr || it.en);
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
  function fill(body, since) {
    body.textContent = "";
    var shown = 0;
    (data.releases || []).forEach(function (rel) {
      if (since && cmp(rel.version, since) <= 0) return;
      body.appendChild(renderRelease(rel));
      shown++;
    });
    if (!shown) body.appendChild(el("p", "changelog-empty", "Aucune nouveauté à afficher."));
    return shown;
  }

  function close() {
    if (!overlay) return;
    document.removeEventListener("keydown", onKey, true);
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    overlay = null;
  }

  function onKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
  }

  function open(since) {
    close();
    overlay = el("div", "changelog-overlay");
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) close(); // click outside the card dismisses it
    });
    var card = el("div", "changelog-modal");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-label", "Quoi de neuf");

    var head = el("div", "changelog-head");
    head.appendChild(el("h2", "changelog-title", since ? "Quoi de neuf ?" : "Journal des versions"));
    var x = el("button", "changelog-close", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Fermer");
    x.addEventListener("click", close);
    head.appendChild(x);
    card.appendChild(head);
    if (since) {
      card.appendChild(
        el("p", "changelog-sub", "Nouveautés depuis votre dernière visite (version " + since + ").")
      );
    }

    var body = el("div", "changelog-body"); // the scrollable part
    fill(body, since);
    card.appendChild(body);

    var foot = el("div", "changelog-foot");
    if (since) {
      var all = el("button", "changelog-all", "Tout afficher");
      all.type = "button";
      all.addEventListener("click", function () {
        fill(body, null);
        body.scrollTop = 0;
        foot.removeChild(all);
        track("changelog-tout-afficher", {});
      });
      foot.appendChild(all);
    }
    var ok = el("button", "changelog-ok", "Fermer");
    ok.type = "button";
    ok.addEventListener("click", close);
    foot.appendChild(ok);
    card.appendChild(foot);

    overlay.appendChild(card);
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey, true);
    ok.focus();
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
    var seen = ls(true, SEEN_KEY);
    if (!seen) { ls(false, SEEN_KEY, cur); return; } // first visit: store, stay quiet
    if (cmp(seen, cur) >= 0) return; // up to date (or a rollback): nothing to say
    // Don't stack a popup on top of the guided tour.
    var tourParam = null;
    try { tourParam = new URLSearchParams(window.location.search).get("tour"); } catch (e) {}
    if (tourParam || !ls(true, TOUR_SEEN_KEY)) return; // keep the notes for next time
    load(function () {
      ls(false, SEEN_KEY, cur);
      open(seen);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

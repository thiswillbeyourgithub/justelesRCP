/*
 * theme.js - light / dark / auto theme control for justelesRCP.
 *
 * The site's palette already follows the OS via @media (prefers-color-scheme) in
 * style.css. This adds a reader-facing OVERRIDE on top of that, in ONE small file
 * loaded SYNCHRONOUSLY in <head> (before the stylesheet paints) so switching never
 * flashes the wrong theme:
 *   1. Apply the saved preference immediately by setting document.documentElement's
 *      data-theme ("light" | "dark"); "auto" removes it, letting the OS media query
 *      win again. The CSS keys the dark palette off BOTH triggers (see style.css).
 *   2. On DOMContentLoaded, inject one cycling toggle button (auto -> light -> dark
 *      -> auto) into the top bar where present, else a fixed top-right control on
 *      the sidebar-less landing page, and persist each choice in localStorage.
 *
 * CSP-safe: same-origin external script, no inline handlers, no eval, styling via
 * classes + CSSOM setters only (a style="" attribute would trip the strict CSP).
 * UI strings are French (site convention); code + comments are English. Keep in sync
 * with the palette + .theme-toggle rules in style.css and the <script src="/theme.js">
 * tag in every page <head> (index/a-propos/browse/rcp templates).
 */
(function () {
  "use strict";

  var KEY = "jlrcp_theme";                // localStorage: "light" | "dark" | "auto"
  var MODES = ["auto", "light", "dark"];  // cycle order on each click
  var root = document.documentElement;

  // Read the stored choice, defaulting to "auto" (follow the OS) for any missing or
  // unexpected value.
  function read() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }

  // Apply a mode by toggling the data-theme attribute the CSS keys off. "auto" means
  // NO attribute, so @media (prefers-color-scheme) stays in charge.
  function apply(mode) {
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
  }

  // Do the apply NOW, synchronously, before the first paint, to avoid a flash of the
  // OS theme when the reader has chosen a different one.
  apply(read());

  function labelFor(mode) {
    return mode === "light" ? "clair" : mode === "dark" ? "sombre" : "automatique";
  }
  // Glyph reflects the CURRENT mode: sun (light), moon (dark), half-disc (auto).
  function glyphFor(mode) {
    return mode === "light" ? "☀︎" : mode === "dark" ? "☾" : "◐";
  }

  function buildToggle() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-toggle";

    function refresh() {
      var m = read();
      btn.textContent = glyphFor(m);
      var text = "Thème : " + labelFor(m) + " (cliquer pour changer)";
      btn.setAttribute("aria-label", text);
      btn.title = text;
    }
    refresh();

    btn.addEventListener("click", function () {
      var next = MODES[(MODES.indexOf(read()) + 1) % MODES.length];
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: ignore */ }
      apply(next);
      refresh();
    });

    // Prefer to sit inside the top navigation; the landing page has no top bar, so
    // fall back to a fixed top-right control there.
    var nav = document.querySelector(".topbar .topnav") || document.querySelector(".topbar");
    if (nav) {
      btn.classList.add("theme-toggle-nav");
      nav.appendChild(btn);
    } else {
      btn.classList.add("theme-toggle-fixed");
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildToggle);
  } else {
    buildToggle();
  }
})();

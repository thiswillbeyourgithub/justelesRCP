/*
 * util.js - the small helpers every client script used to carry its own copy of.
 *
 * Exposed as window.jlrcp. Loaded deferred and FIRST in every src/*.html, so any
 * later deferred script can use it at load time (deferred scripts run in document
 * order). theme.js is the one exception: it runs synchronously in <head> before
 * this file, so its first-paint read cannot use these helpers.
 *
 * CSP-safe: same-origin external script, no eval, no inline handlers.
 */
(function () {
  "use strict";

  // document.createElement with an optional class and text content.
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // Forward an analytics event to app-init's guarded tracker. A no-op when metrics
  // are off; analytics must never break the caller.
  function track(name, data) {
    try {
      if (typeof window.trackEvent === "function") window.trackEvent(name, data || {});
    } catch (e) { /* best-effort */ }
  }

  // localStorage that degrades to "nothing stored" in private mode / blocked storage.
  function lsGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function lsSet(key, val) {
    try { localStorage.setItem(key, val); } catch (e) { /* not persisted */ }
  }
  function lsDel(key) {
    try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
  }

  // Accent-fold + lowercase, the matching key search and the semantic search share.
  function fold(s) {
    return String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  // An ISO date (YYYY-MM-DD) in French, e.g. "2 mai 2022". The date is parsed as UTC
  // midnight, so it is formatted in UTC too: a reader west of Greenwich would
  // otherwise see the previous day. ``opts`` overrides the default day/month/year
  // fields. Returns null when Intl is unavailable or the date is invalid.
  function frDate(iso, opts) {
    try {
      var o = opts || { day: "numeric", month: "long", year: "numeric" };
      var f = { timeZone: "UTC" };
      for (var k in o) f[k] = o[k];
      return new Intl.DateTimeFormat("fr-FR", f).format(new Date(iso + "T00:00:00Z"));
    } catch (e) {
      return null;
    }
  }

  // A whole number of days as a French relative age: "aujourd'hui", "il y a 3 jours",
  // "il y a 2 mois", "il y a 1 an".
  function ago(d) {
    if (d < 1) return "aujourd'hui";
    if (d < 31) return "il y a " + d + " jour" + (d > 1 ? "s" : "");
    if (d < 365) return "il y a " + Math.max(1, Math.round(d / 30)) + " mois";
    var yr = Math.floor(d / 365);
    return "il y a " + yr + " an" + (yr > 1 ? "s" : "");
  }

  window.jlrcp = {
    el: el, track: track, lsGet: lsGet, lsSet: lsSet, lsDel: lsDel,
    fold: fold, frDate: frDate, ago: ago,
  };
})();

// Optional "work in progress" banner across the top of the page.
//
// When the container is started with DEV=1 (docker/.env -> entrypoint ->
// /app-config.js), this shows a prominent notice that the site is a
// prototype in development, plus how
// long ago the container was last restarted. STARTED_AT (epoch seconds) is stamped by the entrypoint at
// container start, so "il y a X" reflects the real restart and stays accurate as
// the tab stays open.
//
// Off entirely unless DEV resolved to the literal "1": empty, "0" or a leftover
// "{{...}}" placeholder (local dev, no templating) all keep it hidden, so
// production looks normal unless explicitly enabled. Plain script, loaded after
// app-config.js so window.__APP_CONFIG__ exists. Self-contained: it creates its
// own element and inline styling, so no HTML/CSS changes are required.
//
// Clicking it dismisses it, and the dismissal is remembered per-device
// (localStorage) scoped to the current STARTED_AT, so it stays closed across page
// navigations and browser restarts and only reappears after the next container
// restart (new deployment).
(function () {
  const cfg = window.__APP_CONFIG__ || {};
  // Enabled only on an exact "1" so an unset/placeholder value never trips it.
  if (cfg.dev !== "1") return;

  const startedAt = Number(cfg.startedAt);
  const hasStart = Number.isFinite(startedAt) && startedAt > 0;

  // Persisted dismissal, scoped to THIS deployment. The token is the container's
  // STARTED_AT: dismissing stores it in localStorage (so the banner stays closed on
  // this device across page navigations AND a browser restart), and it reappears
  // only when a new deployment stamps a new STARTED_AT (i.e. "until the next
  // restart", the very event the banner reports). Absent a real stamp
  // ("Déploiement en cours") the token is empty and nothing is persisted, so the
  // WIP notice keeps showing until a real deployment lands.
  const DISMISS_KEY = "justelesRCP.devBannerDismissed";
  const deployToken = hasStart ? String(startedAt) : "";
  function isDismissed() {
    if (!deployToken) return false;
    try {
      return window.localStorage.getItem(DISMISS_KEY) === deployToken;
    } catch (_) {
      return false; // storage blocked/unavailable: fall back to always showing
    }
  }
  if (isDismissed()) return;

  // Human "il y a X minutes/heures/jours" from a count of elapsed seconds.
  function humanAgo(seconds) {
    const minutes = Math.max(0, Math.round(seconds / 60));
    if (minutes < 1) return "moins d'une minute";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} heure${hours === 1 ? "" : "s"}`;
    const days = Math.round(hours / 24);
    return `${days} jour${days === 1 ? "" : "s"}`;
  }

  // Optional "source" link. Only an http(s) URL is rendered so a stray/empty
  // value can't inject markup.
  const sourceUrl = String(cfg.sourceUrl || "").trim();
  const validSource = /^https?:\/\//i.test(sourceUrl) ? sourceUrl : "";

  const banner = document.createElement("div");
  banner.id = "dev-banner";
  banner.setAttribute("role", "status");
  // Non-sticky on purpose: the page already has a sticky top bar (and RCP pages a
  // sticky ToC), both at top:0. A sticky banner would sit at the same offset and
  // hide the nav on scroll. Static keeps the WIP notice visible on load, then
  // scrolls away, leaving the real nav sticky and unobscured.
  banner.style.cssText =
    "position:relative;z-index:1;background:#b45309;color:#fff;" +
    "padding:.5rem 1rem;font-size:.9rem;line-height:1.4;text-align:center;" +
    "cursor:pointer;";
  banner.title = "Cliquer pour masquer";

  function render() {
    // Client clock vs container clock can differ slightly; close enough for a
    // human "redémarré il y a ~X" cue. Clamp negatives (skewed clocks) to 0.
    const elapsed = hasStart ? Date.now() / 1000 - startedAt : 0;
    const when = hasStart
      ? `Dernier redémarrage il y a ${humanAgo(elapsed)}.`
      : "Déploiement en cours.";
    const source = validSource
      ? ` <a href="${validSource}" target="_blank" rel="noopener noreferrer">Code source</a>.`
      : "";
    banner.innerHTML =
      `<strong>Prototype en développement.</strong> ${when}${source}`;
    // Style the link via the CSSOM (a style="" attribute would trip the strict
    // style-src CSP; programmatic styles are allowed).
    const link = banner.querySelector("a");
    if (link) {
      link.style.color = "#fff";
      link.style.textDecoration = "underline";
    }
  }

  render();
  document.body.prepend(banner);

  // Keep the "il y a X" fresh without a reload while the tab stays open.
  const timer = hasStart ? setInterval(render, 60 * 1000) : null;

  // Clicking hides it and remembers the dismissal on this device (see isDismissed):
  // it stays closed across page navigations and browser restarts, and only returns
  // when the container restarts under a new STARTED_AT.
  banner.addEventListener("click", (e) => {
    if (e.target.closest("a")) return; // let the source link navigate
    banner.remove();
    if (timer) clearInterval(timer);
    if (deployToken) {
      try {
        window.localStorage.setItem(DISMISS_KEY, deployToken);
      } catch (_) {
        // storage blocked (private mode, quota): dismissal just won't persist
      }
    }
  });
})();

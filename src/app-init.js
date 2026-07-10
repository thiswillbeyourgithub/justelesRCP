// Injects the privacy-friendly umami metrics tag when configured at runtime,
// fills the build version into the page, and forwards clicks to umami as events.
//
// Config arrives in window.__APP_CONFIG__ from app-config.js, which the
// container fills from docker/.env (see docker/Caddyfile). When the values are
// empty (local dev, or metrics disabled) this is a no-op, so the page works
// with no tracking at all. Plain script, loaded after app-config.js so
// window.__APP_CONFIG__ exists.
(function () {
  const cfg = window.__APP_CONFIG__ || {};

  // Build version. Served at runtime from app-version.js (window.__APP_VERSION__,
  // written by build.py) rather than baked into each page, so page HTML stays
  // independent of the version and the incremental build cache survives a bump.
  // Fill every [data-app-version] slot (footer, sidebar, about page).
  const version = String(window.__APP_VERSION__ || "").trim();
  if (version) {
    document.querySelectorAll("[data-app-version]").forEach((el) => {
      el.textContent = "justelesRCP v" + version;
    });
  }

  // Point every [data-source-link] at the configured source repo (SOURCE_URL in
  // the container env). Kept in config, not hardcoded, so the repo URL (which
  // carries the author's GitHub handle) is not baked into the static site. Only
  // an http(s) value is accepted; otherwise the element keeps its fallback href.
  const sourceUrl = String(cfg.sourceUrl || "").trim();
  if (/^https?:\/\//i.test(sourceUrl)) {
    document.querySelectorAll("a[data-source-link]").forEach((a) => {
      a.href = sourceUrl;
    });
  }

  // A value is "set" only if it is a non-empty, non-placeholder string.
  function isSet(value) {
    return typeof value === "string" && value.length > 0 && !value.startsWith("{{");
  }

  // Value for umami's data-do-not-track attribute: "true" (the default) respects
  // the browser's Do Not Track signal so those visitors are not tracked; "false"
  // tracks everyone. Any other value falls back to the privacy-friendly default.
  function resolveDnt(value) {
    return typeof value === "string" && value.trim().toLowerCase() === "false"
      ? "false"
      : "true";
  }

  if (isSet(cfg.url) && isSet(cfg.websiteId)) {
    const script = document.createElement("script");
    script.defer = true;
    script.src = cfg.url;
    script.setAttribute("data-website-id", cfg.websiteId);
    // Always emit data-do-not-track explicitly so the chosen behavior is visible.
    script.setAttribute("data-do-not-track", resolveDnt(cfg.dnt));
    // Optional Subresource Integrity for the umami script (requires CORS).
    if (isSet(cfg.sri)) {
      script.integrity = cfg.sri;
      script.crossOrigin = "anonymous";
    }
    document.head.appendChild(script);
  }

  // Guarded event tracker: forwards to umami when it loaded, no-op otherwise (so
  // call sites never need to check, and nothing happens when metrics are off or
  // the visitor is Do-Not-Track).
  window.trackEvent = function (name, data) {
    if (window.umami && typeof window.umami.track === "function") {
      try {
        window.umami.track(name, data);
      } catch (_) {
        /* best-effort */
      }
    }
  };

  // Best-effort: capture clicks on interactive elements, labelled by (in
  // priority) an explicit data-track, aria-label, id, or trimmed text. No
  // per-call-site wiring, and a no-op when umami is absent.
  document.addEventListener(
    "click",
    (ev) => {
      const el = ev.target.closest(
        "[data-track], button, a[href], #results li, .azbar a, .drug-list a"
      );
      if (!el) return;
      const label = (
        el.getAttribute("data-track") ||
        el.getAttribute("aria-label") ||
        el.id ||
        (el.textContent || "").trim()
      ).slice(0, 60);
      if (label) window.trackEvent("click", { target: label });
    },
    { capture: true }
  );
})();

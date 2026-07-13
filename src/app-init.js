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

  // RCP freshness banner: turn the baked absolute "as of" date into a relative
  // age ("il y a X") and flag data older than a year. Runs only on RCP pages
  // (the [data-rcp-asof] element is absent elsewhere). build.py bakes the
  // absolute date so no-JS readers still see it; we only enhance it here, which
  // keeps the page cacheable while the age stays correct without a rebuild.
  (function () {
    const el = document.querySelector("[data-rcp-asof]");
    if (!el) return;
    const iso = (el.getAttribute("data-rcp-asof") || "").trim();
    const when = new Date(iso + "T00:00:00Z");
    if (isNaN(when.getTime())) return;
    const days = Math.floor((Date.now() - when.getTime()) / 86400000);
    if (days < 0) return; // future date (clock skew): leave the baked text alone

    function humanAge(d) {
      if (d < 1) return "aujourd'hui";
      if (d < 31) return "il y a " + d + " jour" + (d > 1 ? "s" : "");
      if (d < 365) return "il y a " + Math.max(1, Math.round(d / 30)) + " mois";
      const yr = Math.floor(d / 365);
      return "il y a " + yr + " an" + (yr > 1 ? "s" : "");
    }

    try {
      const dateStr = new Intl.DateTimeFormat("fr-FR", {
        day: "numeric",
        month: "long",
        year: "numeric",
      }).format(when);
      el.textContent =
        "Informations à jour au " + dateStr + " (" + humanAge(days) + ").";
    } catch (_) {
      /* Intl unavailable: keep build.py's baked absolute date. */
    }
    if (days > 365) {
      el.classList.add("stale");
      el.append(
        " Ces informations datent de plus d'un an ; vérifiez une source à jour."
      );
    }
  })();

  // On-demand RCP refresh: a "Rafraichir maintenant" button plus an automatic
  // background refresh when the page's data is over a year old. Both call the
  // same-origin companion service POST /api/refresh/<cis> (refresh-service.py),
  // which Caddy reverse-proxies, so the strict `connect-src 'self'` CSP allows
  // it. Everything degrades gracefully: on a plain static deploy with no service
  // the fetch simply fails and the button reports it unavailable, changing
  // nothing else. The button is created here (not baked) so no-JS pages don't
  // show a dead control; styling is via the .rcp-refresh class (a style="" attr
  // would trip the strict style-src CSP).
  (function () {
    const main = document.querySelector(".rcp[data-cis]");
    if (!main) return; // not an RCP page
    const cis = (main.getAttribute("data-cis") || "").trim();
    if (!/^\d{8}$/.test(cis)) return; // missing/placeholder CIS: nothing to refresh

    const asofEl = document.querySelector("[data-rcp-asof]");
    const bakedAsof = asofEl
      ? (asofEl.getAttribute("data-rcp-asof") || "").trim()
      : "";
    const ageDays = bakedAsof
      ? Math.floor((Date.now() - new Date(bakedAsof + "T00:00:00Z").getTime()) / 86400000)
      : NaN;

    function refresh(cisId) {
      return fetch("/api/refresh/" + cisId, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
    }

    const box = document.createElement("p");
    box.className = "rcp-refresh";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Rafraîchir maintenant";
    const msg = document.createElement("span");
    msg.className = "msg";
    box.append(btn, msg);
    const anchor = asofEl || main.querySelector(".cis");
    if (anchor && anchor.parentNode) anchor.after(box);
    else main.prepend(box);

    let polling = false;
    function setMsg(text) {
      msg.textContent = text ? " " + text : "";
    }

    // After a refresh is queued, poll the service until it reports a scrape date
    // newer than the one baked into this page, then reload to show the fresh RCP.
    function pollUntilFresh(deadline) {
      fetch("/api/status/" + cis, { headers: { Accept: "application/json" } })
        .then((r) => r.json())
        .then((s) => {
          if (s.asof && s.asof !== bakedAsof && !s.pending) {
            setMsg("à jour, rechargement…");
            location.reload();
            return true;
          }
          return false;
        })
        .catch(() => false)
        .then((done) => {
          if (done) return;
          if (Date.now() < deadline) {
            setTimeout(() => pollUntilFresh(deadline), 3000);
          } else {
            polling = false;
            btn.disabled = false;
            setMsg("la mise à jour prend du temps ; réessayez plus tard.");
          }
        });
    }

    btn.addEventListener("click", () => {
      if (polling) return;
      btn.disabled = true;
      setMsg("mise à jour demandée…");
      refresh(cis)
        .then((r) => r.json())
        .then((s) => {
          if (s.status === "fresh") {
            if (s.asof && s.asof !== bakedAsof) {
              location.reload();
              return;
            }
            btn.disabled = false;
            setMsg("déjà à jour.");
          } else if (s.status === "busy") {
            btn.disabled = false;
            setMsg("service occupé, réessayez plus tard.");
          } else {
            polling = true;
            setMsg("mise à jour en cours…");
            pollUntilFresh(Date.now() + 90000);
          }
        })
        .catch(() => {
          btn.disabled = false;
          setMsg("rafraîchissement indisponible.");
        });
    });

    // Automatic, fire-and-forget refresh when the page is over a year old. The
    // server dedups + rate-limits, so many visitors on the same stale page cause
    // a single fetch; the freshened page shows up on a later visit. We do not
    // reload here (no surprise reloads), we just nudge the queue.
    if (Number.isFinite(ageDays) && ageDays > 365) {
      refresh(cis).catch(() => {});
    }
  })();

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

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

  // RCP freshness banner. Two baked dates, distinct on purpose (see build.py
  // _asof_html): data-rcp-ansm is ANSM's own revision date (the headline "à jour
  // au …", .rcp-primary), and data-rcp-asof is when WE last captured the copy
  // (.rcp-checked line). We turn each absolute date into a relative age here
  // (page stays cacheable) and, keyed on OUR capture age only, add a soft
  // "notre copie" notice: a stable old RCP is still ANSM's current text, but a
  // copy we have not re-checked in over a year may lag their live version.
  // Runs only on RCP pages (the element is absent elsewhere).
  (function () {
    const el = document.querySelector("[data-rcp-asof], [data-rcp-ansm]");
    if (!el) return;

    function ageDays(iso) {
      const when = new Date(iso + "T00:00:00Z");
      if (isNaN(when.getTime())) return NaN;
      return Math.floor((Date.now() - when.getTime()) / 86400000);
    }
    function humanAge(d) {
      if (d < 1) return "aujourd'hui";
      if (d < 31) return "il y a " + d + " jour" + (d > 1 ? "s" : "");
      if (d < 365) return "il y a " + Math.max(1, Math.round(d / 30)) + " mois";
      const yr = Math.floor(d / 365);
      return "il y a " + yr + " an" + (yr > 1 ? "s" : "");
    }
    function frDate(iso) {
      try {
        return new Intl.DateTimeFormat("fr-FR", {
          day: "numeric",
          month: "long",
          year: "numeric",
        }).format(new Date(iso + "T00:00:00Z"));
      } catch (_) {
        return null; // Intl unavailable: keep build.py's baked absolute date
      }
    }

    const ansmIso = (el.getAttribute("data-rcp-ansm") || "").trim();
    const asofIso = (el.getAttribute("data-rcp-asof") || "").trim();

    // Headline: ANSM's revision date when known, else our capture date.
    const headIso = ansmIso || asofIso;
    const headDays = ageDays(headIso);
    const primary = el.querySelector(".rcp-primary");
    if (primary && !isNaN(headDays) && headDays >= 0) {
      const d = frDate(headIso);
      if (d) {
        primary.textContent =
          "Informations à jour au " + d + " (" + humanAge(headDays) + ").";
      }
    }

    // Small secondary line: when justelesRCP last verified the copy against ANSM.
    const checked = el.querySelector(".rcp-checked");
    const asofDays = ageDays(asofIso);
    if (checked && !isNaN(asofDays) && asofDays >= 0) {
      const d = frDate(asofIso);
      if (d) {
        checked.textContent =
          "Version vérifiée par justelesRCP le " +
          d +
          " (" +
          humanAge(asofDays) +
          ").";
      }
    }

    // "Our copy is old" notice, keyed on OUR capture age (never the ANSM
    // revision age). The refresh button below fetches the live version on demand.
    if (!isNaN(asofDays) && asofDays > 365) {
      el.classList.add("stale");
      const warn = document.createElement("span");
      warn.className = "rcp-warn";
      warn.textContent =
        "Notre copie n'a pas été actualisée depuis plus d'un an ; utilisez « Rafraîchir maintenant » pour récupérer la dernière version publiée par l'ANSM.";
      el.append(warn);
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
    // No baked capture date means there is nothing to refresh against, so skip the
    // whole refresh control. The refresh flow is keyed off data-rcp-asof by design.
    // This is what gates the button correctly on the /eu/ side: a lightweight EU
    // stub (a mere pointer to the EMA) has no capture date and shows no button,
    // while a full /eu/ page (the EMA PDF converted on-site) DOES bake one and gets
    // the button, whose POST the refresh service routes to its EMA lane.
    if (!bakedAsof) return;
    const ageDays = bakedAsof
      ? Math.floor((Date.now() - new Date(bakedAsof + "T00:00:00Z").getTime()) / 86400000)
      : NaN;

    // src labels the trigger for the service's crawl stats: "user" for the
    // button below, "auto" for the fire-and-forget >1-year refresh further down.
    function refresh(cisId, src) {
      return fetch("/api/refresh/" + cisId + "?src=" + src, {
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
      refresh(cis, "user")
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
      refresh(cis, "auto").catch(() => {});
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

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
    // A lightweight EU stub (a mere pointer to the EMA, no copy captured yet) is
    // detectable by its .rcp-stub body and has no data-rcp-asof. We still want a
    // button on it: clicking asks the refresh service to fetch + convert this
    // drug's EMA PDF on demand (its EMA lane harvests the link live if needed), so
    // the reader never has to wait for the background crawler to reach it. A page
    // WITHOUT a capture date AND not a stub (a plain RCP page missing its asof) has
    // nothing to refresh against, so we skip the control entirely.
    const isEuStub = !!document.querySelector(".rcp-stub");
    if (!bakedAsof && !isEuStub) return;
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
    // On a stub we have no copy yet, so the action is "fetch it", not "refresh it".
    // Be explicit that justelesRCP ITSELF downloads + shows the document here: readers
    // misread the old "Récupérer le RCP depuis l'EMA" as a link to go read it at the EMA.
    btn.textContent = isEuStub
      ? "Importer le RCP de l'EMA sur justelesRCP"
      : "Rafraîchir maintenant";
    const msg = document.createElement("span");
    msg.className = "msg";
    box.append(btn);
    // Pull the server-rendered "Ouvrir la source officielle" link (baked as a
    // no-JS fallback in .rcp-source) up INTO this control, right after the button
    // with an "ou" separator, so the reader sees "Rafraîchir maintenant ou Ouvrir
    // la source officielle" inside the freshness card. Only a lone source link is
    // paired: both ANSM RCP pages and full /eu/ pages now carry a single source
    // button (the direct EMA PDF on /eu/; the EMA search moved to "En savoir plus").
    // No .rcp-source (e.g. a stub) => nothing to pair.
    const srcP = document.querySelector(".rcp-source");
    if (srcP && srcP.querySelectorAll(".official-link").length === 1) {
      const link = srcP.querySelector(".official-link");
      const or = document.createElement("span");
      or.className = "rcp-refresh-or";
      or.textContent = "ou";
      box.append(or, link);
      srcP.remove();
    }
    box.append(msg);
    // Prefer to sit INSIDE the freshness "carton" (.rcp-asof), so the refresh
    // control reads as belonging to those capture dates. A stub has no such card
    // (no asofEl): fall back to just after the CIS line, else prepend to main.
    if (asofEl) {
      asofEl.append(box);
    } else {
      const anchor = main.querySelector(".cis");
      if (anchor && anchor.parentNode) anchor.after(box);
      else main.prepend(box);
    }

    let polling = false;
    function setMsg(text) {
      msg.textContent = text ? " " + text : "";
      msg.classList.remove("msg-note");
    }
    // A standing, wrapping note (muted, under the button) for a TERMINAL outcome the
    // reader should keep seeing, e.g. the "retiré" case. setMsg() is the transient
    // inline variant; setNote() the persistent block one.
    function setNote(text) {
      msg.textContent = text || "";
      msg.classList.toggle("msg-note", !!text);
    }
    // Set the reader's expectation clearly: the page reloads itself once the copy is
    // ready (usually well under 30 s), and if it takes longer they can just refresh.
    // A stub is a first-time DOWNLOAD from the EMA, so it gets "récupération" wording.
    const askedMsg = isEuStub ? "Récupération demandée…" : "Mise à jour demandée…";
    const workingMsg = isEuStub
      ? "Récupération du RCP depuis l'EMA… la page se rechargera automatiquement dès qu'il est prêt, en général en moins de 30 secondes."
      : "Mise à jour en cours… la page se rechargera automatiquement dès qu'elle est prête, en général en moins de 30 secondes.";
    const slowMsg =
      "C'est un peu plus long que prévu ; rechargez la page dans un instant pour voir le résultat.";
    // The honest terminal message when the ANSM has no RCP for this drug (delisted /
    // retiré de la base): we keep showing our last captured copy and say so, instead
    // of reload-looping on a date that can never match (see the .archived handling).
    const copyLabel = bakedAsof
      ? (function () {
          try {
            return new Date(bakedAsof + "T00:00:00Z").toLocaleDateString("fr-FR", {
              month: "long",
              year: "numeric",
            });
          } catch (_) {
            return "";
          }
        })()
      : "";
    const retiredNote =
      "L'ANSM ne publie plus de RCP pour ce médicament (retiré de la base). " +
      "Nous affichons notre dernière copie" +
      (copyLabel ? " (" + copyLabel + ")" : "") +
      ".";

    // --- persistent feedback (survives a reload) ----------------------------
    // A reader often reloads to "check", which used to wipe every inline message.
    // We remember the last outcome for THIS drug in localStorage (keyed by CIS,
    // self-pruning) and re-show it on load; a pending refresh resumes its poll.
    const STORE_KEY = "jlrcp_maj_" + cis;
    const STORE_TTL = 3 * 86400000; // keep an outcome visible for ~3 days, then forget
    function remember(outcome) {
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify({ t: Date.now(), o: outcome }));
      } catch (_) {}
    }
    function recall() {
      try {
        const rec = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
        if (!rec || !rec.o) return null;
        if (Date.now() - (rec.t || 0) > STORE_TTL) {
          localStorage.removeItem(STORE_KEY);
          return null;
        }
        return rec;
      } catch (_) {
        return null;
      }
    }
    function forget() {
      try {
        localStorage.removeItem(STORE_KEY);
      } catch (_) {}
    }

    // The ANSM has no RCP for this drug: honest terminal state, never a reload.
    function showRetired() {
      polling = false;
      btn.disabled = false;
      setNote(retiredNote);
      remember("retired");
    }
    // A genuinely newer copy landed: remember it so the reloaded page can show a
    // brief confirmation, then reload. Reached ONLY when the drug is NOT archived,
    // so a delisted drug (whose baked date can never match) never loops here.
    function reloadFresh() {
      remember("updated");
      setMsg("à jour, rechargement…");
      location.reload();
    }

    // After a refresh is queued, poll the service until it either reports the drug is
    // archived (no ANSM RCP) or a capture date newer than this page's, then resolve.
    function pollUntilFresh(deadline) {
      fetch("/api/status/" + cis, { headers: { Accept: "application/json" } })
        .then((r) => r.json())
        .then((s) => {
          if (s.archived) {
            showRetired();
            return true;
          }
          if (s.asof && s.asof !== bakedAsof && !s.pending) {
            reloadFresh();
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
            setMsg(slowMsg);
            remember("slow");
          }
        });
    }

    btn.addEventListener("click", () => {
      if (polling) return;
      btn.disabled = true;
      setMsg(askedMsg);
      remember("pending");
      refresh(cis, "user")
        .then((r) => r.json())
        .then((s) => {
          if (s.archived) {
            showRetired();
            return;
          }
          if (s.status === "fresh") {
            if (s.asof && s.asof !== bakedAsof) {
              reloadFresh();
              return;
            }
            btn.disabled = false;
            setMsg("déjà à jour.");
            forget();
          } else if (s.status === "busy") {
            btn.disabled = false;
            setMsg("service occupé, réessayez plus tard.");
            remember("busy");
          } else {
            polling = true;
            setMsg(workingMsg);
            pollUntilFresh(Date.now() + 90000);
          }
        })
        .catch(() => {
          btn.disabled = false;
          setMsg("rafraîchissement indisponible.");
          forget();
        });
    });

    // Restore the last outcome for this drug so a reload doesn't lose the feedback.
    const stored = recall();
    if (stored) {
      if (stored.o === "retired") {
        setNote(retiredNote);
      } else if (stored.o === "pending") {
        // A refresh was in flight when the page was left/reloaded: resume its poll.
        polling = true;
        btn.disabled = true;
        setMsg(workingMsg);
        pollUntilFresh(Date.now() + 90000);
      } else if (stored.o === "updated") {
        // Just reloaded after a successful refresh: one-shot confirmation, then clear.
        setMsg("à jour, vérifié à l'instant.");
        forget();
      }
    }

    // Automatic, fire-and-forget refresh when the page is over a year old. The
    // server dedups + rate-limits, so many visitors on the same stale page cause a
    // single fetch; the freshened page shows up on a later visit. We do not reload
    // here (no surprise reloads). Skip it entirely for a drug already known to be
    // retiré (re-fetching a delisted RCP is pointless), but still LEARN the retiré
    // state from the response so an old delisted page shows the honest note on load
    // without a click.
    const knownRetired = stored && stored.o === "retired";
    if (!knownRetired && Number.isFinite(ageDays) && ageDays > 365) {
      refresh(cis, "auto")
        .then((r) => r.json())
        .then((s) => {
          if (s && s.archived) showRetired();
        })
        .catch(() => {});
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
      // The in-RCP semantic search emits its OWN privacy-safe events from
      // rcp-semsearch.js (usage + prev/next, never the query). Skip its controls
      // here so a result snippet (page text) never becomes an event label and nav
      // clicks are not double-counted.
      if (el.closest(".semsearch")) return;
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

// The dock: the sticky block holding the Sommaire (.toc) and the semantic-search box
// (rcp-semsearch.js appends it here), plus the top bar it sits under. RCP pages only.
//
// One place owns how that chrome moves, so nothing else hardcodes an offset:
//   - measures the top bar height (--bar-h) and the Sommaire's share of the dock
//     (--dock-lift) and the search box height (--dock-peek) into CSS variables;
//   - scrolling DOWN slides the top bar and the Sommaire out of view
//     (body.chrome-hidden, see .rcp-dock in style.css), leaving only the search box at
//     the top of the screen; scrolling UP slides them back;
//   - window.jlrcpDock lets the search box ask where it rests and keep the chrome
//     hidden while IT scrolls the page (a jump to a passage is not the reader scrolling
//     up, and must not bring the Sommaire back);
//   - snaps the Sommaire shut after a section link is tapped.
// Plain script, loaded with defer before rcp-semsearch.js. No-op without a dock.
(function () {
  var dock = document.querySelector(".rcp-dock");
  if (!dock) return;
  var root = document.documentElement;
  var body = document.body;
  var bar = document.querySelector(".topbar");
  var toc = dock.querySelector("details.toc");

  // --- measurements -> CSS variables -----------------------------------------
  var barH = 0;
  var lift = 0;
  function measure() {
    barH = bar ? bar.offsetHeight : 0;
    // The search box is the dock's last panel; everything above it slides away, except
    // the dock's own top padding, which keeps a gap between the screen edge and the box.
    var box = dock.lastElementChild;
    var pad = parseFloat(getComputedStyle(dock).paddingTop) || 0;
    lift = box && box !== toc ? box.offsetTop - pad : dock.offsetHeight;
    root.style.setProperty("--bar-h", barH + "px");
    root.style.setProperty("--dock-lift", lift + "px");
    root.style.setProperty("--dock-peek", dock.offsetHeight - lift + "px");
  }
  measure();
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(measure);
    ro.observe(dock);
    if (bar) ro.observe(bar);
    new MutationObserver(measure).observe(dock, { childList: true });
  } else {
    window.addEventListener("resize", measure);
  }

  // --- hide on scroll down, show on scroll up ---------------------------------
  var SLACK = 8; // px; ignores sub-pixel / momentum jitter
  var last = window.scrollY;
  var holdUntilIdle = false; // a programmatic scroll is running: keep the chrome hidden
  var idleTimer = 0;
  // The hold ends when the scroll does ("scrollend"). A smooth scroll can pause for
  // several hundred ms between scroll events on a busy device, so where the browser
  // has scrollend the idle timer is only a backstop (a jump that needed no scroll fires
  // no scrollend); without it (older Safari), a short silence has to do.
  var HAS_SCROLLEND = "onscrollend" in window;
  var IDLE_MS = HAS_SCROLLEND ? 1500 : 250;
  function release() {
    clearTimeout(idleTimer);
    holdUntilIdle = false;
    last = window.scrollY;
  }
  function releaseAfter(ms) {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(release, ms);
  }
  if (HAS_SCROLLEND) {
    // Chrome also fires scrollend when a new smooth scroll replaces a running one, so
    // only release if nothing moves for a moment after it (a scroll event in between
    // re-arms the long timer in onScroll).
    window.addEventListener("scrollend", function () { if (holdUntilIdle) releaseAfter(150); });
  }

  function hidden() { return body.classList.contains("chrome-hidden"); }
  // Announces each change as a "jlrcp-dock" window event: the search box re-fits its
  // results list to the room the chrome leaves.
  function setHidden(h) {
    if (h === hidden()) return;
    body.classList.toggle("chrome-hidden", h);
    window.dispatchEvent(new Event("jlrcp-dock"));
  }

  // Hiding is only sensible once the dock is stuck; in flow, the reader is still at
  // the top of the page where the title and the Sommaire belong.
  function stuck() { return dock.getBoundingClientRect().top <= barH + 1; }

  function canHide() {
    return !(toc && toc.open) && !window.__TOUR_ACTIVE__ && (hidden() || stuck());
  }

  var dockH = dock.offsetHeight;
  function onScroll() {
    var y = window.scrollY;
    if (holdUntilIdle) { last = y; releaseAfter(IDLE_MS); return; }
    // When the dock grows or shrinks (a status line in the search box comes and goes),
    // the browser shifts the page to keep the text in place. That shift is not the
    // reader scrolling, so resync instead of reacting to it.
    var h = dock.offsetHeight;
    if (h !== dockH) { dockH = h; last = y; return; }
    if (Math.abs(y - last) < SLACK) return;
    setHidden(y > last && canHide());
    last = y;
  }
  var ticking = false;
  window.addEventListener("scroll", function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () { ticking = false; onScroll(); });
  }, { passive: true });

  // --- API for the search box -------------------------------------------------
  window.jlrcpDock = {
    // Hide the chrome and keep it hidden until the page stops scrolling. Call it right
    // BEFORE scrolling the page yourself.
    holdHidden: function () {
      // At the top of the page (dock not stuck yet) or during the tour: leave it be.
      if (window.__TOUR_ACTIVE__ || !(hidden() || stuck())) return;
      setHidden(true);
      holdUntilIdle = true;
      releaseAfter(Math.max(IDLE_MS, 400)); // a smooth scroll can take a moment to start
    },
    // Viewport y where `el` (a panel of the dock) sits once the dock is stuck, in the
    // chrome's current state. The search box uses it to scroll itself there and to size
    // its results list.
    restTop: function (el) {
      var offset = el.offsetTop; // the dock is the panels' offsetParent (sticky)
      return hidden() ? offset - lift : barH + offset;
    },
  };

  // --- Sommaire ---------------------------------------------------------------
  if (toc) {
    toc.addEventListener("click", function (ev) {
      if (!ev.target.closest("nav a")) return;
      toc.open = false; // the open list would cover the section just jumped to
      window.jlrcpDock.holdHidden();
    });
    toc.addEventListener("toggle", function () {
      if (toc.open) setHidden(false);
    });
  }
})();

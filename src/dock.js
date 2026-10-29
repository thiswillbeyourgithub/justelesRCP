// The dock: the sticky block holding the Sommaire (.toc) and the semantic-search box
// (rcp-semsearch.js appends it here), plus the top bar it sits under. RCP pages only.
//
// One number drives how that chrome moves: `off`, how many pixels of it have scrolled
// away, published as --chrome-off (see .rcp-dock in style.css). Scrolling down by N px
// grows it by N, scrolling up shrinks it by N, clamped to [0, max] where max is the top
// bar plus the Sommaire. So the chrome moves exactly with the page, pixel for pixel, and
// the search box is all that is left once it has fully scrolled away. No animation,
// no hidden/shown state.
//
// window.jlrcpDock lets the search box ask where it rests and push the chrome away
// before IT scrolls the page (a jump to a passage above is not the reader scrolling up,
// and must not bring the Sommaire back).
// Plain script, loaded with defer before rcp-semsearch.js. No-op without a dock.
(function () {
  var dock = document.querySelector(".rcp-dock");
  if (!dock) return;
  var root = document.documentElement;
  var bar = document.querySelector(".topbar");
  var toc = dock.querySelector("details.toc");

  // --- measurements ----------------------------------------------------------
  var barH = 0;
  var lift = 0; // how much of the dock scrolls away: everything above the search box
  function measure() {
    barH = bar ? bar.offsetHeight : 0;
    // The dock's own top padding stays, as a gap between the screen edge and the box.
    var box = dock.lastElementChild;
    var pad = parseFloat(getComputedStyle(dock).paddingTop) || 0;
    lift = box && box !== toc ? box.offsetTop - pad : dock.offsetHeight;
    root.style.setProperty("--bar-h", barH + "px");
    root.style.setProperty("--dock-peek", dock.offsetHeight - lift + "px");
    setOff(off); // re-clamp to the new maximum
  }
  function max() { return barH + lift; }

  // --- the offset --------------------------------------------------------------
  var off = 0;
  function setOff(v) {
    v = Math.max(0, Math.min(max(), v));
    if (v === off && root.style.getPropertyValue("--chrome-off")) return;
    off = v;
    root.style.setProperty("--chrome-off", off + "px");
    window.dispatchEvent(new Event("jlrcp-dock")); // the search box re-fits its results
  }
  // The open Sommaire and the guided tour need everything in view.
  function pinned() { return (toc && toc.open) || window.__TOUR_ACTIVE__; }

  var last = window.scrollY;
  var held = false; // our own scroll is running: keep the chrome away until it ends
  var holdTimer = 0;
  function endHold() { clearTimeout(holdTimer); held = false; last = window.scrollY; }
  function holdFor(ms) { clearTimeout(holdTimer); holdTimer = setTimeout(endHold, ms); }
  var dockH = dock.offsetHeight;
  function onScroll() {
    var y = window.scrollY;
    var h = dock.offsetHeight;
    if (held) {
      holdFor(1500); // backstop for browsers without scrollend
    } else if (h === dockH) {
      setOff(pinned() ? 0 : off + (y - last));
    }
    // else: the dock changed height (a status line in the search box) and the browser
    // shifted the page to keep the text still. Not the reader scrolling: ignore it.
    dockH = h;
    last = y;
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  // Chrome also fires scrollend when one smooth scroll replaces another, so the hold
  // ends only if nothing moves for a moment after it (a scroll event re-arms 1.5 s).
  window.addEventListener("scrollend", function () { if (held) holdFor(150); });

  measure();
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(measure);
    ro.observe(dock);
    if (bar) ro.observe(bar);
  } else {
    window.addEventListener("resize", measure);
  }

  // --- API for the search box -------------------------------------------------
  window.jlrcpDock = {
    // Push the chrome fully away and keep it there until the page stops scrolling.
    // Call it right BEFORE scrolling the page yourself.
    holdHidden: function () {
      if (pinned()) return;
      setOff(max());
      held = true;
      holdFor(1500);
    },
    // Viewport y where `el` (a panel of the dock) sits once the dock is stuck, at the
    // current offset. The search box uses it to scroll itself there and to size its
    // results list.
    restTop: function (el) {
      return barH - off + el.offsetTop; // the dock is the panels' offsetParent (sticky)
    },
  };

  // --- Sommaire ---------------------------------------------------------------
  if (toc) {
    toc.addEventListener("click", function (ev) {
      if (!ev.target.closest("nav a")) return;
      toc.open = false; // the open list would cover the section just jumped to
      window.jlrcpDock.holdHidden();
    });
    toc.addEventListener("toggle", function () { if (toc.open) setOff(0); });
  }
})();

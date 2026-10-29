// RCP "Sommaire" (table of contents) behavior. RCP pages only.
//
// The <details class="toc"> ships collapsed (no `open` attribute) and is now a
// collapsible in-flow block on ALL viewports (no more desktop sidebar). The one
// thing CSS can't do: once open, the list pushes down / covers the section you
// jumped to, so snap it shut again after a section link is tapped.
// Plain script, loaded with defer. No-op on pages that have no ToC.
(function () {
  var toc = document.querySelector("details.toc");
  if (!toc) return;
  toc.addEventListener("click", function (ev) {
    if (ev.target.closest("nav a")) toc.open = false;
  });

  // The stuck Sommaire bar costs a lot of height on a phone: slide it away while the
  // reader scrolls DOWN, bring it back as soon as they scroll UP (body.toc-hidden,
  // styled in style.css, which also lifts the sticky search box into its slot). Never
  // while it is open (the reader is using it), near the top of the page, or during
  // the tour (which spotlights the .toc). SLACK ignores sub-pixel / momentum jitter.
  var SLACK = 8;
  var last = window.scrollY;
  var ticking = false;
  function update() {
    ticking = false;
    var y = window.scrollY;
    if (Math.abs(y - last) < SLACK) return;
    var hidden = document.body.classList.contains("toc-hidden");
    // Only hide a bar that is already stuck: hiding it in flow would leave a hole.
    var stuck = hidden ||
      toc.getBoundingClientRect().top <= parseFloat(getComputedStyle(toc).top) + 1;
    var hide = y > last && stuck && !toc.open && !window.__TOUR_ACTIVE__;
    document.body.classList.toggle("toc-hidden", hide);
    last = y;
  }
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, { passive: true });
  toc.addEventListener("toggle", function () {
    if (toc.open) document.body.classList.remove("toc-hidden");
  });
})();

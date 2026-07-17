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
})();

// RCP "Sommaire" (table of contents) behavior. RCP pages only.
//
// The <details class="toc"> ships collapsed (no `open` attribute), so on phones
// it starts folded away instead of burying the top of the document; on wide
// screens style.css reveals it as a permanent sidebar regardless of open state.
// The one thing CSS can't do: on phones the open list covers the section you
// just jumped to, so snap it shut again after a section link is tapped.
// Plain script, loaded with defer. No-op on pages that have no ToC.
(function () {
  var toc = document.querySelector("details.toc");
  if (!toc) return;
  var phone = window.matchMedia("(max-width: 59.99rem)");
  toc.addEventListener("click", function (ev) {
    if (phone.matches && ev.target.closest("nav a")) toc.open = false;
  });
})();

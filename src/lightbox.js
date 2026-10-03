/*
 * lightbox.js - click-to-zoom for images inside a drug page.
 *
 * The only content images on the site are the figures embedded in converted EMA /eu/
 * pages (molecular structures, charts; ANSM RCPs occasionally carry one too). On the
 * page they are CSS-scaled down to the reading column; clicking one opens it full-size
 * in a centred overlay. Closes on a backdrop click, the close button, or Escape, and
 * works on mobile (the image fits the viewport and native pinch-zoom still applies; the
 * whole backdrop is a large tap target to dismiss).
 *
 * CSP-safe: same-origin external script, no inline handlers, no eval, styling via
 * classes + CSSOM setters only. Loaded (deferred) by rcp.html, after util.js and
 * changelog.js; a no-op on pages with no
 * drug body or no images. UI strings are French (site convention); code + comments are
 * English. Keep in sync with the .lightbox* rules in style.css and the
 * <script src="/lightbox.js"> tag in src/rcp.html.
 */
(function () {
  "use strict";

  var scope = document.querySelector(".rcp");
  if (!scope) return;
  var imgs = scope.querySelectorAll("img");
  if (!imgs.length) return;

  // The overlay mechanics (one at a time, backdrop click + Escape close, page frozen,
  // focus restored on close) are changelog.js's shared layer (window.jlrcpModal),
  // loaded on every page before this file; only the image-specific bits live here.
  var modal = window.jlrcpModal;
  if (!modal) return;
  var el = window.jlrcp.el;

  function open(src, alt) {
    var overlay = modal.layer("lightbox");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Image agrandie");

    var closeBtn = el("button", "lightbox-close", "×");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Fermer l'image");
    closeBtn.addEventListener("click", modal.close);

    var big = el("img", "lightbox-img");
    big.src = src;
    big.alt = alt || "";
    // A click on the image itself must NOT close (so a reader can pinch-zoom / drag
    // it): the layer only closes on a click that lands on the backdrop itself.

    overlay.appendChild(closeBtn);
    overlay.appendChild(big);
    try { closeBtn.focus(); } catch (e) {}
  }

  imgs.forEach(function (img) {
    function activate() { open(img.currentSrc || img.src, img.getAttribute("alt")); }
    // Make each image an accessible, keyboard-activatable button. EMA figures carry an
    // empty alt, so give the control an explicit label (role=button needs a name).
    img.setAttribute("role", "button");
    img.setAttribute("tabindex", "0");
    if (!img.getAttribute("aria-label")) img.setAttribute("aria-label", "Agrandir l'image");
    img.addEventListener("click", activate);
    img.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); activate(); }
    });
  });
})();

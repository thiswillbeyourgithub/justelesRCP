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
 * classes + CSSOM setters only. Loaded (deferred) by rcp.html; a no-op on pages with no
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

  var overlay = null;      // the current lightbox element, or null when closed
  var prevFocus = null;    // element to restore focus to on close
  var onKey = null;        // the active Escape handler (removed on close)

  function close() {
    if (!overlay) return;
    document.body.classList.remove("lb-open");
    if (onKey) document.removeEventListener("keydown", onKey, true);
    onKey = null;
    overlay.remove();
    overlay = null;
    if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) {} }
  }

  function open(src, alt) {
    if (overlay) close();
    prevFocus = document.activeElement;

    overlay = document.createElement("div");
    overlay.className = "lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Image agrandie");
    // A backdrop click (anywhere but the image / close button) dismisses.
    overlay.addEventListener("click", close);

    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "lightbox-close";
    closeBtn.setAttribute("aria-label", "Fermer l'image");
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", close);

    var big = document.createElement("img");
    big.className = "lightbox-img";
    big.src = src;
    big.alt = alt || "";
    // Clicks on the image itself must NOT close (so a reader can pinch-zoom / drag it);
    // only the backdrop, the close button, or Escape close.
    big.addEventListener("click", function (ev) { ev.stopPropagation(); });

    overlay.appendChild(closeBtn);
    overlay.appendChild(big);
    document.body.appendChild(overlay);
    document.body.classList.add("lb-open"); // freeze the page scroll behind the overlay

    onKey = function (ev) { if (ev.key === "Escape") { ev.preventDefault(); close(); } };
    document.addEventListener("keydown", onKey, true);
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

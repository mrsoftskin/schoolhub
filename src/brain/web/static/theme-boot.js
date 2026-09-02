/* Applies the saved (or OS) theme before first paint. Loaded synchronously in
   <head> because the CSP forbids inline scripts and app.js runs after the body
   renders, which would flash the light theme at every load in dark mode. */
"use strict";
(function () {
  // ?theme=light|dark previews a theme without persisting it (also how
  // headless screenshots pin a theme regardless of the OS setting).
  let forced = null;
  try { forced = new URLSearchParams(location.search).get("theme"); } catch { /* very old engine */ }
  let saved = null;
  try { saved = localStorage.getItem("cc-theme"); } catch { /* private mode */ }
  const pick = forced === "light" || forced === "dark" ? forced : saved;
  const dark = pick ? pick === "dark"
    : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();

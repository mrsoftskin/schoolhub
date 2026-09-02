/* Command Center frontend. Vanilla JS, no build step.
   All rendering of model output goes through DOMPurify before DOM insertion:
   markdown via the html profile, ```svg fences via the svg profile, ```html
   fences via the html profile. Script tags and on* attributes never survive.

   Layout system: the "authored page" anatomy (see style.css header). Content
   sits on the flat ground; new content enters as a labeled section or margin
   note, never as a box. */

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  collections: [],        // from /api/state
  models: [],
  defaultModel: null,
  model: null,
  user: { name: "", location: "", has_location: false },
  analytics: null,
  conversations: [],      // unfiltered list, grouped client-side in the rail
  chat: {
    collection: null,     // selected collection name or "all"
    conversationId: null,
    streaming: false,
    abort: null,          // AbortController for the in-flight answer
    pending: [],          // attached images awaiting send: {media_type, data, dataUrl}
  },
  cal: {
    mode: "month",        // "month" | "week"
    cursor: startOfDay(new Date()),
    showClasses: true,
  },
  weekload: [],
  sync: null,             // last /api/sync/status payload (null = none/unavailable)
  library: null,          // last /api/library payload (for the status popover)
  /* The index job is SERVER-GLOBAL - one _index_job behind one lock, shared
     by every browser tab - so this is a view of the server's run, not this
     tab's run. `mine` is the only thing that distinguishes them, and it
     exists so the copy never claims credit for a run another window started.
     `results` outlives the run: renderLibrary() wipes the whole ledger, and
     without this the per-row result text the user just read vanishes with
     the next rebuild. */
  index: {
    watching: false, starting: false, mine: false,
    scope: null,          // status.collection; null = all collections
    curCol: null,         // last [NAME] seen in a progress line
    phase: "start",       // start | scan | embed
    result: "", line: "", // terminal kind ("done" | "bad") and its sentence
    sawRunning: false, ticks: 0, fails: 0,
    embedDone: 0, embedTotal: 0, elapsed: 0,
    results: new Map(),   // collection -> per-row result text, this session
  },
  syncWatch: {
    watching: false, startedAt: 0, sawRunning: false, fails: 0,
    // run_id is the termination signal; baseRun is only the fallback for a
    // payload from a server that predates it.
    baseRunId: null, baseRun: null,
  },
};

/* ================================================================ utils */

function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function mondayOf(d) {
  const x = startOfDay(d);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function sameDay(a, b) { return isoDate(a) === isoDate(b); }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function fmtWhen(ev) {
  const d = new Date(ev.starts_at);
  const day = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  if (ev.all_day) return day;
  return `${day} ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}
function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
function fmtRelative(iso) {
  if (!iso) return "never";
  const then = new Date(iso);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs} h ago`;
  return then.toLocaleDateString();
}
function daysUntil(iso) {
  const d = startOfDay(new Date(iso));
  return Math.round((d - startOfDay(new Date())) / 86400000);
}
/* Course names come from ICS/CSV data and may not match a collection's
   casing; match the way the Discuss button does so colors never silently
   fall back to gray for the same event the popover happily links. */
function collectionByCourse(course) {
  const want = String(course).toUpperCase();
  return state.collections.find(c => c.name.toUpperCase() === want) || null;
}
function collectionColor(name) {
  const c = collectionByCourse(name);
  return c ? themedColor(c.color) : courseFallback();
}
/* Course hue as TEXT: light hues like amber are too weak on white, so light
   mode borrows the darker validated step; dark mode uses its own set. */
function courseInk(name) {
  const c = collectionByCourse(name);
  if (!c) return courseFallback();
  const hex = c.color;
  return isDark() ? themedColor(hex) : (DARK_COURSE[String(hex).toLowerCase()] || hex);
}
function basename(p) { return String(p).split(/[\\/]/).pop(); }

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* Count a numeral in from zero over ~450ms. Falls back to setting the text
   directly under reduced motion or when the surface is not entering. */
function animateCount(el, target, { animate = true, format = n => n.toLocaleString() } = {}) {
  if (!el) return;
  // Overlapping calls on the same node (double renders, sync refreshes)
  // must not fight over textContent: newest call wins.
  if (el._countRaf) cancelAnimationFrame(el._countRaf);
  el._countRaf = null;
  if (!animate || reducedMotion() || !Number.isFinite(target) || target <= 0) {
    el.textContent = format(target);
    return;
  }
  const dur = 450;
  const t0 = performance.now();
  const finalStr = String(format(target));
  // An intermediate must never render WIDER than the value it is counting to.
  // Mid-ramp values routinely carry a decimal the target does not ("72.7" on
  // the way to "80"), and one extra glyph was enough to wrap a figure onto a
  // second line in the wrap-enabled Grades strip - bouncing the whole
  // gradebook 53px down and back for most of the 450ms. Dropping the decimal
  // keeps the string monotonically non-widening, so nothing can reflow.
  const fit = v => {
    let s = String(format(v));
    if (s.length > finalStr.length) s = s.replace(/[.,]\d+$/, "");
    return s;
  };
  // Paint frame zero synchronously: waiting for the first rAF tick left the
  // figure strip showing labels with no numbers for one frame.
  el.textContent = fit(0);
  const step = now => {
    if (!el.isConnected) { el._countRaf = null; return; }
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 4);   // matches the CSS settle curve
    el.textContent = p < 1 ? fit(Math.round(target * eased)) : finalStr;
    el._countRaf = p < 1 ? requestAnimationFrame(step) : null;
  };
  el._countRaf = requestAnimationFrame(step);
}

/* Is a tab inside its entrance window? Entrance choreography keys off this
   so background refreshes and resizes never replay it. */
function isEntering(panelId) {
  const p = $(panelId);
  return !!p && p.classList.contains("entering");
}

/* Instrument strips draw in when their SEGMENTS are built, not when the
   empty container appears; the one-shot class is removed after the play so
   a display flip can never restart it. */
/* Arrival helper: one-shot settle for a datum that just ARRIVED (empty pane
   populated, a genuinely new row inserted). Strips its own class so display
   flips and rebuilds can never replay it. The LAW: never call this from a
   poll-driven repaint or a wholesale rebuild of unchanged data - that is how
   5-minute flicker gets built. */
function settle(el, { rise = false } = {}) {
  if (!el || reducedMotion()) return;
  const cls = rise ? "settle-rise" : "settle";
  el.classList.add(cls);
  const done = () => el.classList.remove(cls);
  el.addEventListener("animationend", done, { once: true });
  setTimeout(done, 600);
}

function drawStrip(el, entering) {
  if (!el || !entering || reducedMotion()) return;
  el.classList.add("grow-strip");
  setTimeout(() => el.classList.remove("grow-strip"), 700);
}

/* True while the user is typing somewhere: global single-key shortcuts must
   never fire into a composer. */
function isTypingContext() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/* ================================================================ theme */

/* Dark-surface steps for the config.toml course palette. NOT an automatic
   lightness flip: each step was re-derived and validated (lightness band,
   chroma, CVD separation, contrast) against the dark ground with the
   dataviz six-check validator, same as the light set was against white.
   Hues absent here pass on both surfaces unchanged. */
const DARK_COURSE = {
  "#eb6834": "#d95a26",
  "#eda100": "#c28500",
  "#e87ba4": "#d55c8d",
  "#4a3aa7": "#7263d6",
};

function isDark() { return document.documentElement.dataset.theme === "dark"; }

function themedColor(hex) {
  if (!isDark()) return hex;
  return DARK_COURSE[String(hex).toLowerCase()] || hex;
}

let _fallbackColor = null;
function courseFallback() {
  if (_fallbackColor === null) {
    _fallbackColor = getComputedStyle(document.documentElement)
      .getPropertyValue("--course-fallback").trim() || "#666e7e";
  }
  return _fallbackColor;
}

function rerenderActiveTab() {
  const active = $(".tab-panel.active");
  if (!active) return;
  // A theme repaint is not an arrival: make sure a still-open entrance
  // window cannot replay the choreography on the rebuilt content.
  active.classList.remove("entering");
  clearTimeout(active._enterTimer);
  if (active.id === "tab-today") loadToday();
  else if (active.id === "tab-analytics") loadAnalytics();
  else if (active.id === "tab-grades") {
    // A repaint is not a fetch: reuse the rendered data so open disclosures
    // and scroll state survive a theme flip.
    if (_lastGrades) renderGrades(_lastGrades); else loadGrades();
  }
  else if (active.id === "tab-calendar") renderCalendar();
  else if (active.id === "tab-socials") loadSocials();
  else if (active.id === "tab-library") loadLibrary();
  else if (active.id === "tab-chat") {
    renderRailTree();
    renderChatMasthead();
    // Repaint inline course hues in the open thread: message rules and
    // citation ticks carry per-theme hex values the theme flip must remap.
    $$("#chat-messages .msg-user").forEach(m =>
      m.style.setProperty("--turn-hue", chatHue()));
    $$("#chat-messages .ctick[data-col]").forEach(t => {
      t.style.background = collectionColor(t.dataset.col);
    });
  }
}

function initTheme() {
  const btn = $("#theme-toggle");
  const sun = $("#theme-icon-sun");
  const moon = $("#theme-icon-moon");
  // The icon shows what a click switches TO: moon in light, sun in dark.
  const sync = () => {
    sun.classList.toggle("hidden", !isDark());
    moon.classList.toggle("hidden", isDark());
  };
  const apply = (dark, persist) => {
    // Cross-fade the flip: existing chrome transitions its colors, then the
    // active tab repaints its inline course hues once the fade lands.
    const anim = !reducedMotion();
    if (anim) document.documentElement.classList.add("theme-anim");
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    if (persist) {
      try { localStorage.setItem("cc-theme", dark ? "dark" : "light"); } catch { /* private mode */ }
    }
    _fallbackColor = null;   // token changed with the theme
    sync();
    if (anim) {
      setTimeout(() => {
        // Rebuild WHILE the fade class is still on, then drop it a frame
        // later, so the inline-hue repaint lands inside the same fade
        // instead of popping after it.
        rerenderActiveTab();
        requestAnimationFrame(() =>
          setTimeout(() => document.documentElement.classList.remove("theme-anim"), 60));
      }, 300);
    } else {
      rerenderActiveTab();
    }
  };
  btn.addEventListener("click", () => apply(!isDark(), true));
  // Follow OS changes only while the user has not chosen explicitly.
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onOs = e => {
    let saved = null;
    try { saved = localStorage.getItem("cc-theme"); } catch { /* private mode */ }
    if (!saved) apply(e.matches, false);
  };
  if (mq.addEventListener) mq.addEventListener("change", onOs);
  sync();
}

/* ================================================== toasts + indicators */

function toast(message, { danger = false, duration = 4000 } = {}) {
  const box = $("#toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast" + (danger ? " toast-danger" : "");
  t.textContent = message;
  box.appendChild(t);
  setTimeout(() => {
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 300);
  }, duration);
}

/* Write only what changed. A poll tick at 1.2s against a server that emits
   a new line every ~2.6s has nothing to say most of the time, and a DOM
   write that changes nothing is still a DOM write. */
function setText(el, s) { if (el && el.textContent !== s) el.textContent = s; }

/* The app's one always-rendered announcement channel. A live region that is
   display:none at load, or inserted in the same task as its text, is not
   reliably announced - so transient status routes through this node instead
   of aria-live being sprinkled on whichever surface happens to show it.
   Clearing first and writing next frame forces a mutation, so the SAME
   sentence twice still announces. */
function announce(message) {
  const el = $("#a11y-status");
  if (!el || !message) return;
  el.textContent = "";
  requestAnimationFrame(() => { el.textContent = String(message); });
}

/* A quiet per-message copy control; getText defers so streaming callers can
   bind it before the final text exists. */
function copyBtn(getText) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "copy-btn";
  b.textContent = "Copy";
  b.title = "Copy this answer as Markdown";
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(getText());
      b.textContent = "Copied";
      setTimeout(() => { b.textContent = "Copy"; }, 1500);
    } catch {
      toast("Could not access the clipboard.", { danger: true });
    }
  });
  return b;
}

function thinkingEl(label = "Thinking") {
  const d = document.createElement("div");
  d.className = "thinking";
  const dots = document.createElement("span");
  dots.className = "thinking-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";
  const text = document.createElement("span");
  text.textContent = label;
  d.appendChild(dots);
  d.appendChild(text);
  return d;
}

function railKeyable(el) {
  el.tabIndex = 0;
  // A row that contains a real button must not claim role=button itself.
  if (!el.querySelector("button")) el.setAttribute("role", "button");
  el.addEventListener("keydown", e => {
    // Keys that originate on a descendant control belong to that control.
    if (e.target !== el) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.click();
    }
  });
}

/* ================================================================== api */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep statusText */ }
    // The status code, not the prose: the two index routes word the same
    // 404 differently, and 409 ("a run is already going") is a state to
    // adopt rather than an error to report.
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/* =========================================================== SSE client */

async function streamAsk(url, body, handlers, signal = null) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      let payload;
      try { payload = JSON.parse(data); } catch { continue; }
      if (handlers[event]) handlers[event](payload);
    }
  }
}

/* ===================================================== markdown + fences
   ```svg and ```html fences are extracted BEFORE marked runs, replaced with
   placeholder tokens, and rendered separately through DOMPurify profiles.
   That keeps sanitization per-block and survives marked version changes. */

const FENCE_RE = /```(svg|html)[ \t]*\n([\s\S]*?)```/g;

/* Tags DOMPurify keeps by default that this app must not render, on ANY path.
   <form> would post credentials off-origin; <style> is not scoped to its
   container and would restyle the whole app (hiding assist badges, faking
   dialogs); the rest are remote-content loaders. The server's CSP is the
   real backstop, but these must be blocked identically everywhere so the
   markdown path is no weaker than the fence path. */
const FORBID_TAGS_COMMON = [
  "script", "style", "form", "input", "button", "textarea", "select",
  "iframe", "object", "embed", "link", "base", "meta", "audio", "video",
];
const FORBID_ATTR_COMMON = ["srcset", "ping", "formaction", "background"];

function sanitizeHtml(dirty) {
  return DOMPurify.sanitize(dirty, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: FORBID_TAGS_COMMON,
    FORBID_ATTR: FORBID_ATTR_COMMON,
  });
}

function sanitizeSvg(dirty) {
  return DOMPurify.sanitize(dirty, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ["script", "style", "foreignObject", "image", "use", "a"],
    FORBID_ATTR: [...FORBID_ATTR_COMMON, "href", "xlink:href"],
  });
}

/* Rewrite only TOP-LEVEL ```svg / ```html fences. A fence nested inside a
   longer ```` ```` block is content the model is showing, not a diagram to
   render, and a placeholder there would leak the token into a code listing.
   Walking the fences in order is the only way to know which is which. */
function extractVisFences(md) {
  const lines = md.split("\n");
  const out = [];
  const blocks = [];
  let openMarker = null;   // the ``` run that opened the current block
  let capture = null;      // {lang, lines} while inside a top-level vis fence

  for (const line of lines) {
    const m = line.match(/^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$/);
    if (m) {
      const [, marker, lang] = m;
      if (openMarker === null) {
        if (lang === "svg" || lang === "html") {
          openMarker = marker;
          capture = { lang, lines: [] };
          continue;
        }
        openMarker = marker;
        out.push(line);
        continue;
      }
      // A closing fence must be at least as long as the one that opened it.
      if (marker[0] === openMarker[0] && marker.length >= openMarker.length && !lang) {
        if (capture) {
          blocks.push({ lang: capture.lang, src: capture.lines.join("\n") });
          out.push(`VISBLOCK${blocks.length - 1}ENDVIS`);
          capture = null;
        } else {
          out.push(line);
        }
        openMarker = null;
        continue;
      }
    }
    if (capture) capture.lines.push(line);
    else out.push(line);
  }
  // An unterminated vis fence is still streaming; keep its text out of the
  // markdown and let the caller show a pending note.
  const pending = capture ? capture.lang : null;
  return { md: out.join("\n"), blocks, pending };
}

/* If either rendering library is missing, show the answer as PLAIN TEXT.
   Never fall back to innerHTML: DOMPurify absent means nothing is sanitized,
   and model output is untrusted. (This is not hypothetical - a pinned CDN
   path started 404ing and took marked with it, which is why both libraries
   are now vendored locally.) */
function renderersReady() {
  return typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
}

function renderMarkdownInto(el, mdText, { streaming = false } = {}) {
  if (!renderersReady()) {
    el.textContent = mdText;
    if (!el.dataset.degraded) {
      el.dataset.degraded = "1";
      const n = document.createElement("div");
      n.className = "notice notice-warn";
      n.textContent = "Rendering libraries failed to load, so this answer is "
        + "shown as plain text. Reload the page; if it persists, check "
        + "static/vendor/.";
      el.parentNode && el.parentNode.insertBefore(n, el);
    }
    return;
  }
  const extracted = extractVisFences(mdText);
  const visBlocks = extracted.blocks;
  let md = extracted.md;
  if (extracted.pending) md += `\n\n*rendering ${extracted.pending}...*\n`;
  const rawHtml = marked.parse(md, { breaks: false, gfm: true });
  el.innerHTML = sanitizeHtml(rawHtml);

  // Swap placeholder tokens for rendered vis blocks.
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const targets = [];
  let node;
  while ((node = walker.nextNode())) {
    if (/VISBLOCK\d+ENDVIS/.test(node.nodeValue)) targets.push(node);
  }
  for (const textNode of targets) {
    const frag = document.createDocumentFragment();
    const parts = textNode.nodeValue.split(/VISBLOCK(\d+)ENDVIS/);
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) {
        if (parts[i]) frag.appendChild(document.createTextNode(parts[i]));
      } else {
        const block = visBlocks[Number(parts[i])];
        // No such block means the model wrote the token literally; show it
        // rather than deleting a piece of the answer.
        if (block) frag.appendChild(buildVisBlock(block.lang, block.src));
        else frag.appendChild(document.createTextNode(`VISBLOCK${parts[i]}ENDVIS`));
      }
    }
    // Placeholders sit inside a <p>; replace within it.
    textNode.parentNode.replaceChild(frag, textNode);
  }
}

function buildVisBlock(lang, source) {
  const wrap = document.createElement("div");
  wrap.className = "vis-block";

  const toolbar = document.createElement("div");
  toolbar.className = "vis-toolbar";
  toolbar.innerHTML = `<span>${lang === "svg" ? "diagram (svg)" : "widget (html)"}</span><span class="spacer" style="flex:1"></span>`;
  const srcBtn = document.createElement("button");
  srcBtn.className = "text-action quiet";
  srcBtn.textContent = "view source";
  const dlBtn = document.createElement("button");
  dlBtn.className = "text-action quiet";
  dlBtn.textContent = "download";
  toolbar.appendChild(srcBtn);
  toolbar.appendChild(dlBtn);
  toolbar.style.cssText = "display:flex;align-items:center;gap:.5rem;font-size:.74rem;color:var(--text-3);margin-bottom:4px";
  wrap.appendChild(toolbar);

  const content = document.createElement("div");
  content.className = "vis-content";
  content.style.cssText = "background:var(--bg-sunken);border-radius:8px;padding:.8rem";
  const clean = lang === "svg" ? sanitizeSvg(source) : sanitizeHtml(source);
  content.innerHTML = clean;
  wrap.appendChild(content);

  const srcPre = document.createElement("pre");
  srcPre.className = "vis-source hidden";
  const srcCode = document.createElement("code");
  srcCode.textContent = source;
  srcPre.appendChild(srcCode);
  wrap.appendChild(srcPre);

  srcBtn.addEventListener("click", () => {
    srcPre.classList.toggle("hidden");
    srcBtn.textContent = srcPre.classList.contains("hidden") ? "view source" : "hide source";
  });
  dlBtn.addEventListener("click", () => {
    // Download the SANITIZED markup, never the raw model output: the raw
    // string still contains whatever scripts/handlers were stripped for
    // display, and a downloaded .html runs them with no CSP at file://.
    const blob = new Blob([clean], { type: lang === "svg" ? "image/svg+xml" : "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = lang === "svg" ? "diagram.svg" : "widget.html";
    a.click();
    URL.revokeObjectURL(a.href);
  });
  return wrap;
}

/* Last two path segments: enough to tell week3/notes.pdf from week9/notes.pdf
   without pasting an absolute path into every footnote. Full path in title. */
function shortPath(p) {
  const parts = String(p).split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/");
}

/* Footnote line under an answer: hairline rule, citations as course-ticked
   entries, "answered by" + Copy right-aligned. Used by Chat and the palette. */
function buildFootnote({ citations = [], model = null, getText = null } = {}) {
  if (!citations.length && !model && !getText) return null;
  const foot = document.createElement("div");
  foot.className = "msg-foot";
  const cites = document.createElement("span");
  cites.className = "cites";
  citations.forEach((c, i) => {
    const s = document.createElement("span");
    s.className = "cite";
    s.title = `${c.source_path}\nscore ${c.score}`;
    const tick = document.createElement("span");
    tick.className = "ctick";
    tick.dataset.col = c.collection;
    tick.style.background = collectionColor(c.collection);
    s.appendChild(tick);
    s.appendChild(document.createTextNode(
      `${c.collection} · ${shortPath(c.source_path)} · ${c.locator}`));
    cites.appendChild(s);
    if (i < citations.length - 1) cites.appendChild(document.createTextNode("  ·  "));
  });
  foot.appendChild(cites);
  const meta = document.createElement("span");
  meta.className = "meta";
  if (model) {
    const m = document.createElement("span");
    m.textContent = `answered by ${model}`;
    meta.appendChild(m);
  }
  if (getText) meta.appendChild(copyBtn(getText));
  foot.appendChild(meta);
  return foot;
}

/* Server-supplied notices (explain-only + which collection caused it,
   truncation, unindexed collections). Rendered as text, never HTML. */
function renderNotices(container, notices, before = null) {
  for (const text of notices || []) {
    const n = document.createElement("div");
    n.className = "notice notice-warn";
    n.textContent = text;
    if (before) container.insertBefore(n, before);
    else container.appendChild(n);
  }
}

/* ======================================================== frame: tabs */

const TABS = ["today", "analytics", "grades", "calendar", "chat", "socials", "library"];

function positionTabIndicator({ instant = false } = {}) {
  const bar = $("#topbar");
  const active = $(".tab-btn.active");
  const ind = $("#tab-indicator");
  if (!bar || !active || !ind) return;
  if (instant) ind.style.transition = "none";
  const b = bar.getBoundingClientRect();
  const r = active.getBoundingClientRect();
  // transform-only: the CSS holds a 1px-wide bar at left:0; translateX
  // positions it and scaleX carries the width. Stale inline left/width from
  // the pre-transform code are cleared once.
  ind.style.left = "";
  ind.style.width = "";
  ind.style.transform = `translateX(${r.left - b.left}px) scaleX(${r.width})`;
  if (instant) requestAnimationFrame(() => { ind.style.transition = ""; });
}


/* ---------------------------------------------------------------- socials
   Deliberately its own tab and its own store. A happy hour is not
   coursework, and week_load selects deadlines by EXCLUSION
   (`WHERE kind != 'admin'`), so a social event on the school calendar would
   have quietly inflated the workload chart and the due counts.

   A happy hour is also not an EVENT: it is a standing weekly attribute of a
   venue. Research found that essentially nobody publishes them as calendar
   entries and the big aggregators omit the DAYS entirely, so this list is
   curated and every row carries when it was last checked. Anything whose
   days were never confirmed is shown, but never as "on today". */
/* ---------------------------------------------------------------- socials
   One view at a time. Stacking today's list, all 21 venues and 70 events in
   a single column meant ~97 rows of scroll to reach anything - so this is
   segmented (Today / All spots / What's on) with a text filter, and each row
   links out to the venue or the event page. */
const socState = {
  view: "today", data: null, events: null, chip: "",
  // One-shot guard: renderSocials re-runs on every filter keystroke inside
  // the 2s entering window; _counted stops replayed count-ups.
  _counted: false, _tick: null, _todayVenues: null,
};

/* Minutes-since-midnight now, and the temporal bucket a venue is in. The
   times are curated Charleston local; the client clock is the right clock. */
function socNowMin() {
  const n = new Date();
  return n.getHours() * 60 + n.getMinutes();
}
function socFmtMin(m) {
  const hh = Math.floor(m / 60) % 24, ap = hh >= 12 ? "PM" : "AM";
  return `${hh % 12 || 12}:${String(m % 60).padStart(2, "0")} ${ap}`;
}
/* The window as a sentence fragment for the row's temporal state. Built from
   numbers; the only server-text path goes through escapeHtml. */
function socWindow(h, state) {
  if (h.start_min == null) return escapeHtml(h.window);
  if (state === "live") return h.end_min == null ? "until late" : `until ${socFmtMin(h.end_min)}`;
  if (state === "done") return h.end_min == null ? "ended" : `ended ${socFmtMin(h.end_min)}`;
  if (state === "soon" || state === "upcoming") {
    if (h.end_min == null) return `from ${socFmtMin(h.start_min)}`;
    const st = socFmtMin(h.start_min), en = socFmtMin(h.end_min);
    const [sT, sAp] = st.split(" ");
    return sAp === en.split(" ")[1] ? `${sT}\u2013${en}` : `${st}\u2013${en}`;
  }
  return escapeHtml(h.window);   // stateless rows print the curated window
}
function socBucket(h, nowMin) {
  const st = h.start_min, en = h.end_min;
  if (st == null) return "upcoming";
  const end = en == null ? 1380 : en;
  if (nowMin >= st && nowMin < end) return "live";
  if (nowMin < st && st - nowMin <= 60) return "soon";
  if (en != null && nowMin >= en) return "done";
  return "upcoming";
}

function socLink(url, text, cls) {
  if (!url) return `<span class="${cls}">${escapeHtml(text)}</span>`;
  // rel=noreferrer: these are third-party sites and the app's own URL is
  // nobody else's business.
  return `<a class="${cls} soc-link" href="${escapeHtml(url)}" target="_blank"`
    + ` rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function socRow(h, { showDays = true, state = "", socI = null } = {}) {
  const row = document.createElement("div");
  row.className = "soc-row" + (h.confirmed ? "" : " is-unconfirmed")
    + (state ? ` is-${state}` : "");
  if (socI != null) row.dataset.socI = String(socI);
  if (state) row.dataset.state = state;
  const days = showDays && h.days
    ? `<span class="soc-days">${escapeHtml(h.days)}</span>` : "";
  // Type carries the temporal state - a word, never a dot.
  // Only the soon countdown survives as a tag - the section heads carry
  // "Pouring now" and "Ended" for everything else.
  let tag = "";
  if (state === "soon") tag = `<span class="soc-tag">in ${h.start_min - socNowMin()} min</span>`;
  // Prices step forward inside the deal line. escape-then-wrap is safe:
  // escapeHtml never emits $-digit sequences.
  const deals = h.deals
    ? escapeHtml(h.deals).replace(/\$\d+(?:\.\d{2})?/g, '<b class="soc-price">$&</b>') : "";
  row.innerHTML =
    `<span class="soc-ico">${socIconSvg(socGlyphFor(h.category))}</span>`
    + `<div class="soc-head">`
    + socLink(h.url, h.name, "soc-name")
    + days
    + tag
    + `<span class="soc-when tnum">${socWindow(h, state)}</span>`
    + `</div>`
    + (deals ? `<div class="soc-deals">${deals}</div>` : "");
  const meta = [h.address, h.checked ? `checked ${h.checked}` : "",
                h.confirmed ? "" : "days not confirmed", h.note || ""];
  row.title = [h.name, h.window, h.deals, ...meta.filter(Boolean)].filter(Boolean).join(" · ");
  return row;
}

function socEventRow(ev) {
  const row = document.createElement("div");
  row.className = "soc-ev";
  const t = ev.all_day ? "all day" : fmtTime(ev.starts_at);
  row.innerHTML =
    `<span class="soc-ico" title="${escapeHtml(ev.feed)}">${socIconSvg(socFeedGlyph(ev.feed))}</span>`
    + `<span class="soc-ev-time tnum">${escapeHtml(t)}</span>`
    + socLink(ev.url, ev.title, "soc-ev-title")
    + (ev.location
        ? `<span class="soc-ev-where">${escapeHtml(ev.location)}</span>` : "");
  row.title = [ev.title, ev.feed, ev.location].filter(Boolean).join(" · ");
  return row;
}

function socMatches(text) {
  const q = ($("#soc-search")?.value || "").trim().toLowerCase();
  return !q || text.toLowerCase().includes(q);
}

function renderSocials() {
  const box = $("#soc-list");
  const chips = $("#soc-chips");
  const count = $("#soc-count");
  if (!box) return;
  box.innerHTML = "";
  chips.innerHTML = "";
  const d = socState.data;
  if (!d) return;

  if (socState.view === "events") {
    const evs = socState.events;
    if (evs === null) {
      count.textContent = "loading…";
      box.innerHTML = '<div class="empty-line">Checking the campus calendars…</div>';
      return;
    }
    // Chips = the feeds themselves, so "just show me games" is one click.
    const feeds = [...new Set(evs.map(e => e.feed))];
    for (const f of ["All", ...feeds]) {
      const b = document.createElement("button");
      const on = (socState.chip || "All") === f;
      b.className = "soc-chip" + (on ? " active" : "");
      b.textContent = f;
      b.addEventListener("click", () => {
        socState.chip = f === "All" ? "" : f;
        renderSocials();
      });
      chips.appendChild(b);
    }
    if (socState.feedErrors && socState.feedErrors.length) {
      const warn = document.createElement("div");
      warn.className = "notice notice-warn";
      warn.textContent = `Some campus feeds failed: ${socState.feedErrors.join("; ")}`;
      box.appendChild(warn);
    }
    const shown = evs.filter(e =>
      (!socState.chip || e.feed === socState.chip)
      && socMatches(`${e.title} ${e.feed} ${e.location || ""}`));
    count.textContent = `${shown.length} event${shown.length === 1 ? "" : "s"}`;
    let lastDay = "";
    for (const ev of shown.slice(0, 80)) {
      const day = ev.starts_at.slice(0, 10);
      if (day !== lastDay) {
        lastDay = day;
        const head = document.createElement("div");
        head.className = "soc-day" + (day === isoDate(new Date()) ? " is-today" : "");
        head.textContent = new Date(day + "T00:00:00").toLocaleDateString(
          undefined, { weekday: "long", month: "short", day: "numeric" });
        box.appendChild(head);
      }
      box.appendChild(socEventRow(ev));
    }
    if (!shown.length) {
      // innerHTML here would wipe the feed-error notice above; and "nothing
      // matches that filter" is a lie when no filter is set and the feeds
      // are simply down or empty.
      const search = $("#soc-search");
      const hasFilter = !!(socState.chip || (search && search.value.trim()));
      const line = document.createElement("div");
      line.className = "empty-line";
      line.textContent = hasFilter
        ? "Nothing matches that filter."
        : (socState.feedErrors && socState.feedErrors.length
          ? "The campus feeds are unreachable right now."
          : "No upcoming events found.");
      box.appendChild(line);
    }
    return;
  }

  const todayView = socState.view === "today";
  let venues = todayView ? d.today : d.all;
  if (!todayView) {
    // Chips = category, the only split that matters when browsing them all.
    const cats = [...new Set(d.all.map(v => v.category))].sort();
    for (const c of ["All", ...cats]) {
      const b = document.createElement("button");
      const on = (socState.chip || "All") === c;
      b.className = "soc-chip" + (on ? " active" : "");
      b.textContent = c === "both" ? "bar + food" : c;
      b.addEventListener("click", () => {
        socState.chip = c === "All" ? "" : c;
        renderSocials();
      });
      chips.appendChild(b);
    }
    if (socState.chip) venues = venues.filter(v => v.category === socState.chip);
  }
  venues = venues.filter(v => socMatches(`${v.name} ${v.deals} ${v.address}`));
  if (todayView) {
    const nowMin = socNowMin();
    // Temporal order replaces alphabetical: what is pouring, then what is
    // next, then what already ended. Ties break alphabetically.
    const bucketOf = new Map(venues.map(v => [v, socBucket(v, nowMin)]));
    const rank = { live: 0, soon: 1, upcoming: 1, done: 2 };
    venues = [...venues].sort((a, b) => {
      const ra = rank[bucketOf.get(a)], rb = rank[bucketOf.get(b)];
      if (ra !== rb) return ra - rb;
      if (ra === 0) {
        const ea = a.end_min == null ? 9999 : a.end_min;
        const eb = b.end_min == null ? 9999 : b.end_min;
        if (ea !== eb) return ea - eb;
      } else if (ra === 1) {
        const sa = a.start_min == null ? 9999 : a.start_min;
        const sb = b.start_min == null ? 9999 : b.start_min;
        if (sa !== sb) return sa - sb;
      } else {
        const ea = a.end_min == null ? 0 : a.end_min;
        const eb = b.end_min == null ? 0 : b.end_min;
        if (ea !== eb) return eb - ea;
      }
      return (a.name || "").localeCompare(b.name || "");
    });
    socState._todayVenues = venues;


    // Three worded sections carry the time story; venues arrive rank-sorted
    // so buckets emerge contiguously and empty sections never render.
    // data-soc-i stays a GLOBAL index into socState._todayVenues, so the
    // minute tick is untouched.
    const SOC_SECS = { 0: ["Pouring now", " is-live"], 1: ["Later today", ""], 2: ["Ended", ""] };
    const counts = { 0: 0, 1: 0, 2: 0 };
    for (const v of venues) counts[rank[bucketOf.get(v)]]++;
    let grid = null, lastRank = -1;
    venues.forEach((h, vi) => {
      const r = rank[bucketOf.get(h)];
      if (r !== lastRank) {
        lastRank = r;
        const head = document.createElement("div");
        head.className = "soc-day soc-sec" + SOC_SECS[r][1];
        head.innerHTML = `${SOC_SECS[r][0]} <b class="tnum">${counts[r]}</b>`;
        box.appendChild(head);
        grid = document.createElement("div");
        grid.className = "soc-grid";
        box.appendChild(grid);
      }
      grid.appendChild(socRow(h, { showDays: false, state: bucketOf.get(h), socI: vi }));
    });
    if (socState._counted) {
      count.textContent = `${venues.length} on today`;
    } else {
      animateCount(count, venues.length, {
        animate: isEntering("#tab-socials"),
        format: n => `${n} on today`,
      });
      socState._counted = true;
    }
  } else {
    count.textContent = `${venues.length} of ${d.total} spots`;
    const grid = document.createElement("div");
    grid.className = "soc-grid";
    for (const h of venues) grid.appendChild(socRow(h, { showDays: true }));
    box.appendChild(grid);
  }

  if (!venues.length) {
    box.innerHTML = `<div class="empty-line">${
      todayView ? "No confirmed happy hours today." : "Nothing matches that filter."
    }</div>`;
  }
}

async function loadSocials() {
  if (!$("#soc-list")) return;
  if (!socState.data) {
    try {
      socState.data = await api("/api/socials");
    } catch (e) {
      $("#soc-list").innerHTML =
        '<div class="empty-line">Could not load the happy hour list.</div>';
      return;
    }
    $("#soc-note").textContent =
      "Hand-checked, not live: bars change hours without telling anyone. "
      + "Hover a row for its address and when it was last checked.";
    $("#soc-search")?.addEventListener("input", renderSocials);
    // The minute tick keeps the sections honest: rewrites countdowns in
    // place, and re-renders ONLY when a venue crosses a state boundary
    // (which moves it to its new section). Created once; never refetches.
    if (!socState._tick) {
      socState._tick = setInterval(() => {
        if (!$("#tab-socials").classList.contains("active")) return;
        if (socState.view !== "today" || !socState._todayVenues) return;
        const nowMin = socNowMin();
        let crossed = false;
        for (const row of $("#soc-list").querySelectorAll(".soc-row[data-soc-i]")) {
          const v = socState._todayVenues[Number(row.dataset.socI)];
          if (!v) continue;
          const bucket = socBucket(v, nowMin);
          if (bucket !== row.dataset.state) { crossed = true; break; }
          if (bucket === "soon") {
            const tag = row.querySelector(".soc-tag");
            if (tag) tag.textContent = `in ${v.start_min - nowMin} min`;
          }
        }
        if (crossed) renderSocials();
      }, 60000);
    }
    $$(".soc-seg-btn").forEach(b => b.addEventListener("click", () => {
      $$(".soc-seg-btn").forEach(x => {
        const on = x === b;
        x.classList.toggle("active", on);
        x.setAttribute("aria-selected", on ? "true" : "false");
      });
      socState.view = b.dataset.view;
      socState.chip = "";
      renderSocials();
      if (socState.view === "events" && socState.events === null) loadSocialEvents();
    }));
  }
  renderSocials();
}

async function loadSocialEvents() {
  try {
    const d = await api("/api/socials/events");
    socState.events = d.events || [];
    socState.feedErrors = d.feed_errors || [];
  } catch (e) {
    socState.events = [];
    socState.feedErrors = ["Could not reach the campus calendars."];
  }
  if (socState.view === "events") renderSocials();
}

let _updCheckAt = 0;
let _updCheckRes = null;

function showTab(name, { updateHash = true } = {}) {
  if (!TABS.includes(name)) name = "today";
  $$(".tab-btn").forEach(b => {
    const active = b.dataset.tab === name;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  $$(".tab-panel").forEach(p => {
    const active = p.id === `tab-${name}`;
    p.classList.toggle("active", active);
    // Entrance choreography plays ONCE per tab per session: a settle is an
    // arrival, and re-playing it on every revisit reads as decoration.
    // Revisits get only the panel-in fade.
    if (active && !p.dataset.entered) {
      p.dataset.entered = "1";
      p.classList.add("entering");
      clearTimeout(p._enterTimer);
      p._enterTimer = setTimeout(() => p.classList.remove("entering"), 2000);
    }
  });
  document.title = `${name[0].toUpperCase()}${name.slice(1)} · Command Center`;
  if (typeof hideTip === "function") hideTip();
  hideBarPop();
  positionTabIndicator();
  if (updateHash && location.hash.slice(1) !== name) location.hash = name;
  if (name === "today") loadToday();
  if (name === "analytics") loadAnalytics();
  if (name === "grades") loadGrades();
  if (name === "calendar") renderCalendar();
  if (name === "chat") {
    renderRailTree();
    renderChatMasthead();
    if (!state.chat.conversationId) renderChatEmpty();
  }
  if (name === "socials") loadSocials();
  if (name === "library") loadLibrary();
}

function initTabs() {
  $$(".tab-btn").forEach(btn =>
    btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  $("#tabs").addEventListener("keydown", e => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const i = TABS.indexOf($(".tab-btn.active").dataset.tab);
    const next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
    showTab(next);
    $(`.tab-btn[data-tab="${next}"]`).focus();
  });
  /* The skip link must NOT navigate. Writing #main-content to the hash
     fires the handler below, whose showTab() falls back to "today" for any
     name not in TABS - so a plain anchor would throw the user off whatever
     tab they were on. Focus the ACTIVE panel instead: it is the scroll
     container and the region role=tabpanel names, so the reading cursor and
     the next Tab both land inside the visible content. */
  const skip = $(".skip-link");
  if (skip) skip.addEventListener("click", e => {
    e.preventDefault();
    const panel = $(".tab-panel.active");
    if (panel) panel.focus();
  });
  // showTab writes the hash itself; without this guard every programmatic
  // switch would bounce back through here and run the whole loader twice.
  window.addEventListener("hashchange", () => {
    const name = location.hash.slice(1);
    // A hash that names no tab is not a tab switch. Without this, ANY
    // foreign fragment - a skip target, a pasted deep link, a restored
    // session - silently forces Today via showTab's TABS fallback.
    if (!TABS.includes(name)) return;
    const active = $(".tab-btn.active");
    if (active && active.dataset.tab === name) return;
    showTab(name, { updateHash: false });
  });
  window.addEventListener("resize", () => positionTabIndicator({ instant: true }));
  if (document.fonts && document.fonts.ready) {
    // Inter swaps in after first paint and changes label widths.
    document.fonts.ready.then(() => positionTabIndicator({ instant: true }));
  }
}

/* =================================================== frame: bar popover */

let _barPopFor = null;
let _barPopReturnFocus = null;
function showBarPop(anchor, build) {
  const pop = $("#bar-pop");
  if (_barPopFor === anchor && !pop.classList.contains("hidden")) { hideBarPop(); return; }
  _barPopFor = anchor;
  pop.innerHTML = "";
  build(pop);
  pop.classList.remove("hidden");
  const a = anchor.getBoundingClientRect();
  const w = pop.getBoundingClientRect().width;
  pop.style.top = `${a.bottom + 8}px`;
  pop.style.left = `${Math.max(12, Math.min(a.left, window.innerWidth - w - 12))}px`;
  pop.tabIndex = -1;
  _barPopReturnFocus = document.activeElement;
  pop.focus({ preventScroll: true });
}
function hideBarPop() {
  const pop = $("#bar-pop");
  if (pop.classList.contains("hidden")) { _barPopFor = null; return; }
  pop.classList.add("hidden");
  _barPopFor = null;
  if (_barPopReturnFocus && _barPopReturnFocus.isConnected
      && (pop.contains(document.activeElement) || document.activeElement === document.body)) {
    _barPopReturnFocus.focus({ preventScroll: true });
  }
  _barPopReturnFocus = null;
}

/* ================================================= frame: status + sync */

function pipEl() { return $("#brand .pip"); }

function refreshPip() {
  const pip = pipEl();
  pip.classList.remove("warn", "bad");
  if (state.backendProblem) { pip.classList.add("bad"); return; }
  const lib = state.library;
  if (lib && lib.collections) {
    const dayMs = 24 * 3600 * 1000;
    const trouble = lib.collections.some(c =>
      c.failures.length || c.missing_roots.length || !c.last_indexed
      || (Date.now() - new Date(c.last_indexed).getTime()) > 7 * dayMs);
    if (trouble) pip.classList.add("warn");
  }
  // Library tab warning dot mirrors the same signal.
  const dot = $("#library-dot");
  if (dot) dot.classList.toggle("hidden", !pip.classList.contains("warn") || !!state.backendProblem);
}

async function openStatusPop() {
  // Fetch fresh on demand; the pip is a health instrument, not a cache view.
  try { state.library = await api("/api/library"); } catch { /* keep last */ }
  refreshPip();
  showBarPop($("#pip-btn"), pop => {
    const head = document.createElement("div");
    head.className = "pop-head";
    head.textContent = "System";
    pop.appendChild(head);
    const row = (k, v, cls = "") => {
      const r = document.createElement("div");
      r.className = "pop-row";
      r.innerHTML = `<span>${escapeHtml(k)}</span><b class="${cls}">${escapeHtml(v)}</b>`;
      pop.appendChild(r);
    };
    if (state.backendProblem) {
      const p = document.createElement("div");
      p.className = "bad";
      p.textContent = state.backendProblem;
      p.style.marginBottom = "8px";
      pop.appendChild(p);
    }
    const lib = state.library;
    if (lib) {
      const cs = lib.collections || [];
      const chunks = cs.reduce((n, c) => n + c.chunk_count, 0);
      const fails = cs.reduce((n, c) => n + c.failures.length, 0);
      const missing = cs.reduce((n, c) => n + c.missing_roots.length, 0);
      const oldest = cs.reduce((m, c) => c.last_indexed
        ? Math.max(m, Date.now() - new Date(c.last_indexed).getTime()) : m, 0);
      row("Indexed", `${chunks.toLocaleString()} chunks`);
      row("Oldest index pass", oldest ? fmtRelative(new Date(Date.now() - oldest).toISOString()) : "never");
      if (fails) row("Parse failures", String(fails), "warn"); else row("Parse failures", "0");
      if (missing) row("Missing roots", String(missing), "bad");
      if (lib.calendar_status) {
        row("Calendar", `${lib.calendar_status.total_stored ?? lib.calendar_status.total_imported} events · ${fmtRelative(lib.calendar_status.imported_at)}`);
      }
    }
    if (state.sync) row("Last sync check", "found "
      + `${(state.sync.total_new || 0) + (state.sync.total_moved || 0)} changes`);
    const acts = document.createElement("div");
    acts.className = "pop-actions";
    const open = document.createElement("button");
    open.className = "text-action";
    open.textContent = "Open Library";
    open.addEventListener("click", () => { hideBarPop(); showTab("library"); });
    acts.appendChild(open);
    pop.appendChild(acts);
  });
}

/* Background site-sync results surface chrome-side: a pill in the bar and a
   quiet sentence on Today. Quiet unless the last check found something. */
async function pollSync() {
  try { state.sync = await api("/api/sync/status"); }
  catch { state.sync = null; }   // old server build or poller off: silence
  renderSyncChrome();
  renderSyncLine();
  renderUpdateLine();
}

function syncTotals() {
  const s = state.sync || {};
  return (s.total_new || 0) + (s.total_moved || 0) + (s.total_announcements || 0);
}

function renderSyncChrome() {
  const pill = $("#sync-pill");
  const s = state.sync;
  const total = syncTotals();
  if (!s || !total) { pill.classList.add("hidden"); return; }
  const bits = [];
  if (s.total_new) bits.push(`${s.total_new} new`);
  if (s.total_moved) bits.push(`${s.total_moved} moved`);
  if (s.total_announcements) bits.push(`${s.total_announcements} news`);
  pill.textContent = bits.join(" · ");
  pill.classList.remove("hidden");
}

/* Say what actually happened. The old toast read "Sync applied. Calendar
   updated." whatever the result - including when it wrote nothing - and an
   applied item is frequently an admin row dated weeks out, which The plan
   does not render (exam/project/quiz only). So a working apply and a no-op
   looked identical, which is exactly how this got reported as broken. */
function describeApplied(res) {
  const n = res.applied || 0;
  const items = res.items || [];
  if (!n) {
    return items.length
      ? "Nothing new to add - those items are already on your calendar."
      : "Nothing to apply.";
  }
  const first = items[0];
  const noun = n === 1 ? "item" : "items";
  let msg = `Added ${n} ${noun} to your calendar`;
  if (first) {
    const when = first.date
      ? new Date(first.date + "T00:00:00")
          .toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : "";
    msg += `: ${first.course} ${stripAside(first.title)}${when ? ", " + when : ""}`;
    if (n > 1) msg += ` (+${n - 1} more)`;
  }
  // Point at where it can actually be seen, or the user checks Today, sees
  // no change, and concludes it failed.
  const invisible = items.every(i => !PLAN_KINDS.has(i.kind));
  if (invisible) msg += " - see the Calendar tab";
  return msg;
}

function syncApply(onDone) {
  return async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const res = await api("/api/sync/apply", { method: "POST" });
      toast(describeApplied(res), { duration: 7000 });
      state.weekload = [];
      getAnalytics({ fresh: true });
      await pollSync();
      // Repaint whatever tab is actually open - hardcoding loadToday left
      // the Calendar showing pre-apply events after an apply.
      rerenderActiveTab();
      if (onDone) onDone();
    } catch (e) {
      toast(`Apply failed: ${e.message}`, { danger: true });
      btn.disabled = false;
    }
  };
}

/* ---------------------------------------------------------- sync check
   /api/sync/run blocked for the whole scrape - 14.2s measured, and the
   connectors allow 25s per request, so a slow campus server pushes it past
   a minute with no way to tell slow from hung. /api/sync/start returns at
   once and we watch instead.

   The checking state lives HERE, never on a button: renderSyncLine()
   rebuilds all of its own children on every call, so both call sites ask
   for the affordance rather than owning one. */

const SYNC_TICK_MS = 1500;
const SYNC_GRACE_MS = 6000;
const SYNC_CEILING_MS = 180000;   // a sync poll can trigger the throttled
                                  // content pull, which downloads AND
                                  // indexes; 14.2s is the floor, not a cap
let _syncTimer = null;

function syncClockText() {
  const s = Math.round((Date.now() - state.syncWatch.startedAt) / 1000);
  return s >= 5 ? `${s}s` : "";   // a check that lands in a second must not
}                                 // flash a number at anyone

function syncActionEl(label) {
  if (state.syncWatch.watching) {
    const wrap = thinkingEl("Checking your sites");
    const clock = document.createElement("span");
    clock.className = "sync-clock tnum";
    clock.textContent = syncClockText();
    wrap.appendChild(clock);
    return wrap;
  }
  const b = document.createElement("button");
  b.className = "text-action quiet";
  b.textContent = label;
  b.addEventListener("click", () => startSyncCheck());
  return b;
}

function scheduleSyncTick(delay) {
  clearTimeout(_syncTimer);
  _syncTimer = setTimeout(syncTick, delay != null ? delay
    : (document.hidden ? 5000 : SYNC_TICK_MS));
}

function stopSyncWatch() {
  clearTimeout(_syncTimer);
  _syncTimer = null;
  state.syncWatch.watching = false;
}

function syncOutcomeText() {
  const s = state.sync || {};
  if (s.ok === false && s.error) {
    return "The check finished with errors - your saved logins may need refreshing.";
  }
  const n = (s.total_new || 0) + (s.total_moved || 0);
  if (!n) return "No new deadlines.";
  return `Sync found ${plural(s.total_new || 0, "new assignment")}`
    + ((s.total_moved || 0) ? `, ${s.total_moved} moved.` : ".");
}

async function startSyncCheck() {
  const w = state.syncWatch;
  if (w.watching) return;
  Object.assign(w, {
    watching: true, startedAt: Date.now(),
    baseRunId: state.sync ? state.sync.run_id : null,
    baseRun: state.sync ? state.sync.last_run : null,
    sawRunning: false, fails: 0,
  });
  renderSyncLine();                  // switch to the checking affordance now
  let st;
  try { st = await api("/api/sync/start", { method: "POST" }); }
  catch (e) {
    stopSyncWatch();
    renderSyncLine();
    toast(`Sync check failed to start: ${e.message}`, { danger: true });
    return;
  }
  /* The start response is NOT a result. sync_start spawns the thread and
     reads poller.status from the MAIN thread, while _poll_once_locked sets
     running=True from the worker - so running:false here is the norm, and
     reading it as "finished" would end the check about 30ms after it began
     and then silently change the numbers 14 seconds later. */
  if (st && st.running) w.sawRunning = true;
  scheduleSyncTick();
}

async function syncTick() {
  _syncTimer = null;
  const w = state.syncWatch;
  if (!w.watching) return;
  let s;
  try { s = await api("/api/sync/status"); }
  catch {
    // Deliberately does NOT null state.sync: one failed tick must not blank
    // the sentence and take the only Check-now control away with it.
    if (++w.fails >= 4) return endSyncCheck("lost");
    scheduleSyncTick();
    return;
  }
  if (!w.watching) return;
  w.fails = 0;
  state.sync = s;
  if (s.running) w.sawRunning = true;
  /* run_id, NOT last_run. last_run is a wall clock: two polls inside one
     tick can carry the same value, and a clock that steps backwards makes it
     decrease - so it can both miss a finish and invent one. run_id only ever
     increases and is bumped on the exception path too, so a poll that threw
     still ends the watch instead of hanging on exactly the run that most
     needs reporting. last_run remains the fallback for an older payload.
     A missing baseline is captured rather than compared, so a watch that
     started with no prior status cannot terminate on its own first tick. */
  if (w.baseRunId == null && s.run_id != null) w.baseRunId = s.run_id;
  if (w.baseRun == null && s.last_run != null) w.baseRun = s.last_run;
  const finished = (s.run_id != null && w.baseRunId != null)
    ? s.run_id !== w.baseRunId
    : (s.last_run != null && w.baseRun != null && s.last_run !== w.baseRun);
  const age = Date.now() - w.startedAt;
  if (finished || (w.sawRunning && !s.running)) return endSyncCheck("done");
  if (!w.sawRunning && age > SYNC_GRACE_MS) return endSyncCheck("nostart");
  if (age > SYNC_CEILING_MS) return endSyncCheck("slow");
  renderSyncChrome();
  setText($("#sync-line .sync-clock"), syncClockText());   // one text node
  scheduleSyncTick();
}

function endSyncCheck(kind) {
  stopSyncWatch();
  renderSyncChrome();
  renderSyncLine();
  if (kind === "done") {
    renderUpdateLine();
    announce(syncOutcomeText());
    // "checked just now" is the whole payoff, and it is only visible on
    // Today. From anywhere else the outcome has to travel.
    if (!$("#tab-today").classList.contains("active")) toast(syncOutcomeText());
  } else if (kind === "slow") {
    // No cancel endpoint exists, so we never claim to have stopped anything.
    toast("Still checking in the background - this page updates when it lands.");
  } else if (kind === "nostart") {
    toast("The check did not start. One may already be running.");
  } else if (kind === "lost") {
    toast("Lost contact with Command Center.", { danger: true });
  }
}

function openSyncPop() {
  const s = state.sync;
  if (!s) return;
  showBarPop($("#sync-pill"), pop => {
    const head = document.createElement("div");
    head.className = "pop-head";
    head.textContent = "Sync";
    pop.appendChild(head);
    for (const i of (s.new_items || []).slice(0, 6)) {
      const r = document.createElement("div");
      r.className = "pop-row";
      r.innerHTML = `<span>${escapeHtml(`${i.course}: ${i.title}`)}</span><b>${escapeHtml(i.date || "")}</b>`;
      pop.appendChild(r);
    }
    if ((s.new_items || []).length > 6) {
      const more = document.createElement("div");
      more.className = "pop-row";
      more.textContent = `+${s.new_items.length - 6} more`;
      pop.appendChild(more);
    }
    const acts = document.createElement("div");
    acts.className = "pop-actions";
    if ((s.total_new || 0) + (s.total_moved || 0) > 0) {
      const apply = document.createElement("button");
      apply.className = "text-action";
      apply.textContent = "Apply to calendar";
      apply.addEventListener("click", syncApply(hideBarPop));
      acts.appendChild(apply);
    }
    const re = document.createElement("button");
    re.className = "text-action quiet";
    re.textContent = "Re-check";
    // Close now, not in fourteen seconds: the Today line and the pill are
    // where the check reports, and both outlive this float.
    re.addEventListener("click", () => { hideBarPop(); startSyncCheck(); });
    acts.appendChild(re);
    pop.appendChild(acts);
  });
}

/* ===================================================== frame: palette */

let _paletteReturnFocus = null;

function paletteOpen() {
  _paletteReturnFocus = document.activeElement;
  $("#palette-scrim").classList.remove("hidden");
  $("#palette-answer").classList.add("hidden");
  $("#palette-answer").innerHTML = "";
  const input = $("#palette-input");
  input.value = "";
  renderPaletteJump("");
  renderPaletteModel();
  input.focus();
}

function paletteClose() {
  $("#palette-scrim").classList.add("hidden");
  if (_paletteReturnFocus && _paletteReturnFocus.focus) {
    _paletteReturnFocus.focus();
  }
  _paletteReturnFocus = null;
}

function paletteIsOpen() { return !$("#palette-scrim").classList.contains("hidden"); }

function renderPaletteModel() {
  const btns = [$("#palette-model"), $("#model-btn")].filter(Boolean);
  for (const b of btns) {
    b.textContent = state.model || "";
    b.title = "Answering model - click to switch";
  }
}

function cycleModel() {
  if (!state.models.length) return;
  const i = state.models.indexOf(state.model);
  state.model = state.models[(i + 1) % state.models.length];
  try { localStorage.setItem("cc-model", state.model); } catch { /* private mode */ }
  renderPaletteModel();
  toast(`Model: ${state.model}`, { duration: 1800 });
}

function renderPaletteJump(query) {
  const body = $("#palette-body");
  body.innerHTML = "";
  const q = query.trim().toLowerCase();
  const row = (label, hint, hue, onGo) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "palette-row";
    if (hue) {
      const d = document.createElement("span");
      d.className = "dot";
      d.style.background = hue;
      b.appendChild(d);
    }
    const g = document.createElement("span");
    g.className = "grow";
    g.textContent = label;
    b.appendChild(g);
    if (hint) {
      const k = document.createElement("span");
      k.className = "keycap";
      k.setAttribute("aria-hidden", "true");
      k.textContent = hint;
      b.appendChild(k);
    }
    b.addEventListener("click", onGo);
    body.appendChild(b);
    return b;
  };
  const label = t => {
    const l = document.createElement("div");
    l.className = "palette-label";
    l.textContent = t;
    body.appendChild(l);
  };
  const tabs = TABS.filter(t => !q || t.startsWith(q));
  if (tabs.length) {
    label("Jump to");
    tabs.forEach(t => row(t[0].toUpperCase() + t.slice(1), String(TABS.indexOf(t) + 1), null,
      () => { paletteClose(); showTab(t); }));
  }
  const cols = state.collections.filter(c => !q || c.name.toLowerCase().includes(q));
  if (cols.length) {
    label("Chat with");
    cols.forEach(c => row(c.name, null, themedColor(c.color), async () => {
      paletteClose();
      showTab("chat");
      await selectChatCollection(c.name);
      $("#composer-input").focus();
    }));
  }
}

async function paletteAsk(question) {
  const body = $("#palette-body");
  const out = $("#palette-answer");
  body.innerHTML = "";
  out.classList.remove("hidden");
  out.innerHTML = "";
  let thinking = thinkingEl();
  out.appendChild(thinking);
  const clearThinking = () => { if (thinking) { thinking.remove(); thinking = null; } };
  const answer = document.createElement("div");
  out.appendChild(answer);
  let text = "";
  let citations = [];
  let model = null;
  try {
    await streamAsk("/api/ask", { question, model: state.model }, {
      meta(m) { citations = m.citations; model = m.model; renderNotices(out, m.notices, answer); },
      delta(d) { clearThinking(); text += d.text; renderMarkdownInto(answer, text, { streaming: true }); },
      refusal(r) {
        clearThinking();
        const fix = storeSyncNotice(r);
        if (fix) { out.appendChild(fix); return; }
        const n = document.createElement("div");
        n.className = "notice notice-danger";
        n.textContent = r.collections && r.collections.length
          ? `Blocked by collection(s): ${r.collections.join(", ")}. ${r.detail}` : r.detail;
        out.appendChild(n);
      },
      error(err) {
        clearThinking();
        const n = document.createElement("div");
        n.className = "notice notice-danger";
        n.textContent = `Error: ${err.detail}`;
        out.appendChild(n);
      },
      done() {
        clearThinking();
        renderMarkdownInto(answer, text);
        const foot = buildFootnote({ citations, model, getText: () => text });
        if (foot) out.appendChild(foot);
      },
    });
  } catch (err) {
    clearThinking();
    const n = document.createElement("div");
    n.className = "notice notice-danger";
    n.textContent = `Request failed: ${err.message}`;
    out.appendChild(n);
  }
}

/* The Today tab's Everything ask. Same endpoint and same stream handling as
   the palette, rendered inline on the page instead of in a float. The topbar
   trigger it replaced was absolutely positioned, which is what let a seventh
   tab slide underneath it. */
async function askAnything(question) {
  const out = $("#ask-answer");
  out.classList.remove("hidden");
  out.innerHTML = "";
  settle(out, { rise: true });   // each ask re-renders: replay IS feedback
  let thinking = thinkingEl();
  out.appendChild(thinking);
  const clearThinking = () => { if (thinking) { thinking.remove(); thinking = null; } };
  const answer = document.createElement("div");
  out.appendChild(answer);
  let text = "";
  let citations = [];
  let model = null;
  const fail = msg => {
    clearThinking();
    const n = document.createElement("div");
    n.className = "notice notice-danger";
    n.textContent = msg;
    out.appendChild(n);
  };
  try {
    await streamAsk("/api/ask", { question, model: state.model }, {
      meta(m) { citations = m.citations; model = m.model; renderNotices(out, m.notices, answer); },
      delta(d) { clearThinking(); text += d.text; renderMarkdownInto(answer, text, { streaming: true }); },
      refusal(r) {
        const fix = storeSyncNotice(r);
        if (fix) { clearThinking(); out.appendChild(fix); return; }
        fail(r.collections && r.collections.length
          ? `Blocked by collection(s): ${r.collections.join(", ")}. ${r.detail}` : r.detail);
      },
      error(err) { fail(`Error: ${err.detail}`); },
      done() {
        clearThinking();
        renderMarkdownInto(answer, text);
        const foot = buildFootnote({ citations, model, getText: () => text });
        if (foot) out.appendChild(foot);
        // The hand-off the palette never had: carry this exchange into a real
        // Everything conversation instead of losing it when the page changes.
        const more = document.createElement("button");
        more.type = "button";
        more.className = "text-action";
        more.textContent = "Continue in Chat →";
        more.addEventListener("click", async () => {
          if (blockedByStream()) return;
          showTab("chat");
          await selectChatCollection("all");
          const input = $("#composer-input");
          input.value = question;
          input.focus();
        });
        out.appendChild(more);
      },
    });
  } catch (err) {
    fail(`Request failed: ${err.message}`);
  }
}

function initAskAnything() {
  const form = $("#ask-anything");
  if (!form) return;
  const input = $("#ask-input");
  const send = $("#ask-send");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    send.disabled = true;
    input.blur();
    try { await askAnything(q); } finally { send.disabled = false; }
  });
  // Esc clears the answer without wiping what was typed.
  input.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      const out = $("#ask-answer");
      if (!out.classList.contains("hidden")) {
        out.classList.add("hidden");
        out.innerHTML = "";
        e.stopPropagation();
      }
    }
  });
}

function initPalette() {
  $("#palette-scrim").addEventListener("click", e => {
    if (e.target === $("#palette-scrim")) paletteClose();
  });
  const input = $("#palette-input");
  input.addEventListener("input", () => {
    $("#palette-answer").classList.add("hidden");
    renderPaletteJump(input.value);
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      e.preventDefault();
      const q = input.value.trim();
      if (q) paletteAsk(q);
    }
  });
  $("#palette-model").addEventListener("click", cycleModel);

  document.addEventListener("keydown", e => {
    // Esc closes float layers in priority order.
    if (e.key === "Escape") {
      if (paletteIsOpen()) { paletteClose(); return; }
      if (!$("#bar-pop").classList.contains("hidden")) { hideBarPop(); return; }
      hidePopover();
      return;
    }
    if (isTypingContext()) return;
    if (e.key === "/" || (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey))) {
      e.preventDefault();
      paletteOpen();
      return;
    }
    // Derived from TABS so a new tab can never silently fall off the map
    // (adding Grades as tab 3 left Library unreachable behind a dead "6").
    if (e.key >= "1" && e.key <= String(TABS.length)
        && !e.ctrlKey && !e.metaKey && !e.altKey) {
      showTab(TABS[Number(e.key) - 1]);
    }
  });
  document.addEventListener("click", e => {
    const pop = $("#bar-pop");
    if (!pop.classList.contains("hidden") && !pop.contains(e.target)
        && !e.target.closest("#pip-btn") && !e.target.closest("#sync-pill")) {
      hideBarPop();
    }
  });
}

/* ================================================================ today */

const WX_PATHS = {
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.2 5.2l1.7 1.7M17.1 17.1l1.7 1.7M18.8 5.2l-1.7 1.7M6.9 17.1l-1.7 1.7"/>',
  moon: '<path d="M20 14.1A8.1 8.1 0 0 1 9.9 4a8.1 8.1 0 1 0 10.1 10.1z"/>',
  cloud: '<path d="M7 18h9.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 8.7 4.5 4.5 0 0 0 7 18z"/>',
  sunCloud: '<path d="M15.2 6.8a3.2 3.2 0 0 1 2.8-1.6M18 2.2v1.5M22.1 6.3h-1.5M20.9 3.4l-1.1 1.1"/><path d="M7 19h8.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 9.7 4.5 4.5 0 0 0 7 19z"/>',
  fog: '<path d="M7 13h9.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 3.7 4.5 4.5 0 0 0 7 13z"/><path d="M5.5 17h13M7.5 20.5h9"/>',
  rain: '<path d="M7 15h9.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 5.7 4.5 4.5 0 0 0 7 15z"/><path d="M8.5 18l-1 3M12.5 18l-1 3M16.5 18l-1 3"/>',
  snow: '<path d="M7 15h9.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 5.7 4.5 4.5 0 0 0 7 15z"/><path d="M8 18.5h.01M12 20h.01M16 18.5h.01M10 21.5h.01M14 21.5h.01"/>',
  storm: '<path d="M7 14h9.5a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 6.4 4.7 4.5 4.5 0 0 0 7 14z"/><path d="M12.5 14.5 10 18.5h3l-2 4"/>',
};

function wxIconKind(code, isDay) {
  if (code === 0) return isDay ? "sun" : "moon";
  if (code === 1 || code === 2) return isDay ? "sunCloud" : "cloud";
  if (code === 3) return "cloud";
  if (code === 45 || code === 48) return "fog";
  if (code >= 95) return "storm";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 51) return "rain";
  return "cloud";
}

function wxIconSvg(kind) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"`
    + ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${WX_PATHS[kind]}</svg>`;
}

/* Socials glyphs, drawn in the weather icons' hand (same viewBox, stroke and
   caps). Identity is always shape PLUS the adjacent name - never hue-only. */
const SOC_PATHS = {
  // category glyphs (venue rows, 20px render)
  bar: '<path d="M4.5 4h15l-7.5 8.5z"/><path d="M12 12.5V20"/><path d="M8.5 20h7"/>',
  restaurant: '<path d="M6.5 3v5.5a2 2 0 0 0 4 0V3"/><path d="M8.5 3v18"/><path d="M17.5 3a4.5 4.5 0 0 0-3 4.5V12c0 .8.7 1.5 1.5 1.5h1.5z"/><path d="M17.5 13.5V21"/>',
  both: '<path d="M3 5h9l-4.5 5z"/><path d="M7.5 10v9"/><path d="M5 19h5"/><path d="M16.5 4v4.5a2 2 0 0 0 4 0V4"/><path d="M18.5 4v16"/>',
  // feed glyphs (event rows, 16px render)
  campus: '<path d="M5.5 21V3.5"/><path d="M5.5 4.5c2-1.3 4-1.3 6 0s4 1.3 6 0v8c-2 1.3-4 1.3-6 0s-4-1.3-6 0z"/>',
  orgs: '<circle cx="9.5" cy="7.5" r="3"/><path d="M4 20c0-3.3 2.4-5.5 5.5-5.5S15 16.7 15 20"/><path d="M16 4.9a3 3 0 0 1 0 5.2M17.5 14.9c2 .8 3.2 2.6 3.2 5.1"/>',
  games: '<path d="M8 4h8v5.5a4 4 0 0 1-8 0z"/><path d="M8 6H5v1.5A3 3 0 0 0 8 10.5M16 6h3v1.5a3 3 0 0 1-3 3"/><path d="M12 13.5v7M8.5 20.5h7"/>',
};
function socIconSvg(kind) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"`
    + ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SOC_PATHS[kind]}</svg>`;
}
function socGlyphFor(category) {
  return category === "restaurant" ? "restaurant" : category === "both" ? "both" : "bar";
}
function socFeedGlyph(feed) {
  if (/cougar|game|sport/i.test(feed || "")) return "games";
  if (/org/i.test(feed || "")) return "orgs";
  return "campus";
}

function renderDateline() {
  $("#today-date").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
  });
}

let wxSeq = 0;
async function loadWeather() {
  const seq = ++wxSeq;
  const box = $("#today-wx");
  if (!state.user.has_location) { box.innerHTML = ""; return; }
  let w;
  try { w = await api("/api/weather"); }
  catch { box.innerHTML = ""; return; }   // the dateline simply ends at the date
  if (seq !== wxSeq) return;              // a newer load won
  if (!w.ok) { box.innerHTML = ""; box.title = w.error || ""; return; }
  // Rain changes what he wears walking to class; the H/L pair never did.
  const bits = [`${escapeHtml(w.description)}, ${Math.round(w.temperature)}°`];
  if (w.precip_chance != null) bits.push(`${w.precip_chance}% rain`);
  const wxWasEmpty = !box.childElementCount;
  box.innerHTML = `${wxIconSvg(wxIconKind(w.code, w.is_day))}<span>${bits.join(" · ")}</span>`;
  if (wxWasEmpty) settle(box);
}

/* One shared, deduplicated /api/analytics: Today and Analytics both need it. */
let _analyticsPromise = null;
let _analyticsAt = 0;
function getAnalytics({ fresh = false } = {}) {
  const now = Date.now();
  if (!fresh && _analyticsPromise && now - _analyticsAt < 15000) return _analyticsPromise;
  _analyticsAt = now;
  _analyticsPromise = api("/api/analytics").then(a => (state.analytics = a));
  _analyticsPromise.catch(() => { _analyticsPromise = null; });
  return _analyticsPromise;
}

function renderSemesterRule(an) {
  const fill = $("#semester-fill");
  const note = $("#semester-note");
  if (an && an.semester && an.semester.pct_elapsed != null) {
    const p = Math.max(0, Math.min(100, an.semester.pct_elapsed));
    // scaleX (compositor-friendly) transitions from its current value (0 on
    // first paint) so the rule draws itself in; double-rAF guarantees the
    // zero state actually painted first.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      fill.style.transform = `scaleX(${p / 100})`;
    }));
    // The fill bar already draws pct_elapsed; printing it said the same
    // thing twice.
    note.innerHTML = `<b class="tnum"></b> days left`;
    animateCount(note.querySelector("b"), an.semester.days_remaining,
      { animate: isEntering("#tab-today") });
  } else {
    fill.style.transform = "scaleX(0)";
    note.textContent = "";
  }
}

/* The Today sync sentence mirrors the bar pill, inline where the eye lands. */
/* ---------------------------------------------------------------- updates
   A friend never opens a terminal, so "a newer build exists" has to arrive
   in the app. Downloading is one click; the install itself happens at the
   NEXT launch, because the app is running from the folder being replaced
   (see brain/updates.py). Nothing here ever contacts the network unless the
   copy was configured with an update_url. */
async function renderUpdateLine() {
  const line = $("#update-line");
  if (!line) return;
  const wasEmpty = !line.childElementCount;
  line.innerHTML = "";
  queueMicrotask(() => { if (wasEmpty && line.childElementCount) settle(line); });
  let st;
  try {
    st = await api("/api/update");
  } catch { return; }
  if (!st || !st.configured) return;          // updates not set up for this copy

  if (st.pending) {
    const msg = document.createElement("span");
    msg.className = "ready";
    msg.textContent = `Version ${st.pending.version} is ready - it installs `
      + `the next time you open Command Center.`;
    line.appendChild(msg);
    return;
  }

  // Check quietly; say nothing at all when up to date. A banner that says
  // "you are current" every day is a banner people stop reading.
  // Cached 15 minutes: the banner polls every 5, and 288 upstream checks a
  // day per open tab is abuse of someone else's server.
  let res;
  if (_updCheckRes !== null && Date.now() - _updCheckAt < 15 * 60 * 1000) {
    res = _updCheckRes;
  } else {
    try {
      res = await api("/api/update/check", { method: "POST" });
      _updCheckRes = res;
      _updCheckAt = Date.now();
    } catch { return; }
  }
  if (!res || !res.available) return;

  const msg = document.createElement("span");
  msg.textContent = `Version ${res.available.version} is available `
    + `(you have ${res.current}).`;
  line.appendChild(msg);
  const get = document.createElement("button");
  get.className = "text-action";
  get.textContent = "Download it";
  get.addEventListener("click", async () => {
    get.disabled = true;
    get.textContent = "Downloading...";
    try {
      const done = await api("/api/update/download", { method: "POST" });
      line.innerHTML = "";
      const ok = document.createElement("span");
      ok.className = "ready";
      ok.textContent = `Version ${done.version} downloaded - quit and reopen `
        + `Command Center to finish.`;
      line.appendChild(ok);
    } catch (e) {
      get.disabled = false;
      get.textContent = "Download it";
      toast(`Update failed: ${e.message}`, { danger: true });
    }
  });
  line.appendChild(get);
}

function renderSyncLine() {
  const line = $("#sync-line");
  if (!line) return;
  // Arrival law: breathe in only when a notice APPEARS; the 5-minute poll
  // repainting an existing notice must not blink.
  const wasEmpty = !line.childElementCount;
  line.innerHTML = "";
  const settleIfNew = () => { if (wasEmpty && line.childElementCount) settle(line); };
  queueMicrotask(settleIfNew);
  const s = state.sync;
  if (!s) return;
  // Health FIRST. A sync where every site failed used to render exactly like
  // a healthy one with nothing new - both simply showed no line - so a dead
  // sync was invisible and there was nothing to click to find out.
  /* A site that was never connected has no saved login to refresh, so it
     must not be swept into the "your logins expired" sentence. The backend
     added `configured` for exactly this distinction; a payload from before
     it existed has no such field, and those sites stay in the broken group
     (the old behavior) rather than being silently excused. */
  const down = (s.sites || []).filter(x => !x.ok);
  const failed = down.filter(x => x.configured !== false);
  const unset = down.filter(x => x.configured === false);
  const connected = (s.sites || []).filter(x => x.configured !== false);
  if (!syncTotals()) {
    const quiet = document.createElement("span");
    if (connected.length && failed.length === connected.length) {
      quiet.className = "warn";
      quiet.textContent = "Deadline sync is not working - your saved logins "
        + "need refreshing.";
    } else if (failed.length) {
      quiet.className = "warn";
      quiet.textContent = `No new deadlines. ${failed.length} of `
        + `${connected.length} sites could not be checked `
        + `(${failed.map(f => f.site).join(", ")}).`;
    } else if (unset.length && !connected.length) {
      // Nothing is broken - nothing is set up yet. Different problem,
      // different sentence, and the fix is Settings, not a re-login.
      quiet.className = "quiet";
      quiet.textContent = `No deadline sites connected yet `
        + `(${unset.map(f => f.site).join(", ")}).`;
    } else {
      quiet.className = "quiet";
      quiet.textContent = "No new deadlines"
        + (s.last_run ? ` · checked ${fmtRelative(new Date(s.last_run * 1000).toISOString())}` : "");
    }
    line.appendChild(quiet);
    // Always reachable: the moment you most want to re-check is when it just
    // told you there is nothing.
    line.appendChild(syncActionEl("Check now"));
    return;
  }
  const parts = [];
  if (s.total_new) parts.push(plural(s.total_new, "new assignment"));
  if (s.total_moved) parts.push(`${s.total_moved} moved`);
  if (s.total_announcements) parts.push(plural(s.total_announcements, "announcement"));
  const items = (s.new_items || []).slice(0, 2)
    .map(i => `${i.course} ${i.title}${i.date ? ` (${i.date})` : ""}`);
  const extra = (s.new_items || []).length > 2 ? `, +${s.new_items.length - 2} more` : "";
  const text = document.createElement("span");
  text.textContent = `Sync found ${parts.join(", ")}`
    + (items.length ? `: ${items.join("; ")}${extra}.` : ".");
  line.appendChild(text);
  if ((s.total_new || 0) + (s.total_moved || 0) > 0) {
    const apply = document.createElement("button");
    apply.className = "text-action";
    apply.textContent = "Apply to calendar";
    apply.addEventListener("click", syncApply());
    line.appendChild(apply);
  }
  // A failing site has to be visible even when there IS other news, or it
  // stays hidden for as long as anything else has something to say.
  if (failed.length) {
    const warn = document.createElement("span");
    warn.className = "warn";
    // Measured against the CONNECTED sites: "all of them are down" must not
    // be diluted by sites the student never set up.
    warn.textContent = failed.length === connected.length
      ? "Sync is not working - your saved logins need refreshing."
      : `${failed.map(f => f.site).join(", ")} could not be checked.`;
    line.appendChild(warn);
  }
  line.appendChild(syncActionEl("Re-check"));
}

/* ---------------------------------------------------------------- today
   One greeting, one list. The tab used to print the next deadline three
   times (a sentence, a "Next up" row, and a "The plan" row) and count the
   same workload five ways, which is why it read as noise. Now the only
   figure it prints is the length of the array it just rendered, so it
   cannot contradict itself. */

const PLAN_WINDOW = 7;      // days rendered as rows
const PLAN_FORECAST = 28;   // days fetched, so the tail can look ahead
const PLAN_DAY_CAP = 6;
const PLAN_KINDS = new Set(["exam", "project", "quiz"]);   // = calendar.py

function greetingPart(h) {
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function renderGreeting() {
  const el = $("#today-hello");
  if (!el) return;
  const name = state.user.name ? `, ${state.user.name}` : "";
  // No trailing period: this is a masthead, not a sentence.
  el.textContent = `${greetingPart(new Date().getHours())}${name}`;
}

/* Trailing provenance brackets ("[self-paced target, no real due date]")
   are shelf notes, not titles; the popover still shows the full string. */
function stripAside(t) { return t.replace(/\s*\[[^\]]*\]\s*$/, "").trim() || t; }

/* The same shape app.py parses server-side, ported so the estimate survives
   losing /api/plan. Run on the RAW title, before stripAside. */
function estFromTitle(t) {
  const m = t.match(/est\s+((?:\d+h\s*)?\d+m)|\((\d+m)\)/i);
  return m ? (m[1] || m[2]) : "";
}

/* An all-day deadline stays current all day - the rule app.py's plan
   endpoint already uses. */
function planIsPast(ev) {
  return ev.all_day
    ? ev.starts_at.slice(0, 10) < isoDate(new Date())
    : new Date(ev.starts_at) < new Date();
}

/* Mark an obligation finished. Identity travels as course+title+date, never
   the event id: ids are sha1(...|starts_at), so retiming a deadline mints a
   new one and a tick keyed to it would silently vanish. */
async function toggleDone(ev, done) {
  const res = await fetch("/api/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      course: ev.course, title: ev.title, done,
      date: (ev.starts_at || "").slice(0, 10),
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  ev.done = done;
}

function planRow(ev, isNext) {
  const row = document.createElement("div");
  row.className = "pl-row" + (isNext ? " is-next" : "")
    + (!isNext && planIsPast(ev) ? " is-past" : "")
    + (ev.done ? " is-done" : "");
  const ink = courseInk(ev.course);
  const time = ev.all_day ? "all day" : fmtTime(ev.starts_at);
  const est = estFromTitle(ev.title);
  const note = ev.done ? "done"
    : (isNext ? "Next" : (planIsPast(ev) ? "past" : est));
  row.innerHTML =
    `<button type="button" class="pl-check" aria-pressed="${ev.done ? "true" : "false"}"></button>`
    + `<span class="pl-tick" style="background:${collectionColor(ev.course)}"></span>`
    + `<span class="pl-code" style="color:${ink}">${escapeHtml(ev.course)}</span>`
    + `<span class="pl-title">`
    + (ev.kind === "exam" ? `<span class="ex" style="color:${ink}">EXAM</span>` : "")
    // The text needs its own element: text-overflow never applies to a
    // flex container own anonymous text, so a long title hard-clips.
    + `<span class="t">${escapeHtml(stripAside(ev.title))}</span></span>`
    + `<span class="pl-note">${escapeHtml(note)}</span>`
    + `<span class="pl-time">${escapeHtml(time)}</span>`;
  // The deleted standfirst's prose survives here: screen readers keep the
  // sentence, eyes get the facts in columns.
  row.setAttribute("aria-label",
    `${stripAside(ev.title)}, ${ev.course}, ${ev.kind}, due ${time}`
    + (ev.done ? ", done" : ""));
  if (isNext) row.setAttribute("aria-current", "true");
  railKeyable(row);
  row.addEventListener("click", e => {
    const pt = popoverPoint(e);
    showPopover(ev, pt.x, pt.y);
  });

  const check = row.querySelector(".pl-check");
  check.title = ev.done ? "Mark as not done" : "Mark as done";
  check.setAttribute("aria-label", check.title);
  check.addEventListener("click", async e => {
    e.stopPropagation();          // ticking must not also open the popover
    const next = !ev.done;
    check.disabled = true;
    try {
      await toggleDone(ev, next);
    } catch (err) {
      check.disabled = false;
      toast("Could not save that. Is the app still running?");
      return;
    }
    check.disabled = false;
    // Repaint this row in place rather than re-rendering the plan, so an
    // expanded "+N more" and the scroll position survive a tick.
    const fresh = planRow(ev, isNext && !next);
    row.replaceWith(fresh);
    settle(fresh);
    // Done work sinks to the end of its day, where it stays visible and one
    // click from undo - it is never removed while the user is looking at it.
    // FLIP the sink so the row travels instead of teleporting: read both
    // rects before the single transform write, transform-only, interruptible.
    if (next) {
      const body = fresh.parentElement;
      if (body) {
        const before = fresh.getBoundingClientRect();
        body.appendChild(fresh);
        if (!reducedMotion()) {
          const after = fresh.getBoundingClientRect();
          const dy = before.top - after.top;
          if (dy) {
            fresh.style.transform = `translateY(${dy}px)`;
            fresh.style.transition = "none";
            requestAnimationFrame(() => {
              fresh.style.transition = "transform .3s var(--ease-out)";
              fresh.style.transform = "";
              setTimeout(() => { fresh.style.transition = ""; }, 350);
            });
          }
        }
      }
    }
  });
  return row;
}

function renderPlan(events, an) {
  const list = $("#plan-list");
  const count = $("#plan-count");
  const tail = $("#plan-tail");
  list.innerHTML = "";
  const isWork = ev => PLAN_KINDS.has(ev.kind);
  // Group on the event's own date string. bucketByDay spreads a multi-day
  // event across every day it covers - right for a calendar grid, wrong for
  // a deadline list, where a thing comes due once.
  const byKey = {};
  for (const ev of events) {
    if (!isWork(ev)) continue;
    const k = ev.starts_at.slice(0, 10);
    (byKey[k] = byKey[k] || []).push(ev);
  }
  const start = startOfDay(new Date());
  const groups = [];
  for (let i = 0; i < PLAN_WINDOW; i++) {
    const day = addDays(start, i);
    const rows = (byKey[isoDate(day)] || [])
      .sort((x, y) => x.starts_at.localeCompare(y.starts_at));
    groups.push({ day, i, rows });
  }
  const total = groups.reduce((n, g) => n + g.rows.length, 0);
  // The count IS the list: it can never disagree with what is rendered, and
  // it names the end of its own window rather than implying one.
  const endLabel = addDays(start, PLAN_WINDOW - 1)
    .toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (total) {
    count.innerHTML = `<b class="tnum"></b> due through ${escapeHtml(endLabel)}`;
    animateCount(count.querySelector("b"), total, { animate: isEntering("#tab-today") });
  } else {
    count.textContent = "";
  }

  // Picked across every row first: scoping this to the visible slice let a
  // capped day of past items push the marker onto the following day.
  let nextEv = null;
  for (const g of groups) {
    nextEv = g.rows.find(ev => !planIsPast(ev));
    if (nextEv) break;
  }
  for (const g of groups) {
    // Today's group always renders, so "nothing due today" is stated.
    if (g.i !== 0 && !g.rows.length) continue;
    const box = document.createElement("div");
    box.className = "pl-day" + (g.i === 0 ? " is-today" : "");
    const rail = document.createElement("div");
    rail.className = "pl-rail";
    const rel = g.i === 0 ? "Today" : g.i === 1 ? "Tomorrow" : "";
    rail.innerHTML =
      `<div class="dow">${g.day.toLocaleDateString(undefined, { weekday: "short" })}</div>`
      + `<div class="dnum tnum">${g.day.getDate()}</div>`
      + (rel ? `<div class="rel">${rel}</div>` : "");
    box.appendChild(rail);
    const body = document.createElement("div");
    if (!g.rows.length) {
      body.innerHTML = `<div class="pl-empty">Nothing due today</div>`;
    }
    let shown = g.rows.slice(0, PLAN_DAY_CAP);
    // The next action is never the thing hidden behind "+N more".
    if (nextEv && g.rows.includes(nextEv) && !shown.includes(nextEv)) {
      shown = [nextEv].concat(shown.slice(0, PLAN_DAY_CAP - 1));
    }
    for (const ev of shown) body.appendChild(planRow(ev, ev === nextEv));
    if (g.rows.length > shown.length) {
      const rest = g.rows.filter(ev => !shown.includes(ev));
      const more = document.createElement("button");
      more.type = "button";
      more.className = "text-action quiet pl-more";
      more.textContent = `+${rest.length} more`;
      more.addEventListener("click", () => {
        for (const ev of rest) body.insertBefore(planRow(ev, ev === nextEv), more);
        more.remove();
      });
      body.appendChild(more);
    }
    box.appendChild(body);
    list.appendChild(box);
  }

  if (!total) {
    const line = document.createElement("div");
    line.className = "empty-line";
    const noCollections = an && an.totals && an.totals.collections_total === 0;
    if (noCollections) {
      line.textContent = "Add collections and run a calendar import to get started.";
    } else {
      // Only claim 28 quiet days when days 7-27 really are empty.
      line.textContent = events.some(isWork)
        ? "Nothing due this week." : "Nothing due in the next 28 days.";
    }
    list.appendChild(line);
  }

  // Tail: the one surviving look past the window, carrying what the heat
  // ledger and the totals line used to say, in a line instead of a grid.
  const later = events.filter(ev => isWork(ev) && daysUntil(ev.starts_at) >= PLAN_WINDOW);
  const nextWeek = later.filter(ev => daysUntil(ev.starts_at) < PLAN_WINDOW * 2).length;
  const exam = later.find(ev => ev.kind === "exam");
  const bits = [];
  if (nextWeek) {
    const a1 = addDays(start, PLAN_WINDOW)
      .toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const a2 = addDays(start, PLAN_WINDOW * 2 - 1)
      .toLocaleDateString(undefined, { month: "short", day: "numeric" });
    bits.push(`<b>${nextWeek}</b> due ${escapeHtml(a1)}–${escapeHtml(a2)}`);
  }
  if (exam) {
    const when = new Date(exam.starts_at)
      .toLocaleDateString(undefined, { month: "short", day: "numeric" });
    bits.push(`<span class="code" style="color:${courseInk(exam.course)}">`
      + `${escapeHtml(exam.course)}</span> exam ${escapeHtml(when)}`);
  }
  tail.classList.toggle("hidden", !bits.length);
  tail.innerHTML = bits.length
    ? `<span class="overline">Later</span><span class="val">${bits.join(" · ")}</span>`
    : "";
}

/* Only the newest loadToday may paint. */
let todayRenderSeq = 0;

async function loadToday() {
  const seq = ++todayRenderSeq;
  renderDateline();
  renderGreeting();
  loadWeather();
  renderSyncLine();

  const start = startOfDay(new Date());
  const [e, a] = await Promise.allSettled([
    fetchEvents(start, addDays(start, PLAN_FORECAST)),
    getAnalytics(),
  ]);
  if (seq !== todayRenderSeq) return;
  const an = a.status === "fulfilled" ? a.value : null;
  renderSemesterRule(an);
  if (e.status === "rejected") {
    $("#plan-count").textContent = "";
    $("#plan-tail").classList.add("hidden");
    $("#plan-list").innerHTML =
      `<div class="notice notice-danger">Failed to load: ${escapeHtml(e.reason.message)}</div>`;
    announce("Your plan failed to load: " + e.reason.message);
    return;
  }
  renderPlan(e.value, an);

  // Bar signal: a dot while something is still to do today. It now clears as
  // the day is consumed instead of staying lit until midnight.
  const todayKey = isoDate(start);
  const dueToday = e.value.some(ev => PLAN_KINDS.has(ev.kind)
    && ev.starts_at.slice(0, 10) === todayKey && !planIsPast(ev) && !ev.done);
  $("#today-dot").classList.toggle("hidden", !dueToday);
  positionTabIndicator();
}

/* A laptop reopened at 6:05 PM must not still say "Good afternoon" in
   60px type. */
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && $("#tab-today").classList.contains("active")) {
    renderGreeting();
  }
});

/* ============================================================ analytics */

function bandEmpty(id, msg) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

async function loadAnalytics() {
  const errBox = $("#analytics-error");
  // #an-lead replaced #an-masthead; keep-last-data-on-failure keys off it.
  const hasContent = $("#course-ledger").childElementCount > 0;
  let a;
  try { a = await getAnalytics(); }
  catch (e) {
    if (hasContent) {
      errBox.innerHTML = `<div class="notice notice-warn">Refresh failed (${escapeHtml(e.message)}). Showing the last loaded data.</div>`;
    } else {
      errBox.innerHTML = `<div class="notice notice-danger">Failed to load analytics: ${escapeHtml(e.message)}</div>`;
      announce("Failed to load analytics: " + e.message);
    }
    return;
  }
  errBox.innerHTML = "";
  // A throw inside the render used to leave the tab blank and silent, which
  // is indistinguishable from "it never loaded". The most common cause is a
  // stale tab: markup and script drift apart across a deploy, an id comes
  // back null, and the render aborts halfway. Say so, and name the fix.
  try {
    renderAnalytics(a);
  } catch (err) {
    errBox.innerHTML = `<div class="notice notice-danger">`
      + `Analytics failed to render: ${escapeHtml(err.message)}. `
      + `If the app was just updated, reload the page (Ctrl+Shift+R).</div>`;
    throw err;   // still reaches the console for a real diagnosis
  }
}

/* Grades tab: cached gradebooks from GET /api/grades (never network);
   POST /api/grades/refresh fetches live. */
let _gradesInFlight = false;

function gradesScore(v) {
  // 8.0 -> "8", null -> "?", so a legacy cache never renders literal "null".
  // Capped at 2 decimals: D2L's weighted fields carry full float precision
  // (FINC313 reported 7.462136364), and an 11-digit numerator overflowed the
  // fixed 72px fraction column and collided with the percent beside it.
  // Trailing zeros are trimmed so 7.50 still reads "7.5".
  if (v === null || v === undefined) return "?";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n === Math.trunc(n)) return String(Math.trunc(n));
  return String(Number(n.toFixed(2)));
}

async function loadGrades(refresh = false) {
  const body = $("#grades-body");
  const meta = $("#grades-meta");
  if (!body) return;
  if (_gradesInFlight) return;         // no concurrent double-scrapes
  _gradesInFlight = true;
  const hadContent = body.querySelector(".gr-band") !== null;
  if (refresh) {
    body.classList.add("gr-loading");
    body.setAttribute("aria-busy", "true");
  } else if (!hadContent) {
    // First visit: skeleton bands, never a blank pane. These use .gr-skel,
    // NOT .gr-band: the panel still carries .entering while the fetch lands,
    // so a placeholder wearing the animated class played the whole entrance
    // and then the real bands replaced it and played it a second time.
    body.innerHTML = Array.from({ length: 3 }, () =>
      `<div class="gr-skel"><div class="skeleton" style="height:16px;width:45%"></div>
       <div class="skeleton" style="height:12px;width:70%;margin-top:10px"></div></div>`).join("");
  }
  let g;
  try {
    g = refresh
      ? await api("/api/grades/refresh", { method: "POST" })
      : await api("/api/grades");
  } catch (e) {
    // Never destroy a rendered gradebook to show an error (house grammar:
    // keep last data + warn). Only an empty pane gets the empty state.
    if (hadContent) {
      const warn = document.createElement("div");
      warn.className = "notice notice-warn";
      warn.textContent = `Refresh failed (${e.message}). Showing the last loaded grades.`;
      body.insertBefore(warn, body.firstChild);
    } else {
      body.innerHTML = `<div class="empty-line">Grades unavailable: ${escapeHtml(e.message)}</div>`;
      meta.textContent = "";
    }
    return;
  } finally {
    body.classList.remove("gr-loading");
    body.removeAttribute("aria-busy");
    _gradesInFlight = false;
  }
  renderGrades(g);
}

function gradesMetaLine(g) {
  const meta = $("#grades-meta");
  meta.innerHTML = "";
  const ageH = g.fetched_at ? (Date.now() / 1000 - g.fetched_at) / 3600 : null;
  if (ageH !== null) {
    meta.appendChild(document.createTextNode(
      ageH < 1 ? "fetched under an hour ago" : `fetched ${Math.round(ageH)}h ago`));
    meta.appendChild(document.createTextNode(" · "));
  }
  // The refresh action is ALWAYS reachable; only the age text is conditional.
  const btn = document.createElement("button");
  btn.className = "text-action";
  btn.textContent = g.needs_refresh ? "fetch grades" : "refresh";
  btn.addEventListener("click", () => loadGrades(true));
  meta.appendChild(btn);
}

/* ---- The Instrument: pure arithmetic that becomes marks ---- */

function computeCourseStats(c) {
  const all = c.items || [];
  const counted = all.filter(i => !i.excluded);
  const graded = counted.filter(i => i.graded);
  // Rail dots: one per graded, counted item with a real denominator.
  const dots = [];
  all.forEach((i, idx) => {
    if (!i.graded || i.excluded) return;
    const outOf = Number(i.out_of);
    if (!outOf) return;
    const pct = 100 * (Number(i.score) || 0) / outOf;
    dots.push({ idx, item: i, pct, weightKey: Number(i.max_points) || outOf });
  });
  // Weight-sized dots: diameter by tercile of the item's stake in-course.
  const keys = [...new Set(dots.map(d => d.weightKey))].sort((a, b) => a - b);
  for (const d of dots) {
    if (keys.length <= 1) { d.size = 5.5; continue; }
    const pos = keys.indexOf(d.weightKey) / (keys.length - 1);
    d.size = pos < 1 / 3 ? 4 : pos < 2 / 3 ? 5.5 : 7;
  }
  const pcts = dots.map(d => d.pct).sort((a, b) => a - b);
  let median = null;
  if (pcts.length) {
    const mid = pcts.length >> 1;
    median = pcts.length % 2 ? pcts[mid] : (pcts[mid - 1] + pcts[mid]) / 2;
  }
  // Banked / lost / open, raw points only: mixing D2L's weighted fields with
  // sum-to-100 weight percentages adds apples to oranges (verified against
  // the live cache), so the honest suffix flags raw math on weighted books.
  let banked = 0, lost = 0, open = 0;
  for (const i of graded) {
    banked += Number(i.score) || 0;
    if (!i.bonus && i.out_of != null) {
      lost += Math.max((Number(i.out_of) || 0) - (Number(i.score) || 0), 0);
    }
  }
  for (const i of counted) {
    if (!i.graded && i.max_points != null) open += Number(i.max_points) || 0;
  }
  return { dots, pcts, median, banked, lost, open };
}

/* The 0-100 axis, printed once; every band's rail sits in the same grid
   column via the shared --gr-cols spine, so axis and marks cannot drift. */
function buildGradesScale(show) {
  const scale = $("#gr-scale");
  if (!scale) return;
  scale.innerHTML = "";
  scale.classList.toggle("hidden", !show);
  if (!show) return;
  const rail = document.createElement("div");
  rail.className = "gr-scale-rail";
  for (const n of [0, 25]) {
    const pip = document.createElement("i");
    pip.className = "pip";
    pip.style.left = `${n}%`;
    rail.appendChild(pip);
  }
  for (const n of [50, 60, 70, 80, 90, 100]) {
    const lab = document.createElement("span");
    lab.textContent = n;
    lab.style.left = `${n}%`;
    if (n === 50 || n === 80 || n === 100) lab.classList.add("keep");
    rail.appendChild(lab);
    const tick = document.createElement("i");
    tick.style.left = `${n}%`;
    rail.appendChild(tick);
  }
  const cap = document.createElement("div");
  cap.className = "gr-scale-cap";
  cap.textContent = "Score field";
  scale.appendChild(rail);
  scale.appendChild(cap);
}

function buildGradesRail(c, stats, hue, entering) {
  const rail = document.createElement("div");
  rail.className = "gr-rail";
  rail.tabIndex = 0;
  rail.setAttribute("role", "img");
  const s = c.summary || {};
  if (stats.dots.length) {
    const min = Math.round(stats.pcts[0]);
    const max = Math.round(stats.pcts[stats.pcts.length - 1]);
    rail.setAttribute("aria-label",
      `${c.course}: ${plural(stats.dots.length, "graded mark")}, ${min}% to ${max}%`
      + (stats.median != null ? `, median ${Math.round(stats.median)}%` : "")
      + (s.current_pct != null ? `, course at ${s.current_pct}%` : ""));
  } else {
    rail.setAttribute("aria-label", `${c.course}: no graded items yet`);
  }
  rail.style.setProperty("--rail-hue", hue);
  // The printed axis continues through every band: pips at the same
  // percents #gr-scale labels, sharing the --gr-cols register.
  for (const p of [50, 60, 70, 80, 90, 100]) {
    const pip = document.createElement("i");
    pip.className = "gr-grid";
    pip.style.left = `${p}%`;
    rail.appendChild(pip);
  }
  const anim = entering && !reducedMotion();
  stats.dots.forEach((d, k) => {
    const dot = document.createElement("span");
    dot.className = "gr-dot";
    dot.dataset.idx = String(d.idx);
    dot.style.width = dot.style.height = `${d.size}px`;
    dot.style.background = hue;
    dot.style.left = `${Math.max(0, Math.min(100, d.pct))}%`;
    if (anim) {
      dot.style.animation = "fade-soft .35s var(--ease-out) backwards";
      dot.style.animationDelay = `${Math.min(k, 20) * 12}ms`;
    }
    rail.appendChild(dot);
  });
  if (s.current_pct != null) {
    const needle = document.createElement("span");
    needle.className = "gr-needle";
    needle.style.background = hue;
    // The server's figure verbatim: the needle may never disagree with the
    // printed percentage.
    needle.style.left = `${Math.max(0, Math.min(100, s.current_pct))}%`;
    // Lands after the dot stagger (0-240ms): the reading arrives last.
    if (anim) needle.style.animation = "needle-in .45s var(--ease-out) 260ms backwards";
    rail.appendChild(needle);
  }
  // Hover: every mark within 6px of the cursor, true (unclamped) percents.
  const litIdx = new Set();
  const syncLit = hits => {
    // Delta-toggle, never clear-all-then-re-add: dozens of dots, 60Hz moves.
    const want = new Set(hits.map(d => String(d.idx)));
    const bandEl = rail.closest(".gr-band");
    for (const idx of [...litIdx]) {
      if (!want.has(idx)) {
        const dot = rail.querySelector(`.gr-dot[data-idx="${idx}"]`);
        if (dot) dot.classList.remove("hot");
        const row = bandEl && bandEl.querySelector(`.gr-item[data-idx="${idx}"]`);
        if (row) row.classList.remove("lit");
        litIdx.delete(idx);
      }
    }
    for (const idx of want) {
      if (!litIdx.has(idx)) {
        const dot = rail.querySelector(`.gr-dot[data-idx="${idx}"]`);
        if (dot) dot.classList.add("hot");
        const row = bandEl && bandEl.querySelector(`.gr-item[data-idx="${idx}"]`);
        if (row) row.classList.add("lit");
        litIdx.add(idx);
      }
    }
    rail.classList.toggle("has-hot", litIdx.size > 0);
  };
  rail.addEventListener("mousemove", e => {
    const r = rail.getBoundingClientRect();
    const px = e.clientX - r.left;
    const hits = stats.dots.filter(d =>
      Math.abs((Math.max(0, Math.min(100, d.pct)) / 100) * r.width - px) <= 6);
    syncLit(hits);
    if (!hits.length) { hideTip(); return; }
    const html = `<div class="tip-head">${escapeHtml(c.course)}</div>`
      + hits.map(d => tipRow(hue, escapeHtml(d.item.name || "?"),
        `${gradesScore(d.item.score)}/${gradesScore(d.item.out_of)} · ${Math.round(d.pct)}%`)).join("");
    showTip(html, e);
  });
  rail.addEventListener("mouseleave", () => { syncLit([]); hideTip(); });
  return rail;
}

function buildPointsRow(stats, hue, entering, basis) {
  const row = document.createElement("div");
  row.className = "gr-pointsrow";
  const wrap = document.createElement("div");
  wrap.className = "gr-pwrap";
  row.appendChild(wrap);
  const total = stats.banked + stats.lost + stats.open;
  if (total <= 0) return row;
  const bar = document.createElement("div");
  bar.className = "gr-pbar";
  const bw = 100 * stats.banked / total;
  const lw = 100 * stats.lost / total;
  const banked = document.createElement("span");
  banked.className = "banked";
  banked.style.width = `${bw}%`;
  banked.style.background = hue;
  const lost = document.createElement("span");
  lost.className = "lost";
  lost.style.left = `${bw}%`;
  lost.style.width = `${lw}%`;
  lost.style.background = hue;
  const open = document.createElement("span");
  open.className = "open";
  open.style.left = `${bw + lw}%`;
  open.style.width = `${Math.max(0, 100 - bw - lw)}%`;
  bar.appendChild(banked);
  bar.appendChild(lost);
  bar.appendChild(open);
  drawStrip(bar, entering);
  wrap.appendChild(bar);
  const nums = document.createElement("div");
  nums.className = "gr-pnums";
  const part = (v, word) => {
    const sp = document.createElement("span");
    if (!v) sp.className = "dim";
    sp.textContent = `${gradesScore(v)} ${word}`;
    return sp;
  };
  // Coverage first. Early in the term "0 banked · 0 lost · 1370 open" led
  // with the least useful number and read as three-quarters zeros; how much
  // of the book has been decided is the honest headline. It stays a FRACTION,
  // never a percent - a second percentage here could be misread as a grade.
  const decided = stats.banked + stats.lost;
  const totalPts = decided + stats.open;
  if (totalPts > 0) {
    const cov = document.createElement("span");
    cov.className = "gr-cov";
    cov.textContent = `${gradesScore(decided)} of ${gradesScore(totalPts)} pts graded`;
    nums.appendChild(cov);
    nums.appendChild(document.createTextNode(" · "));
  }
  if (!decided) nums.classList.add("dim");
  nums.appendChild(part(stats.banked, "banked"));
  nums.appendChild(document.createTextNode(" · "));
  nums.appendChild(part(stats.lost, "lost"));
  // Honesty suffix: on a weighted gradebook these sums are raw points, not
  // the grade's currency - say so, always.
  nums.appendChild(document.createTextNode(
    basis === "weighted" ? " · raw pts" : " · pts"));
  wrap.appendChild(nums);
  return row;
}

function buildGradedRow(i, idx, hue, band) {
  const row = document.createElement("div");
  row.className = "gr-item";
  row.dataset.idx = String(idx);
  if (i.excluded) row.classList.add("dimmed");
  const name = document.createElement("span");
  name.className = "gr-item-name";
  name.title = i.name || "";
  // Ellipsis lives on an inner span so the BONUS / NOT COUNTED tags can
  // never be clipped by the name's own overflow.
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = i.name || "?";
  name.appendChild(nm);
  if (i.bonus) {
    const t = document.createElement("span");
    t.className = "gr-tag bonus";
    t.textContent = "BONUS";
    name.appendChild(t);
  }
  if (i.excluded) {
    const t = document.createElement("span");
    t.className = "gr-tag excl";
    t.textContent = "NOT COUNTED";
    name.appendChild(t);
  }
  row.appendChild(name);
  // The platform's own words, quoted in true italic.
  const shown = document.createElement("span");
  shown.className = "gr-shown";
  shown.textContent = i.displayed || "";
  if (i.displayed) shown.title = i.displayed;
  row.appendChild(shown);
  const outOf = Number(i.out_of);
  const meter = document.createElement("span");
  if (outOf) {
    meter.className = "gr-meter";
    const fill = document.createElement("b");
    const pct = 100 * (Number(i.score) || 0) / outOf;
    fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    fill.style.background = hue;
    meter.appendChild(fill);
  }
  row.appendChild(meter);
  const frac = document.createElement("span");
  frac.className = "gr-frac" + (Number(i.score) === 0 ? " zero" : "");
  frac.innerHTML = `<span class="n">${escapeHtml(gradesScore(i.score))}</span>`
    + `<span class="d">/${escapeHtml(gradesScore(i.out_of))}</span>`;
  row.appendChild(frac);
  const ip = document.createElement("span");
  ip.className = "gr-itempct" + (outOf && Number(i.score) === 0 ? " zero" : "");
  ip.textContent = outOf ? `${Math.round(100 * (Number(i.score) || 0) / outOf)}%` : "—";
  if (!outOf) ip.classList.add("zero");
  row.appendChild(ip);
  // Cross-highlight: hovering the row lights its mark AND recedes the
  // rail's other dots, so the mark is findable among dozens.
  row.addEventListener("mouseenter", () => {
    const dot = band.querySelector(`.gr-dot[data-idx="${idx}"]`);
    if (dot) dot.classList.add("hot");
    const rl = band.querySelector(".gr-rail");
    if (rl) rl.classList.add("has-hot");
  });
  row.addEventListener("mouseleave", () => {
    const dot = band.querySelector(`.gr-dot[data-idx="${idx}"]`);
    if (dot) dot.classList.remove("hot");
    const rl = band.querySelector(".gr-rail");
    if (rl) rl.classList.remove("has-hot");
  });
  return row;
}

function buildPendingRow(i, bandMaxOut) {
  const row = document.createElement("div");
  row.className = "gr-item gr-ungraded";
  if (i.excluded) row.classList.add("dimmed");
  const name = document.createElement("span");
  name.className = "gr-item-name";
  name.title = i.name || "";
  // Ellipsis lives on an inner span so the BONUS / NOT COUNTED tags can
  // never be clipped by the name's own overflow.
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = i.name || "?";
  name.appendChild(nm);
  if (i.bonus) {
    const t = document.createElement("span");
    t.className = "gr-tag bonus";
    t.textContent = "BONUS";
    name.appendChild(t);
  }
  if (i.excluded) {
    const t = document.createElement("span");
    t.className = "gr-tag excl";
    t.textContent = "NOT COUNTED";
    name.appendChild(t);
  }
  row.appendChild(name);
  row.appendChild(document.createElement("span"));   // shown: empty
  const meter = document.createElement("span");
  meter.className = "gr-meter";                      // open = empty track
  // The track quietly draws each item's share of the band's outstanding
  // weight - the "heaviest ahead" claim made visible. Computed number only.
  const mp = Number(i.max_points) || 0;
  if (bandMaxOut > 0 && mp > 0) {
    const fill = document.createElement("b");
    fill.className = "pending";
    fill.style.width = `${Math.max(28, Math.round(100 * mp / bandMaxOut))}%`;
    meter.appendChild(fill);
  }
  row.appendChild(meter);
  // Ink inversion: the worth IS the datum here, so it carries the ink.
  const frac = document.createElement("span");
  frac.className = "gr-frac";
  frac.innerHTML = i.max_points != null
    ? `<span class="n">—</span><span class="d">/${escapeHtml(gradesScore(i.max_points))}</span>`
    : `<span class="n">—</span>`;
  row.appendChild(frac);
  row.appendChild(document.createElement("span"));   // pct: empty
  return row;
}

let _lastGrades = null;

function renderGrades(g) {
  _lastGrades = g;
  const body = $("#grades-body");
  // Rebuilds must not collapse what the reader opened: snapshot open
  // disclosure ids, re-open them after the rebuild (missing ids degrade to
  // collapsed, never throw).
  const openIds = [...body.querySelectorAll(".gr-pending:not(.hidden)")].map(el => el.id);
  const courses = g.courses || [];
  const entering = isEntering("#tab-grades");
  gradesMetaLine(g);
  buildGradesScale(courses.length > 0);

  // ---- lead: the report card's own numbers, per course, in course ink.
  const lead = $("#grades-lead");
  if (lead) {
    lead.innerHTML = "";
    // The GPA leads the strip: it is the one figure ABOUT the strip rather
    // than a member of it. Server-derived (brain.grades.gpa_summary) so a
    // single implementation owns the 4.0 scale, and courses with nothing
    // graded are excluded rather than counted as zero.
    const gpa = g.gpa;
    if (gpa && gpa.gpa != null) {
      const fig = document.createElement("div");
      fig.className = "an-fig gr-fig gr-gpa";
      const fl = document.createElement("div");
      fl.className = "fl";
      fl.textContent = "GPA";
      const fv = document.createElement("div");
      fv.className = "fv tnum";
      const num = document.createElement("span");
      // animateCount is integer-only: count in hundredths, format back down.
      animateCount(num, Math.round(gpa.gpa * 100),
        { animate: entering, format: n => (n / 100).toFixed(2) });
      fv.appendChild(num);
      const basis = document.createElement("div");
      basis.className = "gr-fig-basis";
      // It states its own footing, so a figure standing on 4 of 5 courses
      // cannot be mistaken for a final standing.
      basis.textContent = gpa.courses_counted === gpa.courses_total
        ? `all ${gpa.courses_total} courses`
        : `${gpa.courses_counted} of ${gpa.courses_total} courses`;
      fig.appendChild(fl);
      fig.appendChild(fv);
      fig.appendChild(basis);
      lead.appendChild(fig);
    }
    const letterOf = {};
    for (const r of (gpa && gpa.rows) || []) letterOf[r.course] = r.letter;
    for (const c of courses) {
      const s = c.summary || {};
      const fig = document.createElement("div");
      fig.className = "an-fig gr-fig";
      const fl = document.createElement("div");
      fl.className = "fl";
      fl.textContent = c.course || "?";
      fl.style.color = courseInk(c.course);
      const fv = document.createElement("div");
      fv.className = "fv tnum";
      if (s.current_pct != null) {
        fv.style.color = courseInk(c.course);
        const num = document.createElement("span");
        // animateCount is integer-only: count in tenths, format back down.
        animateCount(num, Math.round(s.current_pct * 10),
          { animate: entering, format: n => gradesScore(n / 10) });
        fv.appendChild(num);
        const pc = document.createElement("span");
        pc.className = "gr-fig-pc";
        pc.textContent = "%";
        fv.appendChild(pc);
      } else {
        fv.classList.add("zero");
        fv.textContent = "—";
      }
      const basis = document.createElement("div");
      basis.className = "gr-fig-basis";
      basis.textContent = s.current_pct != null
        ? (s.basis || "") : "nothing graded yet";
      // Same mapping the GPA used, so the strip and the headline can never
      // disagree about a course.
      if (letterOf[c.course]) {
        const lt = document.createElement("span");
        lt.className = "gr-letter";
        lt.style.color = courseInk(c.course);
        lt.textContent = letterOf[c.course];
        basis.prepend(lt);
      }
      fig.appendChild(fl);
      fig.appendChild(fv);
      fig.appendChild(basis);
      // The figure is a real target: it jumps to its course band.
      fig.tabIndex = 0;
      fig.setAttribute("role", "button");
      fig.setAttribute("aria-label", `${c.course}: jump to gradebook`);
      const jump = () => {
        const bands = [...$("#grades-body").querySelectorAll(".gr-band")];
        const target = bands.find(b2 => {
          const codeEl = b2.querySelector(".gr-code");
          return codeEl && codeEl.textContent === (c.course || "");
        });
        if (target) target.scrollIntoView({
          behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
      };
      fig.addEventListener("click", jump);
      fig.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); jump(); }
      });
      lead.appendChild(fig);
    }
    if (courses.length) {
      const graded = courses.reduce((n, c) => n + ((c.summary || {}).graded_count || 0), 0);
      const totalN = courses.reduce((n, c) => n + ((c.summary || {}).total_count || 0), 0);
      const meta = document.createElement("span");
      meta.id = "gr-lead-meta";
      meta.textContent = `${graded} of ${totalN} items graded`;
      lead.appendChild(meta);
    }
  }

  body.innerHTML = "";
  if (!courses.length) {
    const why = (g.errors && g.errors.length)
      ? g.errors.map(e => e[1]).join("; ")
      : "is the OAKS session alive?";
    const hint = g.needs_refresh
      ? "No grades fetched yet - use “fetch grades” above to pull your gradebooks."
      : `No gradebooks reachable - ${why}. Re-connect: paste a fresh OAKS cURL to Claude, or run brain sync login oaks.`;
    body.innerHTML = `<div class="empty-line">${escapeHtml(hint)}</div>`;
    return;
  }
  if (g.stale) {
    const warn = document.createElement("div");
    warn.className = "notice notice-warn";
    warn.textContent = "The OAKS session has expired - showing the last "
      + "fetched grades. Paste a fresh OAKS cURL to Claude (or run "
      + "brain sync login oaks) to reconnect.";
    body.appendChild(warn);
  } else if (g.errors && g.errors.length) {
    // Partial failure must not masquerade as a complete gradebook.
    const warn = document.createElement("div");
    warn.className = "notice notice-warn";
    warn.textContent = "Some sources did not refresh: "
      + g.errors.map(e => `${e[0]}: ${e[1]}`).join(" · ");
    body.appendChild(warn);
  }
  for (const c of courses) {
    const s = c.summary || {};
    const stats = computeCourseStats(c);
    const hue = collectionColor(c.course);
    const band = document.createElement("div");
    band.className = "gr-band";

    // ---- head row: tick / code / pct / THE RAIL / count
    const head = document.createElement("div");
    head.className = "gr-course";
    const tick = document.createElement("span");
    tick.className = "gr-tick";
    tick.style.background = hue;
    head.appendChild(tick);
    const code = document.createElement("span");
    code.className = "gr-code";
    code.style.color = courseInk(c.course);
    code.textContent = c.course || "?";
    head.appendChild(code);
    const hasPct = s.current_pct !== null && s.current_pct !== undefined;
    const pctEl = document.createElement("span");
    pctEl.className = "gr-pct" + (hasPct ? "" : " zero");
    pctEl.textContent = hasPct ? `${s.current_pct}%` : "—";
    head.appendChild(pctEl);
    // A rail with no marks is a zero-length scale pretending to be an axis -
    // a bare hairline plus a tab stop whose only content repeats the visible
    // "0/16 graded". Print it only where there is something to plot.
    if (stats.dots.length) {
      head.appendChild(buildGradesRail(c, stats, hue, entering));
    } else {
      head.appendChild(document.createElement("span"));
    }
    const gradedCount = s.graded_count || 0;
    const count = document.createElement("span");
    count.className = "gr-count tnum" + (gradedCount === 0 ? " zero" : "");
    count.textContent = `${gradedCount}/${s.total_count ?? "?"} graded`;
    head.appendChild(count);
    band.appendChild(head);

    // ---- banked / lost / open points bar
    band.appendChild(buildPointsRow(stats, hue, entering, s.basis));

    // ---- item ledger
    const items = c.items || [];
    items.forEach((i, idx) => {
      if (i.graded) band.appendChild(buildGradedRow(i, idx, hue, band));
    });
    const pending = items.filter(i => !i.graded);
    // Sparse bands get their heaviest outstanding work promoted into view.
    // With 1 of 8 graded, hiding every remaining item behind a disclosure
    // leaves the band with nothing to show but an apology; the work ahead is
    // the only thing that course can honestly report. Sorted by what it is
    // worth (max_points), NOT by `weight` - that field sums to 80/100/400/
    // 550/100 across the five live books and is unusable for share math.
    const sparse = (s.graded_count || 0) <= 2 && pending.length > 0;
    if (sparse) {
      const ahead = [...pending]
        .sort((a2, b2) => (Number(b2.max_points) || 0) - (Number(a2.max_points) || 0))
        .slice(0, 3);
      const cap = document.createElement("div");
      cap.className = "gr-aheadcap";
      cap.textContent = "Heaviest ahead";
      band.appendChild(cap);
      const bandMaxOut = Math.max(0, ...pending.map(p => Number(p.max_points) || 0));
      for (const i of ahead) band.appendChild(buildPendingRow(i, bandMaxOut));
    }
    if (pending.length) {
      const openPts = stats.open;
      const toggle = document.createElement("button");
      toggle.className = "text-action gr-pending-toggle";
      const label = `show ${sparse ? "all " : ""}${pending.length} outstanding`
        + (openPts ? ` · ${gradesScore(openPts)} pts` : "");
      toggle.textContent = label;
      toggle.setAttribute("aria-expanded", "false");
      const list = document.createElement("div");
      list.className = "gr-pending hidden";
      list.id = `gr-pending-${(c.course || "x").replace(/\W/g, "")}`;
      toggle.setAttribute("aria-controls", list.id);
      const maxOut = Math.max(0, ...pending.map(p => Number(p.max_points) || 0));
      for (const i of pending) list.appendChild(buildPendingRow(i, maxOut));
      toggle.addEventListener("click", () => {
        const nowHidden = list.classList.toggle("hidden");
        toggle.setAttribute("aria-expanded", String(!nowHidden));
        toggle.textContent = nowHidden ? label : "hide outstanding";
        if (!nowHidden) {
          // Reveal settles row by row; settle() strips its class, so
          // re-toggling never replays a stale animation.
          [...list.children].forEach((row, ri) => {
            row.style.animationDelay = `${Math.min(ri, 8) * 14}ms`;
            settle(row, { rise: true });
          });
        }
      });
      if (openIds.includes(list.id)) {
        list.classList.remove("hidden");
        toggle.setAttribute("aria-expanded", "true");
        toggle.textContent = "hide outstanding";
      }
      band.appendChild(toggle);
      band.appendChild(list);
    }
    body.appendChild(band);
  }
}

/* Pure render from data: resize and theme-toggle re-invoke this. */
function renderAnalytics(a) {
  const t = a.totals;

  // ---- Band 1: masthead figure strip
  const entering = isEntering("#tab-analytics");

  // ---- Lead: one sentence about the window a student can act on.
  // The old masthead printed four 26px figures that the ledger's totals
  // footer repeats verbatim further down the same screen, and they were
  // semester-scale numbers that change nothing about today.
  const days = a.daily_load || [];
  const todayIso = isoDate(startOfDay(new Date()));
  const win = days.filter(d => d.date >= todayIso).slice(0, 28);
  const winTotal = win.reduce((n, d) => n + d.count, 0);
  const busiest = win.reduce((best, d) => (d.count > (best ? best.count : 0) ? d : best), null);
  const stmt = $("#an-statement");
  const lmeta = $("#an-leadmeta");
  if (!winTotal) {
    stmt.textContent = "Nothing due in the next four weeks.";
  } else {
    const heavy = busiest && busiest.count > 1
      ? `, and ${new Date(busiest.date + "T00:00:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })} is the wall at ${busiest.count}`
      : "";
    stmt.innerHTML = `<b>${winTotal}</b> ${winTotal === 1 ? "deadline" : "deadlines"} in the next four weeks${escapeHtml(heavy)}.`;
    animateCount(stmt.querySelector("b"), winTotal, { animate: entering });
  }
  const clearDays = win.filter(d => !d.count).length;
  const bits = [];
  if (win.length) bits.push(`${clearDays} of ${win.length} days clear`);
  if (a.semester && a.semester.pct_elapsed != null) {
    bits.push(`${a.semester.pct_elapsed}% of the semester elapsed`);
    bits.push(`${a.semester.weeks_remaining} weeks left`);
  }
  lmeta.textContent = bits.join(" \u00b7 ");

  // ---- Ribbon: one column per day, 28 days, stacked by course. This is
  // analytics.daily_load, which every payload has shipped and nothing read.
  const ribbon = $("#an-ribbon");
  const rmeta = $("#ribbon-meta");
  ribbon.innerHTML = "";
  const maxDay = Math.max(1, ...win.map(d => d.count));
  rmeta.textContent = winTotal ? `peak ${maxDay} in a day` : "";
  win.forEach((d, i) => {
    const date = new Date(d.date + "T00:00:00");
    const col = document.createElement("button");
    col.type = "button";
    col.className = "rb-day" + (d.date === todayIso ? " is-today" : "")
      + ([0, 6].includes(date.getDay()) ? " weekend" : "")
      + (d.count === maxDay && d.count > 1 ? " is-peak" : "");
    const stack = document.createElement("span");
    stack.className = "rb-stack";
    // Entrance: each day's column grows from the baseline, 12ms apart.
    stack.style.animationDelay = `${i * 12}ms`;
    // Segments carry course color, but every column also states its count in
    // the tooltip and aria-label, so identity never rests on hue. dataset
    // stamps feed the ledger-to-ribbon cross-highlight.
    for (const [course, n] of Object.entries(d.courses || {})) {
      const seg = document.createElement("span");
      seg.style.background = collectionColor(course);
      seg.style.height = `${Math.round(52 * n / maxDay)}px`;
      seg.dataset.course = course;
      stack.appendChild(seg);
    }
    if (d.count === maxDay && d.count > 1) {
      const pk = document.createElement("span");
      pk.className = "rb-peak tnum";
      pk.textContent = String(d.count);
      stack.appendChild(pk);
    }
    col.appendChild(stack);
    const dow = document.createElement("span");
    dow.className = "rb-dow";
    dow.textContent = date.toLocaleDateString(undefined, { weekday: "narrow" });
    col.appendChild(dow);
    const label = date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
    const aria = `${label}: ${d.count === 0 ? "clear" : plural(d.count, "deadline")}`;
    col.setAttribute("aria-label", aria);
    // The house tooltip replaces the native title: per-course rows in course
    // color, keyboard parity via focus at the column's center.
    const html = `<div class="tip-head">${escapeHtml(label)}</div>`
      + Object.entries(d.courses || {}).map(([c2, n]) =>
        tipRow(collectionColor(c2), escapeHtml(c2), n)).join("")
      + (d.count ? tipRow(null, "<b>total</b>", `<b>${d.count}</b>`) : `<div class="tip-row"><span>clear</span></div>`);
    col.addEventListener("mousemove", e => showTip(html, e));
    col.addEventListener("mouseleave", hideTip);
    col.addEventListener("focus", () => {
      const r = col.getBoundingClientRect();
      showTip(html, { clientX: r.left + r.width / 2, clientY: r.top });
    });
    col.addEventListener("blur", hideTip);
    col.addEventListener("click", () => {
      hideTip();
      state.cal.cursor = startOfDay(date);
      setCalMode("week");
      showTab("calendar");
    });
    ribbon.appendChild(col);
  });

  // ---- Band 2: workload with fused week ruler
  const wl = a.week_load;
  const wlSvg = $("#chart-weekload");
  const wlEmpty = !wl.weeks.length
    || wl.weeks.every(w => Object.values(w.by_course).every(v => v === 0));
  const wlMeta = $("#workload-meta");
  const emptyLine = $("#workload-empty");
  if (wlEmpty) {
    wlSvg.classList.add("hidden");
    emptyLine.textContent = "No semester schedule loaded.";
    emptyLine.classList.remove("hidden");
    wlMeta.innerHTML = "";
  } else {
    wlSvg.classList.remove("hidden");
    emptyLine.classList.add("hidden");
    const series = wl.courses.map(c => ({ key: c, color: collectionColor(c) }));
    const totals = wl.weeks.map(w => Object.values(w.by_course).reduce((x, y) => x + y, 0));
    const peakIdx = totals.indexOf(Math.max(...totals));
    const peakDate = new Date(wl.weeks[peakIdx].week_start + "T00:00:00");
    wlMeta.innerHTML = `events/wk · peak ${totals[peakIdx]}, `
      + `${peakDate.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
      + `&nbsp;&nbsp;` + series.map(s =>
        `<span style="color:${courseInk(s.key)};font-weight:600">${escapeHtml(s.key)}</span>`).join(" ");
    const thisMonday = isoDate(mondayOf(new Date()));
    const weeks = wl.weeks.map(w => {
      const d = new Date(w.week_start + "T00:00:00");
      return {
        label: `Week of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`,
        short: d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" }),
        parts: Object.entries(w.by_course).map(([key, value]) => ({ key, value })),
        _current: w.week_start === thisMonday,
        _past: w.week_start < thisMonday,
      };
    });
    wlSvg.setAttribute("aria-label",
      `Events per week across ${weeks.length} weeks, stacked by course`);
    fusedWeekloadChart(wlSvg, weeks, series, {
      labelEvery: weeks.length > 14 ? 2 : 1,
      markerIndex: weeks.findIndex(w => w._current),
      peakIndex: peakIdx,
      animate: entering && !reducedMotion(),
    });
  }

  // ---- Band 3 left: course ledger
  const ledger = $("#course-ledger");
  ledger.innerHTML = "";
  if (!a.by_course.length) {
    ledger.innerHTML = `<div class="empty-line">No courses with dated work yet.</div>`;
  } else {
    const head = document.createElement("div");
    head.className = "cl-row head";
    head.innerHTML = `<span></span><span>Course</span><span class="cl-num">Exams</span>
      <span class="cl-num">Quizzes</span><span class="cl-num">Projects</span>
      <span class="cl-num">Left</span><span></span><span>Next</span>`;
    ledger.appendChild(head);
    const rows = [...a.by_course].sort((x, y) => y.remaining - x.remaining);
    const maxRemaining = Math.max(1, ...rows.map(c => c.remaining));
    for (const c of rows) {
      const row = document.createElement("div");
      row.className = "cl-row" + (c.remaining === 0 ? " done" : "");
      row.dataset.course = c.course;
      const num = v => `<span class="cl-num${v === 0 ? " zero" : ""}">${v}</span>`;
      let next = `<span class="cl-next">clear</span>`;
      if (c.next_at) {
        const n = c.days_until_next;
        const tagCls = n <= 1 ? "urgent" : n <= 3 ? "soon" : "";
        const tag = n === 0 ? "today" : n === 1 ? "tomorrow" : `${n}d`;
        const when = new Date(c.next_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
        const title = c.next_title ? ` · ${c.next_title.length > 36 ? c.next_title.slice(0, 35) + "…" : c.next_title}` : "";
        next = `<span class="cl-next"><span class="tag ${tagCls}">${tag}</span>${when}${escapeHtml(title)}</span>`;
      } else if (c.note) {
        next = `<span class="cl-next">${escapeHtml(c.note)}</span>`;
      }
      const hue = themedColor(c.color);
      const barW = c.remaining > 0 ? Math.max(3, Math.round(150 * c.remaining / maxRemaining)) : 0;
      row.innerHTML = `
        <span class="cl-tick" style="background:${hue}"></span>
        <span class="cl-code">${escapeHtml(c.course)}</span>
        ${num(c.exam)}${num(c.quiz)}${num(c.project)}
        <span class="cl-left">${c.remaining}</span>
        <span class="cl-bartrack"><span class="cl-bar" style="width:${barW}px;background:${hue}"></span></span>
        ${next}`;
      ledger.appendChild(row);
    }
    const total = document.createElement("div");
    total.className = "cl-row total";
    total.innerHTML = `<span></span><span></span>
      <span class="cl-num">${t.exams_remaining}</span>
      <span class="cl-num">${t.quizzes_remaining}</span>
      <span class="cl-num">${t.projects_remaining}</span>
      <span class="cl-left">${t.deadlines_remaining}</span><span></span><span></span>`;
    ledger.appendChild(total);
  }
  const cmeta = $("#courses-meta");
  if (cmeta) {
    cmeta.textContent = `${t.deadlines_remaining} left this semester \u00b7 `
      + `${t.exams_remaining} exams \u00b7 ${t.quizzes_remaining} quizzes \u00b7 ${t.projects_remaining} projects`;
  }

  // ---- Pressure, scoped to the ribbon's own window so the two agree.
  const bd = $("#busiest-days");
  bd.innerHTML = "";
  const winDates = new Set(win.map(d => d.date));
  const pressure = (a.busiest_days || []).filter(d => winDates.has(d.date));
  if (!pressure.length) {
    bd.innerHTML = `<div class="empty-line">Nothing doubles up in the next four weeks.</div>`;
  }
  for (const d of pressure) {
    const date = new Date(d.date + "T00:00:00");
    const day = document.createElement("div");
    day.className = "pr-day";
    day.addEventListener("click", () => {
      state.cal.cursor = startOfDay(date);
      setCalMode("week");
      showTab("calendar");
    });
    railKeyable(day);
    const items = d.items.map(i => `
      <div class="pr-item">
        <span class="rule" style="background:${collectionColor(i.course)}"></span>
        <span class="code" style="color:${courseInk(i.course)}">${escapeHtml(i.course)}</span>
        <span class="t">${escapeHtml(i.title)}</span>
      </div>`).join("");
    day.innerHTML = `
      <div class="pr-head"><span>${date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</span>
        <span class="cnt">×${d.count}</span></div>${items}`;
    bd.appendChild(day);
  }

}


/* ============================================================= calendar */

async function fetchEvents(start, end) {
  return api(`/api/events?start=${isoDate(start)}T00:00:00&end=${isoDate(end)}T00:00:00`);
}

/* Bucket events by date. A multi-day event is listed on every day it covers,
   not only the day it started. */
function bucketByDay(events) {
  const byDay = {};
  for (const ev of events) {
    const start = new Date(ev.starts_at);
    let end = ev.ends_at ? new Date(ev.ends_at) : start;
    // An end at exactly midnight belongs to the previous day.
    if (end > start && end.getHours() === 0 && end.getMinutes() === 0
        && end.getSeconds() === 0) {
      end = new Date(end.getTime() - 1000);
    }
    let d = startOfDay(start);
    const lastDay = startOfDay(end);
    let guard = 0;
    while (d <= lastDay && guard++ < 400) {
      const key = isoDate(d);
      (byDay[key] = byDay[key] || []).push(
        guard === 1 ? ev : { ...ev, _continued: true });
      d = addDays(d, 1);
    }
  }
  return byDay;
}

/* Clicking prev/next faster than the fetches return would otherwise let an
   older response paint over a newer one; only the newest render may draw. */
let calRenderSeq = 0;

function visibleKind(ev) {
  return state.cal.showClasses || ev.kind !== "recurring";
}

/* Set by prev/next so the fresh view settles in from its travel direction. */
let calSlideDir = 0;

async function renderCalendar() {
  const seq = ++calRenderSeq;
  // Claim the slide direction for THIS render; a superseded render must not
  // be able to zero out a newer click's direction.
  const slideDir = calSlideDir;
  calSlideDir = 0;
  if (!state.weekload.length) {
    try { state.weekload = await api("/api/weekload"); }
    catch { state.weekload = []; }
    if (seq !== calRenderSeq) return;
  }
  renderRuler();
  const view = state.cal.mode === "month" ? $("#cal-month") : $("#cal-week");
  // The directional slide covers the swap on nav clicks; dimming as well
  // would stack three opacity moves into one navigation.
  let loadTimer = null;
  if (!slideDir) {
    // Armed at 120ms: memoized fetches (classes toggle, keyboard t) resolve
    // faster than that and must not flash the dim.
    loadTimer = setTimeout(() => {
      view.classList.add("loading");
      view.setAttribute("aria-busy", "true");
    }, 120);
  }
  try {
    if (state.cal.mode === "month") await renderMonth(seq);
    else await renderWeek(seq);
  } finally {
    clearTimeout(loadTimer);
    if (seq === calRenderSeq) {
      view.classList.remove("loading");
      view.removeAttribute("aria-busy");
      if (slideDir && !reducedMotion()) {
        const cls = slideDir < 0 ? "slide-l" : "slide-r";
        view.classList.remove("slide-l", "slide-r");
        void view.offsetWidth;   // restart the animation cleanly
        view.classList.add(cls);
        setTimeout(() => view.classList.remove(cls), 300);
      }
    }
  }
}

/* Semester ruler: a labeled strip (the overline row above it names it), one
   thin bar per week of DEADLINES — class meetings would flatten the crunch
   weeks — with the current week in accent and a caption bound to the view. */
function renderRuler() {
  const ruler = $("#sem-ruler");
  const meta = $("#ruler-meta");
  ruler.innerHTML = "";
  if (!state.weekload.length) {
    if (meta) meta.textContent = "";
    return;
  }
  const base = document.createElement("div");
  base.className = "base";
  ruler.appendChild(base);
  const bars = document.createElement("div");
  bars.className = "bars";
  ruler.appendChild(bars);
  // Older server payloads carry only count (classes included); degrade to it.
  const dl = w => (w.deadlines !== undefined ? w.deadlines : w.count);
  const max = Math.max(1, ...state.weekload.map(dl));
  const current = isoDate(mondayOf(new Date()));
  let curIdx = -1;
  state.weekload.forEach((w, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wk";
    const isNow = w.week_start === current;
    if (isNow) { curIdx = i; btn.classList.add("now"); }
    const bar = document.createElement("span");
    const n = dl(w);
    if (n > 0) bar.style.height = `${Math.round(3 + 13 * (n / max))}px`;
    else bar.classList.add("zero");
    btn.appendChild(bar);
    const d = new Date(w.week_start + "T00:00:00");
    const label = `Week of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
    btn.setAttribute("aria-label",
      `${label}: ${plural(n, "deadline")}${isNow ? " (this week)" : ""}`);
    const html = `<div class="tip-head">${label}${isNow ? " · now" : ""}</div>`
      + `<div class="tip-row"><span>deadlines</span><span class="tip-val">${n}</span></div>`;
    btn.addEventListener("mousemove", e => showTip(html, e));
    btn.addEventListener("mouseleave", hideTip);
    btn.addEventListener("click", () => {
      hideTip();
      state.cal.cursor = startOfDay(d);
      setCalMode("week");
    });
    bars.appendChild(btn);
  });
  // One caption, bound to the VIEW. (The old design pinned "Week N of M" to
  // today while a separate bracket tracked the view; the two never agreed.)
  if (meta) {
    const total = state.weekload.length;
    const idxOf = iso => state.weekload.findIndex(w => w.week_start === iso);
    let text = "";
    if (state.cal.mode === "week") {
      const i = idxOf(isoDate(mondayOf(state.cal.cursor)));
      if (i >= 0) text = `Week ${i + 1} of ${total}`;
    } else {
      const c = state.cal.cursor;
      let s = idxOf(isoDate(mondayOf(new Date(c.getFullYear(), c.getMonth(), 1))));
      let e = idxOf(isoDate(mondayOf(new Date(c.getFullYear(), c.getMonth() + 1, 0))));
      // A month straddling either end of the semester clamps to the strip.
      if (s < 0 && e >= 0) s = 0;
      if (e < 0 && s >= 0) e = total - 1;
      if (s >= 0 && e >= 0) {
        text = s === e ? `Week ${s + 1} of ${total}`
          : `Weeks ${s + 1}–${e + 1} of ${total}`;
      }
    }
    if (text && curIdx >= 0) text += ` · now wk ${curIdx + 1}`;
    meta.textContent = text;
    // The viewed window brightens on the strip itself (alpha only - tick
    // height keeps encoding load, out-of-view weeks keep their rest alpha).
    let vs = -1, ve = -1;
    if (state.cal.mode === "week") {
      vs = ve = idxOf(isoDate(mondayOf(state.cal.cursor)));
    } else {
      const c = state.cal.cursor;
      vs = idxOf(isoDate(mondayOf(new Date(c.getFullYear(), c.getMonth(), 1))));
      ve = idxOf(isoDate(mondayOf(new Date(c.getFullYear(), c.getMonth() + 1, 0))));
      if (vs < 0 && ve >= 0) vs = 0;
      if (ve < 0 && vs >= 0) ve = total - 1;
    }
    [...bars.children].forEach((btn, bi) =>
      btn.classList.toggle("in-view", vs >= 0 && bi >= vs && bi <= ve));
  }
}

function calSetTitle() {
  const cur = state.cal.cursor;
  const t = $("#cal-title");
  if (state.cal.mode === "month") {
    t.innerHTML = `${cur.toLocaleDateString(undefined, { month: "long" })} <span class="co">${cur.getFullYear()}</span>`;
  } else {
    const start = mondayOf(cur);
    t.innerHTML = `<span class="co">Week of</span> ${start.toLocaleDateString(undefined, { month: "long", day: "numeric" })}`;
  }
}

/* At month zoom the code lane already names the platform/course, so a
   leading platform prefix only eats the ~10 visible title characters that
   could distinguish "Ch 2" from "Ch 3". Strip it; the full title stays in
   the hover and the popover. */
const MONTH_TITLE_NOISE = /^(?:connect smartbook|smartbook|connect|vhl supersite|vhl|supersite|blended teaching|blended|oaks)\b\s*[:\-]?\s*/i;
function monthTitle(t) {
  const s = t.replace(MONTH_TITLE_NOISE, "");
  return s.trim() ? s : t;
}

async function renderMonth(seq = calRenderSeq) {
  $("#cal-month").classList.remove("hidden");
  $("#cal-week").classList.add("hidden");
  calSetTitle();
  const cur = state.cal.cursor;
  const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
  const gridStart = mondayOf(first);
  const lastDay = new Date(cur.getFullYear(), cur.getMonth() + 1, 0);
  // Math.round: a DST spring-forward inside the range shaves an hour off the
  // diff, and a truncating compare would silently drop the month's last day.
  const cellCount = (Math.round((startOfDay(lastDay) - gridStart) / 86400000) < 35) ? 35 : 42;
  const gridEnd = addDays(gridStart, cellCount);
  let events = [];
  try { events = await fetchEvents(gridStart, gridEnd); }
  catch (e) {
    if (seq !== calRenderSeq) return;
    $("#cal-month").innerHTML = `<div class="notice notice-danger">Failed to load events: ${escapeHtml(e.message)}</div>`;
    announce("Failed to load events: " + e.message);
    return;
  }
  if (seq !== calRenderSeq) return;
  const byDay = bucketByDay(events);
  const grid = $("#cal-month");
  grid.innerHTML = "";

  const dowRow = document.createElement("div");
  dowRow.className = "cm-dowrow";
  for (const dow of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
    const h = document.createElement("span");
    h.className = "cm-dow";
    h.textContent = dow;
    dowRow.appendChild(h);
  }
  grid.appendChild(dowRow);

  const today = new Date();
  for (let w = 0; w < cellCount / 7; w++) {
    const week = document.createElement("div");
    week.className = "cm-week";
    let weekHasContent = false;
    for (let i = 0; i < 7; i++) {
      const day = addDays(gridStart, w * 7 + i);
      const cell = document.createElement("div");
      cell.className = "cm-day";
      if (day.getMonth() !== cur.getMonth()) cell.classList.add("other");
      if (sameDay(day, today)) cell.classList.add("today");
      if (i >= 5) cell.classList.add("weekend");
      const dayEvents = (byDay[isoDate(day)] || []);
      const deadlines = dayEvents.filter(ev => ev.kind !== "recurring");
      const classes = dayEvents.filter(ev => ev.kind === "recurring");
      if (deadlines.length || (classes.length && state.cal.showClasses)) {
        weekHasContent = true;
      }
      // Exams surface first so the 3-line cap can never hide one on its
      // start day; multi-day ghosts (continued exams included) sink last.
      const rank = ev => (ev._continued ? 2 : ev.kind === "exam" ? 0 : 1);
      deadlines.sort((a, b) =>
        rank(a) - rank(b) || a.starts_at.localeCompare(b.starts_at));
      const dueCount = deadlines.filter(ev => !ev._continued).length;

      // Scaffold band: landmark numeral (click = week view on that day) and
      // the day's load figure, so slice(0,3) below can never understate a day.
      const band = document.createElement("div");
      band.className = "cm-band";
      const num = document.createElement("button");
      num.type = "button";
      num.className = "cm-num tnum";
      num.textContent = day.getDate();
      num.setAttribute("aria-label",
        `Open week of ${day.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`);
      num.addEventListener("click", () => {
        state.cal.cursor = startOfDay(day);
        setCalMode("week");
      });
      // Zero-deadline days recede so loaded weeks carry the density map;
      // other-month cells already fade and must not stack both treatments.
      if (dueCount === 0 && !cell.classList.contains("other")) {
        num.classList.add("quiet");
      }
      band.appendChild(num);
      if (cell.classList.contains("today")) {
        // The stamp in CSS is the areal channel; this is the lexical one, and
        // it is what survives grayscale, CVD, forced-colors and print. They
        // ship together: alone, either collapses toward the color-only numeral
        // that was already too weak to find.
        num.setAttribute("aria-current", "date");
        const now = document.createElement("span");
        now.className = "cm-today";
        now.textContent = "Today";   // caps come from CSS so SRs say "today"
        band.appendChild(now);
      }
      if (dueCount > 0) {
        const due = document.createElement("span");
        // No shouting from faded cells: hot is for THIS month's crunch days.
        due.className = "cm-due tnum"
          + (dueCount >= 3 && !cell.classList.contains("other") ? " hot" : "");
        // Crunch days carry a whisper of ground, not just a bold word.
        if (dueCount >= 3 && !cell.classList.contains("other")) cell.classList.add("hot");
        due.textContent = `${dueCount} due`;
        band.appendChild(due);
      }
      cell.appendChild(band);

      const makeLine = ev => {
        const line = document.createElement("button");
        line.type = "button";
        line.className = "cm-line" + (ev._continued ? " cont-line" : "");
        const hue = collectionColor(ev.course);
        const ink = courseInk(ev.course);
        line.innerHTML = `<span class="tick" style="background:${hue}"></span>`
          + `<span class="cm-code" style="color:${ink}">${escapeHtml(ev.course)}</span>`
          + `<span class="txt">`
          + (ev.kind === "exam" ? `<span class="ex" style="color:${ink}">EXAM</span>` : "")
          + (ev._continued ? `<span class="cont">cont.</span> ` : "")
          + `${escapeHtml(monthTitle(ev.title))}</span>`;
        // Clock time leaves the line at month zoom; the hover keeps it.
        line.title = `${ev.all_day ? "all day" : fmtTime(ev.starts_at)} · ${ev.course} · ${ev.title}`
          + (ev._continued ? " · continues" : "");
        line.addEventListener("click", e => {
          e.stopPropagation();
          const p = popoverPoint(e);
          showPopover(ev, p.x, p.y);
        });
        return line;
      };
      const shown = deadlines.slice(0, 3);
      for (const ev of shown) cell.appendChild(makeLine(ev));
      if (deadlines.length > shown.length) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "cm-more";
        const rest = deadlines.slice(shown.length);
        more.textContent = `+${rest.length} more`;
        // Expands in place: hiding known items behind a view switch made the
        // grid read as if those days only had three things on them.
        more.addEventListener("click", e => {
          e.stopPropagation();
          rest.forEach((ev, ri) => {
            const line = makeLine(ev);
            line.style.animationDelay = `${ri * 20}ms`;
            cell.insertBefore(line, more);
            settle(line, { rise: true });
          });
          more.remove();
        });
        cell.appendChild(more);
      }
      if (classes.length && state.cal.showClasses) {
        const dots = document.createElement("button");
        dots.type = "button";
        dots.className = "cm-dots";
        const cn = classes.length;
        const word = `${cn} ${cn === 1 ? "class" : "classes"}`;
        dots.textContent = word;
        dots.setAttribute("aria-label", `${word} on ${day.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`);
        const list = classes.map(ev =>
          `${ev.all_day ? "" : fmtTime(ev.starts_at) + " "}${ev.course}`).join(" · ");
        dots.title = list;
        const html = `<div class="tip-head">Classes</div>` + classes.map(ev =>
          tipRow(collectionColor(ev.course), `${ev.all_day ? "" : fmtTime(ev.starts_at) + " "}${escapeHtml(ev.course)}`, "")).join("");
        dots.addEventListener("mousemove", e => showTip(html, e));
        dots.addEventListener("mouseleave", hideTip);
        dots.addEventListener("click", () => {
          hideTip();
          state.cal.cursor = startOfDay(day);
          setCalMode("week");
        });
        cell.appendChild(dots);
      }
      week.appendChild(cell);
    }
    // Weeks with nothing on them recede like quiet days do, so a month
    // that starts mid-void keeps its loaded weeks above the fold.
    if (!weekHasContent) week.classList.add("bare");
    grid.appendChild(week);
  }
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "empty-line";
    empty.textContent = "No events in this range.";
    grid.appendChild(empty);
  }
}

async function renderWeek(seq = calRenderSeq) {
  $("#cal-month").classList.add("hidden");
  $("#cal-week").classList.remove("hidden");
  calSetTitle();
  const start = mondayOf(state.cal.cursor);
  const end = addDays(start, 7);
  let events = [];
  try { events = await fetchEvents(start, end); }
  catch (e) {
    if (seq !== calRenderSeq) return;
    $("#cal-week").innerHTML = `<div class="notice notice-danger">Failed to load events: ${escapeHtml(e.message)}</div>`;
    announce("Failed to load events: " + e.message);
    return;
  }
  if (seq !== calRenderSeq) return;
  const wrap = $("#cal-week");
  wrap.innerHTML = "";
  const byDay = bucketByDay(events);
  const today = new Date();
  for (let i = 0; i < 7; i++) {
    const day = addDays(start, i);
    const allDayEvents = byDay[isoDate(day)] || [];
    const dayEvents = allDayEvents.filter(visibleKind);
    // "Due" counts only things that actually come due THIS day; multi-day
    // spillover rows and exams already counted at their start would inflate it.
    const dueCount = dayEvents.filter(
      ev => ev.kind !== "recurring" && !ev._continued).length;
    const box = document.createElement("div");
    box.className = "cw-day" + (sameDay(day, today) ? " today" : "");
    const margin = document.createElement("div");
    margin.className = "cw-margin";
    margin.innerHTML = `
      <div class="dow">${day.toLocaleDateString(undefined, { weekday: "short" })}</div>
      <div class="dnum">${day.getDate()}</div>
      ${dueCount ? `<div class="due">${dueCount} due</div>` : ""}`;
    box.appendChild(margin);
    const list = document.createElement("div");
    if (!dayEvents.length) {
      // With classes hidden, a bare "No events" would be a lie on days that
      // have hidden meetings; say what is actually being withheld.
      const hidden = state.cal.showClasses ? 0
        : allDayEvents.filter(ev => ev.kind === "recurring").length;
      list.innerHTML = `<div class="cw-empty">${hidden
        ? `No deadlines · ${hidden} ${hidden === 1 ? "class" : "classes"} hidden`
        : "No events"}</div>`;
    }
    for (const ev of dayEvents) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "cw-row" + (ev.kind === "recurring" ? " recur" : "");
      const tm = (ev.all_day || ev._continued)
        ? (ev._continued ? "continues" : "all day") : fmtTime(ev.starts_at);
      let cd = "";
      if (ev.kind !== "recurring") {
        const n = daysUntil(ev.starts_at);
        if (n === 0) cd = `<span class="cd today">TODAY</span>`;
        else if (n > 0) cd = `<span class="cd">in ${n}d</span>`;
        else cd = `<span class="cd past">past</span>`;
      } else cd = `<span class="cd kind">class</span>`;
      const mark = ev.kind === "exam"
        ? `<span class="ex" style="color:${courseInk(ev.course)}">EXAM</span>` : "";
      row.innerHTML = `
        <span class="tm">${escapeHtml(tm)}</span>
        <span class="t">${mark}<span class="tt">${escapeHtml(ev.title)}</span></span>
        <span class="code" style="color:${courseInk(ev.course)}">${escapeHtml(ev.course)}</span>
        ${cd}`;
      row.addEventListener("click", e => {
        const p = popoverPoint(e);
        showPopover(ev, p.x, p.y);
      });
      list.appendChild(row);
    }
    box.appendChild(list);
    wrap.appendChild(box);
  }
}

function showPopover(ev, x, y) {
  const pop = $("#event-popover");
  pop.innerHTML = "";
  const title = document.createElement("div");
  title.className = "pop-title";
  title.textContent = ev.title;
  pop.appendChild(title);
  const kindWord = ev.kind === "recurring" ? "class meeting" : ev.kind;
  const parts = [ev.course, kindWord];
  let when = fmtWhen(ev);
  if (ev.ends_at && !ev.all_day) when += "–" + fmtTime(ev.ends_at);
  parts.push(when);
  const n = daysUntil(ev.starts_at);
  if (ev.kind !== "recurring" && n > 0) parts.push(`in ${plural(n, "day")}`);
  if (ev.source) parts.push(`from ${ev.source}`);
  const meta = document.createElement("div");
  meta.className = "pop-meta";
  meta.textContent = parts.filter(Boolean).join(" · ");
  pop.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "pop-actions";
  const col = collectionByCourse(ev.course);
  const discuss = document.createElement("button");
  discuss.className = "text-action";
  discuss.textContent = col ? `New chat in ${col.name} →` : "No matching collection";
  discuss.disabled = !col;
  if (col) {
    discuss.addEventListener("click", async () => {
      hidePopover();
      await discussEvent(col.name, ev);
    });
  }
  const close = document.createElement("button");
  close.className = "text-action quiet";
  close.textContent = "Close";
  close.addEventListener("click", hidePopover);
  actions.appendChild(discuss);
  actions.appendChild(close);
  pop.appendChild(actions);

  pop.classList.remove("hidden");
  const rect = pop.getBoundingClientRect();
  pop.style.left = `${Math.min(x, window.innerWidth - rect.width - 16)}px`;
  pop.style.top = `${Math.min(y, window.innerHeight - rect.height - 16)}px`;
  pop.tabIndex = -1;
  _popReturnFocus = document.activeElement;
  _popOpenedAt = performance.now();
  pop.focus({ preventScroll: true });
}

let _popReturnFocus = null;
/* The click that OPENS the popover must not also close it: the document-level
   outside-click handler fires on that same click for any opener that does not
   stopPropagation (the Today plan rows did not). Time-stamping the open beats
   maintaining a class allowlist of every opener. */
let _popOpenedAt = 0;
function hidePopover() {
  const pop = $("#event-popover");
  if (pop.classList.contains("hidden")) return;
  pop.classList.add("hidden");
  // Keyboard users get their place back instead of being dumped on <body>.
  if (_popReturnFocus && _popReturnFocus.isConnected
      && (pop.contains(document.activeElement) || document.activeElement === document.body)) {
    _popReturnFocus.focus({ preventScroll: true });
  }
  _popReturnFocus = null;
}

/* Keyboard activation fires clicks at 0,0; anchor those on the element. */
function popoverPoint(e) {
  if (e.clientX || e.clientY) return { x: e.clientX, y: e.clientY };
  const r = e.currentTarget.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.bottom + 4 };
}

async function discussEvent(collectionName, ev) {
  if (blockedByStream()) return;
  let convo;
  try {
    convo = await api("/api/conversations", {
      method: "POST",
      body: { collection: collectionName, title: `Discuss: ${ev.title}`.slice(0, 60) },
    });
  } catch (e) {
    toast(`Could not start a conversation: ${e.message}`, { danger: true });
    return;
  }
  showTab("chat");
  await selectChatCollection(collectionName);
  await selectConversation(convo.id);
  $("#composer-input").focus();
}

function initCalendarControls() {
  const nav = dir => {
    const c = state.cal.cursor;
    if (state.cal.mode === "month") {
      // Keep the day-of-month (clamped) so a later Month->Week jump lands
      // near where the eye was, not snapped back to the 1st.
      const last = new Date(c.getFullYear(), c.getMonth() + dir + 1, 0).getDate();
      state.cal.cursor = new Date(c.getFullYear(), c.getMonth() + dir,
        Math.min(c.getDate(), last));
    } else {
      state.cal.cursor = addDays(c, dir * 7);
    }
    calSlideDir = dir;
    renderCalendar();
  };
  $("#cal-prev").addEventListener("click", () => nav(-1));
  $("#cal-next").addEventListener("click", () => nav(1));
  $("#cal-today-btn").addEventListener("click", () => {
    state.cal.cursor = startOfDay(new Date());
    renderCalendar();
  });
  $("#cal-mode-month").addEventListener("click", () => setCalMode("month"));
  $("#cal-mode-week").addEventListener("click", () => setCalMode("week"));

  // Classes toggle: recurring meetings shown or hidden everywhere on the tab.
  try {
    state.cal.showClasses = localStorage.getItem("cal.showClasses") !== "0";
  } catch { /* private mode */ }
  const toggle = $("#classes-toggle");
  // Action wording over state marks: "Hide classes" both names the feature
  // and says what clicking does; a lit dot said neither.
  const syncToggle = () => {
    toggle.textContent = state.cal.showClasses ? "Hide classes" : "Show classes";
    toggle.setAttribute("aria-pressed", state.cal.showClasses ? "true" : "false");
  };
  syncToggle();
  toggle.addEventListener("click", () => {
    state.cal.showClasses = !state.cal.showClasses;
    try { localStorage.setItem("cal.showClasses", state.cal.showClasses ? "1" : "0"); } catch { /* private */ }
    syncToggle();
    renderCalendar();
  });

  document.addEventListener("click", e => {
    if (performance.now() - _popOpenedAt < 100) return;   // the opening click
    const pop = $("#event-popover");
    if (!pop.classList.contains("hidden") && !pop.contains(e.target)
        && !e.target.closest(".cm-line") && !e.target.closest(".cw-row")) {
      hidePopover();
    }
  });
  // t / [ / ] while the calendar tab is active and nothing has focus.
  document.addEventListener("keydown", e => {
    if (isTypingContext() || paletteIsOpen()) return;
    if (!$("#tab-calendar").classList.contains("active")) return;
    if (e.key === "t") { state.cal.cursor = startOfDay(new Date()); renderCalendar(); }
    else if (e.key === "[") nav(-1);
    else if (e.key === "]") nav(1);
  });
}

function setCalMode(mode) {
  state.cal.mode = mode;
  $("#cal-mode-month").classList.toggle("active", mode === "month");
  $("#cal-mode-month").setAttribute("aria-selected", mode === "month" ? "true" : "false");
  $("#cal-mode-week").classList.toggle("active", mode === "week");
  $("#cal-mode-week").setAttribute("aria-selected", mode === "week" ? "true" : "false");
  renderCalendar();
}

/* ================================================================= chat */

/* Navigating away mid-stream would leave deltas appending into a detached
   message and the send button stuck disabled. */
function blockedByStream() {
  if (!state.chat.streaming) return false;
  toast("Wait for the current answer to finish (or press Esc to stop it).");
  return true;
}

let _railCollapsed = new Set();
try {
  _railCollapsed = new Set(JSON.parse(localStorage.getItem("cc-rail-collapsed") || "[]"));
} catch { /* private mode */ }
function saveRailCollapsed() {
  try { localStorage.setItem("cc-rail-collapsed", JSON.stringify([..._railCollapsed])); } catch { /* private */ }
}

async function fetchConversations() {
  try { state.conversations = await api("/api/conversations"); }
  catch { state.conversations = []; }
}

/* One tree: collections with their conversations nested inside. */
function renderRailTree() {
  const rail = $("#chat-rail");
  rail.innerHTML = "";
  const convos = state.conversations;
  const byCol = new Map();
  for (const c of convos) {
    const key = c.collection || "all";
    if (!byCol.has(key)) byCol.set(key, []);
    byCol.get(key).push(c);
  }

  const convoRow = c => {
    const row = document.createElement("div");
    row.className = "rail-convo" + (c.id === state.chat.conversationId ? " selected" : "");
    const name = document.createElement("span");
    name.className = "rail-name";
    name.textContent = c.title;
    name.title = c.title;
    row.appendChild(name);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "convo-del";
    del.textContent = "×";
    del.title = "Delete conversation";
    del.addEventListener("click", async e => {
      e.stopPropagation();
      if (blockedByStream()) return;
      if (!del.classList.contains("confirm")) {
        del.classList.add("confirm");
        del.textContent = "sure?";
        setTimeout(() => { del.classList.remove("confirm"); del.textContent = "×"; }, 2500);
        return;
      }
      const wasActive = state.chat.conversationId === c.id;
      try { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); }
      catch (err) {
        toast(`Could not delete the conversation: ${err.message}`, { danger: true });
        return;
      }
      if (wasActive) state.chat.conversationId = null;
      await fetchConversations();
      renderRailTree();
      if (wasActive) renderChatEmpty();
    });
    row.appendChild(del);
    row.addEventListener("click", () => selectConversation(c.id));
    railKeyable(row);
    return row;
  };

  // Row 0: All collections
  const all = document.createElement("div");
  all.className = "rail-row" + (state.chat.collection === "all" ? " selected" : "");
  all.style.setProperty("--rail-hue", "var(--accent)");
  all.innerHTML = `<span class="rail-flag" style="background:${courseFallback()}"></span>
    <span class="rail-name">Everything</span>
    <span class="rail-count tnum">${convos.length || ""}</span>`;
  all.addEventListener("click", () => selectChatCollection("all"));
  railKeyable(all);
  rail.appendChild(all);
  if (state.chat.collection === "all") {
    for (const c of (byCol.get("all") || [])) rail.appendChild(convoRow(c));
  }

  for (const col of state.collections) {
    const mine = byCol.get(col.name) || [];
    const hue = themedColor(col.color);
    const head = document.createElement("div");
    head.className = "rail-row" + (state.chat.collection === col.name ? " selected" : "");
    head.style.setProperty("--rail-hue", hue);
    let mark = "";
    if (col.assist_level === "explain_only") mark = `<span class="rail-mark warn">EXPL</span>`;
    if (col.assist_level === "off") mark = `<span class="rail-mark bad">OFF</span>`;
    head.innerHTML = `<span class="rail-flag" style="background:${hue}"></span>
      <span class="rail-name">${escapeHtml(col.name)}</span>${mark}
      <button type="button" class="rail-new text-action" aria-keyshortcuts="n">New</button>
      <span class="rail-count tnum">${mine.length || ""}</span>`;
    head.setAttribute("aria-expanded", _railCollapsed.has(col.name) ? "false" : "true");
    head.querySelector(".rail-new").addEventListener("click", async e => {
      e.stopPropagation();
      await newConversation(col.name);
    });
    head.addEventListener("click", () => selectChatCollection(col.name));
    railKeyable(head);
    rail.appendChild(head);
    const expanded = !_railCollapsed.has(col.name);
    if (expanded) for (const c of mine) rail.appendChild(convoRow(c));
  }
}

async function newConversation(collection) {
  if (blockedByStream()) return;
  try {
    const convo = await api("/api/conversations", {
      method: "POST", body: { collection },
    });
    state.chat.collection = collection;
    await fetchConversations();
    await selectConversation(convo.id);
  } catch (e) {
    toast(`Could not create the conversation: ${e.message}`, { danger: true });
  }
}

function chatCollection() {
  if (!state.chat.collection || state.chat.collection === "all") return null;
  return state.collections.find(c => c.name === state.chat.collection) || null;
}

function chatHue() {
  const col = chatCollection();
  return col ? themedColor(col.color) : "var(--accent)";
}

function renderChatMasthead() {
  const head = $("#chat-masthead");
  const name = state.chat.collection;
  const convo = state.conversations.find(c => c.id === state.chat.conversationId);
  if (!name || !convo) { head.classList.add("hidden"); head.innerHTML = ""; return; }
  head.classList.remove("hidden");
  const col = chatCollection();
  const hue = col ? themedColor(col.color) : courseFallback();
  let alert = "";
  if (col && col.assist_level === "off") alert = `<span class="m-alert bad">ASSIST OFF — ASKS REFUSED</span>`;
  else if (col && col.assist_level === "explain_only") alert = `<span class="m-alert warn">EXPLAIN ONLY</span>`;
  head.innerHTML = `<span class="m-tick" style="background:${hue}"></span>
    <span class="m-col">${escapeHtml(name === "all" ? "Everything" : name)}</span>
    <span>·</span><span class="m-title">${escapeHtml(convo.title)}</span>${alert}`;
}

function composerSync() {
  const col = chatCollection();
  const input = $("#composer-input");
  $("#composer").style.setProperty("--comp-hue", chatHue());
  if (col && col.assist_level === "off") {
    input.disabled = true;
    input.placeholder = "Assist is off for this collection — asks are refused";
  } else {
    input.disabled = false;
    input.placeholder = state.chat.collection && state.chat.collection !== "all"
      ? `Ask ${state.chat.collection}…` : "Ask across all collections…";
  }
}

function renderChatEmpty() {
  const box = $("#chat-messages");
  $("#chat-masthead").classList.add("hidden");
  const name = state.chat.collection;
  if (name && name !== "all") {
    const col = chatCollection();
    const hue = col ? themedColor(col.color) : courseFallback();
    const count = state.conversations.filter(c => c.collection === name).length;
    const assist = col ? (col.assist_level === "full" ? "Full assist"
      : col.assist_level === "explain_only" ? "Explain only" : "Assist off") : "";
    box.innerHTML = "";
    const page = document.createElement("div");
    page.className = "title-page";
    page.innerHTML = `
      <h2><span class="tp-rule" style="background:${hue}"></span>${escapeHtml(name)}</h2>
      <div class="tp-sub">${escapeHtml(assist)} · ${plural(count, "conversation")}</div>
      <div class="tp-label">Start with</div>`;
    for (const [key, tpl] of Object.entries(QUICK_ACTIONS)) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "tp-row";
      const label = { guide: "Study guide", flashcards: "Flashcards", quiz: "Practice quiz", diagram: "Diagram it", summary: "Summarize" }[key];
      row.innerHTML = `<span class="tp-name">${label}</span>
        <span class="tp-desc">${escapeHtml(tpl.split(".")[0])}.</span>`;
      row.addEventListener("click", () => applyQuickAction(key));
      page.appendChild(row);
    }
    box.appendChild(page);
    return;
  }
  // Desk sheet: every conversation, or the pick-a-collection hint.
  box.innerHTML = "";
  const page = document.createElement("div");
  page.className = "title-page";
  const convos = state.conversations;
  const h = document.createElement("h2");
  h.textContent = convos.length ? "Conversations" : "Pick a collection";
  page.appendChild(h);
  if (convos.length) {
    const wrap = document.createElement("div");
    wrap.style.marginTop = "20px";
    for (const c of convos) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "tp-row";
      const hue = (() => {
        const col = state.collections.find(x => x.name === c.collection);
        return col ? themedColor(col.color) : courseFallback();
      })();
      row.innerHTML = `<span class="ctick" style="background:${hue}"></span>
        <span class="code">${escapeHtml(c.collection === "all" ? "All" : c.collection)}</span>
        <span class="tp-desc">${escapeHtml(c.title)}</span>`;
      row.addEventListener("click", () => selectConversation(c.id));
      wrap.appendChild(row);
    }
    page.appendChild(wrap);
  }
  const hint = document.createElement("div");
  hint.className = "tp-hint";
  hint.textContent = "n new conversation · esc stops a streaming answer · shift+enter for a new line";
  page.appendChild(hint);
  box.appendChild(page);
}

async function selectChatCollection(name) {
  if (blockedByStream()) return;
  state.chat.collection = name;
  state.chat.conversationId = null;
  if (state.chat.pending.length) {
    state.chat.pending = [];
    renderPending();
    toast("Pending attachments were cleared when you switched collections.");
  }
  await fetchConversations();
  renderRailTree();
  composerSync();
  renderChatEmpty();
}

async function selectConversation(id) {
  if (blockedByStream()) return;
  state.chat.conversationId = id;
  const box = $("#chat-messages");
  box.innerHTML = "";
  box.appendChild(thinkingEl("Loading conversation…"));
  let detail;
  try { detail = await api(`/api/conversations/${id}`); }
  catch (e) {
    box.innerHTML = `<div class="notice notice-danger">${escapeHtml(e.message)}</div>`;
    return;
  }
  // A conversation opened from the all-conversations list belongs to a
  // collection that may not be the selected one - adopt it so the masthead,
  // rail highlight, and the next question's scope all match the thread.
  if (detail.conversation && detail.conversation.collection
      && detail.conversation.collection !== state.chat.collection) {
    state.chat.collection = detail.conversation.collection;
  }
  await fetchConversations();
  renderRailTree();
  composerSync();
  renderChatMasthead();
  box.innerHTML = "";
  for (const m of detail.messages) {
    appendMessage(m.role, m.content, { citations: m.citations, model: m.model, final: true });
  }
  const scroll = $("#chat-scroll");
  scroll.scrollTop = scroll.scrollHeight;
  $("#composer-input").focus();
}

function appendMessage(role, content, { citations = [], model = null, final = false, images = [] } = {}) {
  const box = $("#chat-messages");
  const msg = document.createElement("div");
  msg.className = `msg msg-${role}`;
  // Only NEW turns animate in; reopened history (final: true) sits settled,
  // and the marker is removed so tab flips can never replay the entrance.
  if (!final) {
    msg.classList.add("msg-new");
    setTimeout(() => msg.classList.remove("msg-new"), 450);
  }
  const body = document.createElement("div");
  body.className = "msg-body";
  msg.appendChild(body);
  if (role === "user") {
    if (String(content).length > 200) msg.classList.add("long");
    msg.style.setProperty("--turn-hue", chatHue());
    if (images.length) {
      const strip = document.createElement("div");
      strip.className = "msg-attachments";
      for (const url of images) {
        const im = document.createElement("img");
        im.src = url;
        im.alt = "attached image";
        strip.appendChild(im);
      }
      msg.insertBefore(strip, body);
    }
    if (content) {
      const t = document.createElement("div");
      t.className = "msg-text";
      t.textContent = content;
      body.appendChild(t);
    }
  } else {
    renderMarkdownInto(body, content, { streaming: !final });
    if (final) {
      const foot = buildFootnote({ citations, model, getText: content ? () => content : null });
      if (foot) msg.appendChild(foot);
    }
  }
  box.appendChild(msg);
  const scroll = $("#chat-scroll");
  scroll.scrollTop = scroll.scrollHeight;
  return { msg, body };
}

/* =============================================== image attachments (chat)
   Screenshots are downscaled in the browser before upload: the long edge is
   capped so a 4K grab does not become a multi-MB base64 payload. Screenshots
   stay PNG (crisp text); other images re-encode as JPEG. */

const MAX_IMAGE_EDGE = 1568;   // Anthropic's recommended max; larger is wasted
const MAX_PENDING = 8;         // mirrors the server's MAX_IMAGES

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("bad image")); };
    img.src = url;
  });
}

async function toAttachment(file) {
  const img = await loadImage(file);
  const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  canvas.getContext("2d").drawImage(img, 0, 0, w, h);
  const png = file.type === "image/png";
  const mediaType = png ? "image/png" : "image/jpeg";
  const dataUrl = canvas.toDataURL(mediaType, png ? undefined : 0.9);
  const data = dataUrl.slice(dataUrl.indexOf(",") + 1);
  return { media_type: mediaType, data, dataUrl };
}

async function addPendingImages(fileList) {
  const files = [...fileList].filter(f => f.type.startsWith("image/"));
  if (!files.length) return;
  for (const f of files) {
    if (state.chat.pending.length >= MAX_PENDING) {
      toast(`You can attach up to ${MAX_PENDING} images per message.`);
      break;
    }
    try {
      state.chat.pending.push(await toAttachment(f));
    } catch {
      toast(`Could not read image: ${f.name || "clipboard image"}`, { danger: true });
    }
  }
  renderPending();
}

function renderPending() {
  const box = $("#composer-previews");
  box.innerHTML = "";
  const items = state.chat.pending;
  box.classList.toggle("hidden", items.length === 0);
  items.forEach((att, i) => {
    const chip = document.createElement("div");
    chip.className = "att-thumb";
    const im = document.createElement("img");
    im.src = att.dataUrl; im.alt = "attachment";
    const rm = document.createElement("button");
    rm.type = "button"; rm.className = "att-remove"; rm.textContent = "×";
    rm.title = "Remove";
    rm.addEventListener("click", () => {
      state.chat.pending.splice(i, 1);
      renderPending();
    });
    chip.appendChild(im); chip.appendChild(rm);
    box.appendChild(chip);
  });
}

/* Quick-action templates. Seed the composer; never auto-send. */
const QUICK_ACTIONS = {
  guide: "Make a thorough study guide from this: the key concepts, definitions, formulas, and the connections between them, organized so I can review fast.",
  flashcards: "Turn this into flashcards. Give a Markdown table with a Question column and an Answer column, one row per card, covering the important points.",
  quiz: "Write a short practice quiz on this (a mix of multiple-choice and short-answer). Put the answer key at the very end under a separate heading.",
  diagram: "Explain this and include a clear diagram of how the pieces relate, as an SVG.",
  summary: "Summarize this concisely, then list the three things most likely to be tested.",
};

function applyQuickAction(kind) {
  const tpl = QUICK_ACTIONS[kind];
  if (!tpl) return;
  const input = $("#composer-input");
  input.value = input.value.trim() ? `${input.value.trim()}\n\n${tpl}` : tpl;
  input.dispatchEvent(new Event("input"));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function initChat() {
  $("#attach-btn").addEventListener("click", () => $("#attach-input").click());
  $("#attach-input").addEventListener("change", e => {
    addPendingImages(e.target.files);
    e.target.value = "";   // allow re-picking the same file
  });

  const input = $("#composer-input");
  input.addEventListener("paste", e => {
    const imgs = [...(e.clipboardData?.items || [])]
      .filter(it => it.kind === "file" && it.type.startsWith("image/"))
      .map(it => it.getAsFile())
      .filter(Boolean);
    if (imgs.length) { e.preventDefault(); addPendingImages(imgs); }
  });

  const drop = $("#chat-main");
  const over = e => { e.preventDefault(); drop.classList.add("drag-over"); };
  const leave = () => drop.classList.remove("drag-over");
  drop.addEventListener("dragover", over);
  drop.addEventListener("dragleave", e => { if (e.target === drop) leave(); });
  drop.addEventListener("drop", e => {
    e.preventDefault(); leave();
    if (e.dataTransfer?.files?.length) addPendingImages(e.dataTransfer.files);
  });

  $("#composer-quick").addEventListener("click", e => {
    const btn = e.target.closest("[data-qa]");
    if (btn) applyQuickAction(btn.dataset.qa);
  });
  $("#model-btn").addEventListener("click", cycleModel);

  const composer = $("#composer");
  composer.addEventListener("submit", async e => {
    e.preventDefault();
    if (state.chat.streaming) {
      if (state.chat.abort) state.chat.abort.abort();
      return;
    }
    await sendChatMessage();
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!state.chat.streaming) sendChatMessage();
    }
    if (e.key === "Escape" && state.chat.streaming && state.chat.abort) {
      state.chat.abort.abort();
    }
  });
  input.addEventListener("focus", () => composer.classList.add("focused"));
  input.addEventListener("blur", () => composer.classList.remove("focused"));
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight + 2, 176)}px`;
    $("#composer-send").classList.toggle("ready", input.value.trim().length > 0);
  });

  // n = new conversation in the current scope (chat tab only, not typing).
  document.addEventListener("keydown", e => {
    if (e.key !== "n" || isTypingContext() || paletteIsOpen()) return;
    if (!$("#tab-chat").classList.contains("active")) return;
    if (!state.chat.collection) { toast("Pick a collection first."); return; }
    newConversation(state.chat.collection === "all" ? "all" : state.chat.collection);
  });
}

async function sendChatMessage() {
  if (state.chat.streaming) return;
  const input = $("#composer-input");
  const q = input.value.trim();
  const attachments = state.chat.pending;
  if (!q && !attachments.length) return;
  if (!state.chat.collection) { toast("Pick a collection first."); return; }
  // Claim the lock BEFORE the await below: creating the conversation is a
  // round trip, and a second Enter during it would otherwise start a second
  // conversation and a second stream.
  state.chat.streaming = true;
  const send = $("#composer-send");
  const composer = $("#composer");
  const setBusy = busy => {
    send.textContent = busy ? "Esc Stop" : "Send ↵";
    send.classList.toggle("stop", busy);
    composer.classList.toggle("busy", busy);
    composer.setAttribute("aria-busy", busy ? "true" : "false");
    $("#attach-btn").disabled = busy;
  };
  setBusy(true);
  const collection = state.chat.collection;
  try {
    if (!state.chat.conversationId) {
      const convo = await api("/api/conversations", {
        method: "POST", body: { collection },
      });
      state.chat.conversationId = convo.id;
      fetchConversations().then(renderRailTree);
    }
  } catch (e) {
    state.chat.streaming = false;
    setBusy(false);
    toast(`Could not start a conversation: ${e.message}`, { danger: true });
    return;
  }
  const conversationId = state.chat.conversationId;
  const images = attachments.map(a => ({ media_type: a.media_type, data: a.data }));
  const thumbs = attachments.map(a => a.dataUrl);
  state.chat.pending = [];
  renderPending();
  input.value = "";
  input.dispatchEvent(new Event("input"));

  // Clear any title-page placeholder before the thread starts.
  $$("#chat-messages > .title-page").forEach(el => el.remove());
  renderChatMasthead();

  appendMessage("user", q, { images: thumbs });
  const { msg, body } = appendMessage("assistant", "", {});
  let thinking = thinkingEl();
  body.appendChild(thinking);
  const clearThinking = () => {
    if (!thinking) return;
    const t = thinking; thinking = null;
    if (reducedMotion()) { t.remove(); return; }
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 160);
  };
  let text = "";
  let citations = [];
  let model = null;
  let finalized = false;
  const scroll = $("#chat-scroll");
  const nearBottom = () =>
    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
  state.chat.abort = new AbortController();
  try {
    await streamAsk(`/api/conversations/${conversationId}/ask`,
      { question: q, model: state.model, images }, {
        meta(m) {
          citations = m.citations;
          model = m.model;
          renderNotices(msg, m.notices, body);
        },
        delta(d) {
          clearThinking();
          const stick = nearBottom();
          text += d.text;
          renderMarkdownInto(body, text, { streaming: true });
          if (stick) scroll.scrollTop = scroll.scrollHeight;
        },
        refusal(r) {
          clearThinking();
          body.remove();
          const fix = storeSyncNotice(r);
          if (fix) { msg.appendChild(fix); return; }
          const n = document.createElement("div");
          n.className = "notice " + (r.reason === "assist_blocked" ? "notice-danger" : "notice-warn");
          n.textContent = r.collections && r.collections.length
            ? `Blocked by: ${r.collections.join(", ")}. ${r.detail}` : r.detail;
          msg.appendChild(n);
        },
        error(err) {
          clearThinking();
          if (!text) body.remove();
          const n = document.createElement("div");
          n.className = "notice notice-danger";
          n.textContent = `Error: ${err.detail}`;
          msg.appendChild(n);
        },
        done() {
          clearThinking();
          finalized = true;
          renderMarkdownInto(body, text);
          const foot = buildFootnote({ citations, model, getText: text ? () => text : null });
          if (foot) msg.appendChild(foot);
        },
      }, state.chat.abort.signal);
  } catch (err) {
    clearThinking();
    if (err && err.name === "AbortError") {
      if (!text) body.remove();
      const foot = document.createElement("div");
      foot.className = "msg-foot";
      foot.innerHTML = `<span class="cites">stopped</span>`;
      msg.appendChild(foot);
    } else {
      if (!text) body.remove();
      const n = document.createElement("div");
      n.className = "notice notice-danger";
      n.textContent = `Request failed: ${err.message}`;
      msg.appendChild(n);
    }
  } finally {
    // A half-rendered '*rendering svg...*' placeholder must not be left
    // behind if the stream died mid-fence (done() already rendered final).
    clearThinking();
    if (!finalized && text && body.isConnected) renderMarkdownInto(body, text);
    state.chat.streaming = false;
    state.chat.abort = null;
    setBusy(false);
    fetchConversations().then(() => { renderRailTree(); renderChatMasthead(); });
  }
}

/* ============================================================== library */

function homeShorten(p) {
  return String(p).replace(/^[A-Za-z]:\\Users\\[^\\]+/, "~");
}

async function loadLibrary() {
  const ledger = $("#lib-ledger");
  if (!ledger.childElementCount) {
    ledger.innerHTML = Array.from({ length: 3 }, () =>
      `<div class="lib-row"><div class="skeleton" style="height:17px;width:40%"></div>
       <div class="skeleton" style="height:13px;width:65%;margin-top:8px"></div></div>`).join("");
  }
  let data;
  try { data = await api("/api/library"); }
  catch (e) {
    ledger.innerHTML = `<div class="notice notice-danger">Failed to load: ${escapeHtml(e.message)}</div>`;
    announce("Library failed to load: " + e.message);
    return;
  }
  state.library = data;
  refreshPip();
  renderLibrary(data);
}

function renderLibrary(data) {
  const cs = data.collections || [];
  const chunks = cs.reduce((n, c) => n + c.chunk_count, 0);
  const docs = cs.reduce((n, c) => n + c.doc_count, 0);

  // ---- colophon statement
  $("#lib-statement").innerHTML = `
    <span class="fig" data-count="${chunks}"></span><span class="word">chunks from</span>
    <span class="fig" data-count="${docs}"></span><span class="word">documents in</span>
    <span class="fig" data-count="${cs.length}"></span><span class="word">collections</span>`;
  const libEntering = isEntering("#tab-library");
  $$("#lib-statement .fig").forEach(el =>
    animateCount(el, Number(el.dataset.count), { animate: libEntering }));

  // ---- health sentence
  const health = $("#lib-health");
  health.innerHTML = "";
  const failing = cs.filter(c => c.failures.length);
  const missing = cs.filter(c => c.missing_roots.length);
  const never = cs.filter(c => !c.last_indexed);
  const link = name => {
    const a = document.createElement("button");
    a.type = "button";
    a.className = "link-btn";
    a.textContent = name;
    a.addEventListener("click", () => {
      const row = $(`#lib-row-${CSS.escape(name)}`);
      if (!row) return;
      row.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
      row.classList.add("flash");
      setTimeout(() => row.classList.remove("flash"), 2000);
    });
    return a;
  };
  if (!failing.length && !missing.length && !never.length) {
    const oldest = cs.reduce((m, c) => c.last_indexed
      ? Math.max(m, Date.now() - new Date(c.last_indexed).getTime()) : m, 0);
    health.textContent = `Everything indexed · oldest pass ${oldest ? fmtRelative(new Date(Date.now() - oldest).toISOString()) : "just now"}`;
  } else {
    const bits = [];
    if (failing.length) {
      const n = failing.reduce((x, c) => x + c.failures.length, 0);
      const s = document.createElement("span");
      s.innerHTML = `<span class="warn">${plural(n, "parse failure")}</span> in `;
      failing.forEach((c, i) => {
        s.appendChild(link(c.name));
        if (i < failing.length - 1) s.appendChild(document.createTextNode(", "));
      });
      bits.push(s);
    }
    if (missing.length) {
      const s = document.createElement("span");
      s.innerHTML = `<span class="bad">${plural(missing.reduce((x, c) => x + c.missing_roots.length, 0), "root")} missing</span>`;
      bits.push(s);
    }
    if (never.length) {
      const s = document.createElement("span");
      s.innerHTML = `<span class="warn">${plural(never.length, "collection")} never indexed</span>`;
      bits.push(s);
    }
    bits.forEach((b, i) => {
      health.appendChild(b);
      if (i < bits.length - 1) health.appendChild(document.createTextNode(" · "));
    });
  }

  // ---- shelf strip
  const shelf = $("#lib-shelf");
  shelf.innerHTML = "";
  const total = Math.max(1, chunks);
  for (const c of cs) {
    const seg = document.createElement("span");
    seg.style.background = themedColor(c.color);
    seg.style.flexGrow = String(Math.max(c.chunk_count / total, 0.008));
    seg.tabIndex = 0;
    seg.setAttribute("role", "img");
    const share = (100 * c.chunk_count / total).toFixed(1);
    seg.setAttribute("aria-label", `${c.name}: ${c.chunk_count.toLocaleString()} chunks, ${share}%`);
    const html = `<div class="tip-head">${escapeHtml(c.name)}</div>`
      + `<div class="tip-row"><span>chunks</span><span class="tip-val">${c.chunk_count.toLocaleString()}</span></div>`
      + `<div class="tip-row"><span>share</span><span class="tip-val">${share}%</span></div>`;
    seg.addEventListener("mousemove", e => showTip(html, e));
    seg.addEventListener("mouseleave", hideTip);
    seg.addEventListener("focus", () => {
      const r = seg.getBoundingClientRect();
      showTip(html, { clientX: r.left + r.width / 2, clientY: r.top });
    });
    seg.addEventListener("blur", hideTip);
    seg.dataset.name = c.name;
    seg.addEventListener("mouseenter", () => {
      const row = $(`#lib-row-${CSS.escape(c.name)}`);
      if (row) row.classList.add("lit");
    });
    seg.addEventListener("mouseleave", () => {
      const row = $(`#lib-row-${CSS.escape(c.name)}`);
      if (row) row.classList.remove("lit");
    });
    seg.addEventListener("click", () => {
      hideTip();
      const row = $(`#lib-row-${CSS.escape(c.name)}`);
      if (row) row.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
    });
    shelf.appendChild(seg);
  }
  drawStrip(shelf, isEntering("#tab-library"));

  // ---- ledger rows
  const ledger = $("#lib-ledger");
  ledger.innerHTML = "";
  if (!cs.length) {
    ledger.innerHTML = `<div class="empty-line">No collections yet. Add collections in config.toml to index your course material.</div>`;
  }
  const dayMs = 24 * 3600 * 1000;
  const totalChunks = cs.reduce((n2, c2) => n2 + (c2.chunk_count || 0), 0);
  for (const c of cs) {
    const row = document.createElement("div");
    row.className = "lib-row";
    row.id = `lib-row-${c.name}`;
    // Compared by equality, never interpolated into a selector: the pulse
    // targets rows without a name from the server ever reaching querySelector.
    row.dataset.col = c.name;
    row.addEventListener("mouseenter", () => {
      const shelf2 = $("#lib-shelf");
      const seg2 = shelf2 && shelf2.querySelector(`[data-name="${CSS.escape(c.name)}"]`);
      if (seg2) { seg2.classList.add("lit"); shelf2.classList.add("has-lit"); }
    });
    row.addEventListener("mouseleave", () => {
      const shelf2 = $("#lib-shelf");
      const seg2 = shelf2 && shelf2.querySelector(`[data-name="${CSS.escape(c.name)}"]`);
      if (seg2) seg2.classList.remove("lit");
      if (shelf2) shelf2.classList.remove("has-lit");
    });
    const hue = themedColor(c.color);
    const assist = c.assist_level !== "full"
      ? `<span class="lib-assist">${c.assist_level === "explain_only" ? "explain only" : "assist off"}</span>` : "";

    let aged = "";
    if (!c.last_indexed) aged = `<span class="never">not indexed yet</span>`;
    else {
      const age = Date.now() - new Date(c.last_indexed).getTime();
      const cls = age > 7 * dayMs ? "stale" : age > dayMs ? "aged" : "";
      aged = `<span class="${cls}">indexed ${escapeHtml(fmtRelative(c.last_indexed))}</span>`;
    }
    const missingBit = c.missing_roots.length
      ? `<span class="missing">${plural(c.missing_roots.length, "root")} missing</span>` : "";
    const failBit = c.failures.length ? `
      <details><summary class="fail-sum">${plural(c.failures.length, "parse failure")}</summary>
        <div class="lib-drawer warn">${c.failures.map(f =>
          `<div class="fail-path">${escapeHtml(f.path)}</div><div class="fail-reason">${escapeHtml(f.reason)}</div>`).join("")}
        </div></details>` : "";
    const rootsBit = `
      <details><summary>${plural(c.roots.length, "root")}</summary>
        <div class="lib-drawer" style="--drawer-hue:${hue}">${c.roots.map(r => {
          const miss = c.missing_roots.includes(r);
          return `<div class="root" title="${escapeHtml(r)}">${miss ? '<span class="missing">MISSING: </span>' : ""}${escapeHtml(homeShorten(r))}</div>`;
        }).join("")}
        <div class="root lib-result-line" data-lastrun="${escapeHtml(c.name)}"></div></div></details>`;

    row.innerHTML = `
      <span class="spine" style="background:${hue}"></span>
      <div class="lib-l1">
        <span class="lib-name">${escapeHtml(c.name)}</span>${assist}
        <span class="sp"></span>
        <span class="lib-fig"><b>${c.doc_count.toLocaleString()}</b> <span>docs</span></span>
        <span class="lib-fig wide"><b>${c.chunk_count.toLocaleString()}</b> <span>chunks</span>
          <i class="lib-meter" style="--w:${totalChunks ? Math.round(1000 * (c.chunk_count || 0) / totalChunks) / 10 : 0};background:${themedColor(c.color)}"></i></span>
      </div>
      <div class="lib-l2">
        ${aged}${missingBit}${failBit}${rootsBit}
        <span class="sp"></span>
        <span class="lib-result-line" data-result-for="${escapeHtml(c.name)}"></span>
        <button type="button" class="text-action quiet" data-reindex="${escapeHtml(c.name)}">Reindex</button>
      </div>`;
    ledger.appendChild(row);
  }
  $$("[data-reindex]").forEach(btn =>
    btn.addEventListener("click", () => startIndexRun(btn.dataset.reindex)));
  /* Paint state is DERIVED, never owned by a node. This rebuild destroys
     every row, so the live marks, the disabled controls and this session's
     results are re-applied from state - which is also why nothing in the
     run path holds a node across a poll tick. */
  for (const [name, text] of state.index.results) {
    const res = $(`[data-result-for="${CSS.escape(name)}"]`);
    if (res) res.textContent = text;
    const last = $(`[data-lastrun="${CSS.escape(name)}"]`);
    if (last) last.textContent = `last run: ${text}`;
  }
  paintIndexRows();

  // ---- calendar chapter
  const lead = $("#lib-cal-lead");
  const box = $("#library-calendar");
  box.innerHTML = "";
  const st = data.calendar_status;
  if (st) {
    const errs = st.sources.flatMap(s => s.errors.map(e => `${basename(s.detail)}: ${e}`));
    lead.innerHTML = `<b>${(st.total_stored ?? st.total_imported).toLocaleString()}</b> events on file · imported ${escapeHtml(fmtRelative(st.imported_at))}`
      + (errs.length ? ` · <span class="bad">${plural(errs.length, "import error")}</span>` : "");
  } else {
    lead.innerHTML = `<span class="muted">No import recorded yet</span>`;
  }
  if (!data.calendar_sources.length) {
    box.innerHTML = `<div class="empty-line">No calendar configured.</div>`;
  }
  const statusFor = src => {
    if (!st) return null;
    return st.sources.find(s => s.detail === src.path)
      || st.sources.find(s => basename(s.detail) === basename(src.path)) || null;
  };
  for (const s of data.calendar_sources) {
    const row = document.createElement("div");
    row.className = "cal-src";
    const joined = statusFor(s);
    let figs = "";
    if (joined && (joined.imported != null || joined.stored != null)) {
      const f = [];
      if (joined.imported != null) f.push(`${joined.imported} imported`);
      if (joined.stored != null) f.push(`${joined.stored} stored`);
      figs = f.join(" · ");
    }
    const errCount = joined ? joined.errors.length : 0;
    row.innerHTML = `
      <span class="type">${escapeHtml(s.type)}</span>
      <span class="path" title="${escapeHtml(s.path)}">${escapeHtml(homeShorten(s.path))}</span>
      <span class="figs">${figs}${errCount ? `${figs ? " · " : ""}<span class="bad">${plural(errCount, "error")}</span>` : ""}</span>
      ${s.exists ? "" : `<span class="missing-word">MISSING</span>`}
      <span class="st ${s.exists && !errCount ? "ok" : "bad"}"></span>`;
    box.appendChild(row);
    if (errCount) {
      const drawer = document.createElement("div");
      drawer.className = "lib-warn-block";
      drawer.innerHTML = joined.errors.map(e => `<div>${escapeHtml(e)}</div>`).join("");
      box.appendChild(drawer);
    }
  }
  for (const src of (st && st.upsert_only) || []) {
    const warn = document.createElement("div");
    warn.className = "lib-warn-block";
    warn.textContent = `'${src}' was updated in place, not rebuilt: one of its inputs `
      + `failed, so existing ${src} events were kept rather than deleted. Anything `
      + `removed at the source may still be showing. Fix the errors and reimport.`;
    box.appendChild(warn);
  }
}

/* ------------------------------------------------------------ index run
   /api/index blocked the request for the whole run - twelve minutes on a
   real library, with a button reading "Indexing..." and nothing else, and
   every trace of it lost on a reload. These routes replace it.

   One poller, module-scope, started from init() rather than owned by the
   Library tab: the run is server-global, so a reload that lands on Today
   has to find it too and the completion has to reach whichever tab the user
   is on. A tick writes textContent and one numeric --w. It creates no node,
   calls no settle(), and touches no innerHTML, so THE ARRIVAL LAW holds by
   construction rather than by a guard someone can forget to keep. */

const INDEX_TICK_MS = 1200;         // "embedded N/M" lands every ~2.6s at the
                                    // measured ~50 chunks/s, so 1.2s never
                                    // lags a step, is under the ~2s "frozen"
                                    // threshold, and shares spine-pulse's beat
const INDEX_TICK_HIDDEN_MS = 5000;  // nobody is reading the clock
const INDEX_FAIL_LIMIT = 4;
const INDEX_START_GRACE = 4;        // ticks a start gets before we call it dead

/* Coupled to src/brain/indexer.py:146 (collection marker), :328 (embed
   total) and :346 (the fraction). `message` is lines[-1].strip(), so the
   embed tick arrives with its two leading spaces already gone. Every match
   is optional and anchored: a reworded line costs the bar, never a wrong
   number. */
const RE_INDEX_COL   = /^\[([^\]]{1,64})\]/;
const RE_INDEX_TOTAL = /^Embedding\s+(\d+)\s+chunks\b/;
const RE_INDEX_TICK  = /^embedded\s+(\d+)\s*\/\s*(\d+)\b/;

let _indexTimer = null;

function fmtDur(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return s < 60 ? `${s}s`
    : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/* Sticky by design. A checkpoint line ("saved progress (N vectors on disk)")
   lands every 2560 chunks and the run ends on "Embedding store: N vectors",
   so `message` is NOT a fraction for long stretches - a UI that believes
   only the current line blanks its own number once a minute. */
function readIndexStatus(st) {
  const ix = state.index;
  if (st.elapsed_sec != null) ix.elapsed = st.elapsed_sec;
  const msg = String(st.message || "");
  const col = RE_INDEX_COL.exec(msg);
  if (col) ix.curCol = col[1];
  const tick = RE_INDEX_TICK.exec(msg);
  if (tick) {
    const done = Number(tick[1]), total = Number(tick[2]);
    if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
      ix.embedDone = done;
      ix.embedTotal = total;
    }
  }
  const head = RE_INDEX_TOTAL.exec(msg);
  if (head && Number(head[1]) > 0) ix.embedTotal = Number(head[1]);
  if (ix.embedTotal) ix.phase = "embed";
  else if (msg) ix.phase = "scan";
}

function indexScopeLabel() { return state.index.scope || "all collections"; }

function indexCaption() {
  const ix = state.index;
  if (ix.phase === "embed") return `Embedding · ${indexScopeLabel()}`;
  const where = ix.scope || ix.curCol;
  return where ? `Reading files · ${where}` : "Starting…";
}

function paintIndexRun() {
  const band = $("#lib-run");
  if (!band) return;
  const ix = state.index;
  if (!ix.watching && !ix.result) {
    band.classList.add("hidden");
    band.dataset.shown = "";
    return;
  }
  // THE ARRIVAL LAW, in one line: the band settles when it APPEARS and never
  // again. The terminal state repaints it in place.
  const arriving = band.dataset.shown !== "1";
  band.dataset.shown = "1";
  band.classList.remove("hidden");
  band.classList.toggle("is-done", ix.result === "done");
  band.classList.toggle("is-bad", ix.result === "bad");
  band.setAttribute("aria-busy", ix.watching ? "true" : "false");

  /* The track exists only once the run has a real denominator. The scan
     phase has none - an unchanged file emits no line at all, so a count of
     indexed files measures work done, not progress - and a bar pinned at 0%
     for four minutes is a worse lie than no bar. */
  const track = $("#lib-run-track");
  const hasBar = ix.embedTotal > 0;
  // .is-blank, not .hidden: the row keeps its height so nothing below moves
  // when the embed phase finally supplies a denominator.
  track.classList.toggle("is-blank", !hasBar);
  if (hasBar) {
    const frac = Math.max(0, Math.min(1, ix.embedDone / ix.embedTotal));
    const pct = ix.result === "done" ? 100 : Math.round(1000 * frac) / 10;
    // The only inline style this feature writes, and it is a clamped,
    // rounded number - never server text.
    $("#lib-run-fill").style.setProperty("--w", String(pct));
    track.setAttribute("aria-valuenow", String(Math.round(pct)));
    track.setAttribute("aria-valuetext",
      `${ix.embedDone.toLocaleString()} of ${ix.embedTotal.toLocaleString()} chunks embedded`);
  }

  setText($("#lib-run-phase"), ix.result ? ix.line : indexCaption());
  setText($("#lib-run-count"), (!ix.result && ix.embedTotal)
    ? `${ix.embedDone.toLocaleString()} of ${ix.embedTotal.toLocaleString()} chunks`
    : "");
  setText($("#lib-run-clock"), fmtDur(ix.elapsed));
  if (arriving) settle(band);
}

/* Which rows are live. Derived from the SERVER's status.collection, never
   from parsing "[MATH210] indexed..." out of a progress line: the embed
   pass runs ONCE over every collection and names none of them, so a walking
   pulse would leave every row dark for the slow 90% of a full run. Every
   row is a true superset - all of these really are in this run. */
function paintIndexRows() {
  const ix = state.index;
  const busy = ix.watching || ix.starting;
  for (const row of $$("#lib-ledger .lib-row")) {
    const on = ix.watching && (!ix.scope || row.dataset.col === ix.scope);
    // Only touch the class when it CHANGES: re-adding a class does not
    // restart a running animation, but this keeps the invariant reviewable.
    if (row.classList.contains("indexing") !== on) row.classList.toggle("indexing", on);
    // Reduced motion kills spine-pulse, so the mark alone cannot carry the
    // live state. aria-busy and the band's ticking clock do.
    if (on) row.setAttribute("aria-busy", "true"); else row.removeAttribute("aria-busy");
  }
  // Disabled state is RENDERED from state, never applied as a side effect at
  // click time: renderLibrary() brings every button back enabled.
  const all = $("#reindex-all");
  if (all) all.disabled = busy;
  for (const b of $$("[data-reindex]")) b.disabled = busy;
}

function scheduleIndexTick(delay) {
  clearTimeout(_indexTimer);
  _indexTimer = setTimeout(indexTick, delay != null ? delay
    : (document.hidden ? INDEX_TICK_HIDDEN_MS : INDEX_TICK_MS));
}

/* The ONLY place watching goes false, and it always clears the timer. */
function stopIndexWatch() {
  clearTimeout(_indexTimer);
  _indexTimer = null;
  state.index.watching = false;
}

async function indexTick() {
  _indexTimer = null;
  const ix = state.index;
  if (!ix.watching) return;
  ix.ticks += 1;
  let st;
  // brief=1: lines[] is up to 400 entries (~24KB) on every one of several
  // hundred polls, and this UI renders only `message`.
  try { st = await api("/api/index/status?brief=1"); }
  catch {
    if (++ix.fails >= INDEX_FAIL_LIMIT) return finishIndexRun(null, "lost");
    scheduleIndexTick(ix.fails * 1500);   // the clock stops moving, which is
    return;                               // itself the "something is wrong" mark
  }
  if (!ix.watching) return;    // stopped while the fetch was open
  ix.fails = 0;

  if (st.running) {
    ix.sawRunning = true;
    ix.scope = st.collection || null;
    readIndexStatus(st);
    paintIndexRun();
    paintIndexRows();
    scheduleIndexTick();
    return;
  }
  if (st.done && st.error) return finishIndexRun(st, "failed");
  if (st.done) return finishIndexRun(st, "done");
  /* running:false AND done:false. On a restart _index_job is back at its
     initial literal, which reads EXACTLY like "never ran" - so this is the
     one state a naive port reports as success. If we ever saw the run
     alive, it died with the process. */
  if (ix.sawRunning) return finishIndexRun(st, "vanished");
  if (ix.ticks <= INDEX_START_GRACE) { paintIndexRun(); scheduleIndexTick(); return; }
  return finishIndexRun(st, "nostart");
}

function finishIndexRun(st, kind) {
  const ix = state.index;
  const scope = ix.scope;
  stopIndexWatch();
  ix.result = kind === "done" ? "done" : "bad";

  if (kind === "done") {
    const o = readIndexOutcome(st, scope);
    for (const [k, v] of o.results) ix.results.set(k, v);
    ix.line = o.line;
    if (st && st.elapsed_sec != null) ix.elapsed = st.elapsed_sec;
  } else if (kind === "failed") {
    // Already-stringified exception text, and it embeds file paths read off
    // disk. textContent only - it never reaches a markup sink. A bare
    // StopIteration stringifies as "StopIteration: " with nothing after it.
    const raw = String((st && st.error) || "").trim().replace(/:\s*$/, "");
    ix.line = `Reindex failed: ${raw || "the index run crashed"}`;
  } else if (kind === "vanished") {
    ix.line = "Command Center restarted before the index finished. The indexer "
      + "checkpoints as it embeds, so most of the work was saved - run it "
      + "again to finish.";
  } else if (kind === "lost") {
    ix.line = "Lost contact with Command Center. The index run may still be going.";
  } else {
    ix.line = "The index run did not start.";
  }

  paintIndexRun();
  paintIndexRows();
  announce(ix.line);
  // The band already says it. A toast is for the user who walked away.
  const onLibrary = $("#tab-library").classList.contains("active");
  if (!onLibrary || ix.result === "bad") {
    toast(ix.line, { danger: ix.result === "bad", duration: 7000 });
  }
  if (kind === "lost") return;
  if (kind === "done" || kind === "failed") getAnalytics({ fresh: true });
  // A repaint is not an arrival: without this, a run that lands inside the
  // Library tab's 2s entrance window replays rise-in on every ledger row.
  const panel = $("#tab-library");
  panel.classList.remove("entering");
  clearTimeout(panel._enterTimer);
  loadLibrary();               // exactly one rebuild per run
}

/* The outcome sentence and the per-row results, read off a terminal
   payload. Shared by the live finish and by the restore-after-reload path so
   the summing rule lives in exactly one place - it is NOT collections[0],
   which is right for a one-collection run and silently wrong for a report
   that carries seven. */
function readIndexOutcome(st, scope) {
  const cs = (st && st.report && st.report.collections) || [];
  const results = new Map();
  for (const c of cs) {
    results.set(c.collection, `${c.indexed} indexed, ${c.skipped} skipped, `
      + `${plural(c.failures.length, "failure")}`);
  }
  // done with no report is reachable: the blocking route sets done in its
  // finally and assigns report afterwards. Say what is true rather than
  // printing a fabricated "0 indexed, 0 skipped", which reads as a broken
  // index.
  if (!cs.length) return { line: "Index finished.", results };
  const sum = k => cs.reduce((a, c) =>
    a + (k === "failures" ? c.failures.length : (c[k] || 0)), 0);
  return {
    line: `${scope || "All collections"} · ${sum("indexed")} indexed, `
      + `${sum("skipped")} skipped, ${plural(sum("failures"), "failure")}`,
    results,
  };
}

/* A run that ended moments ago is still news: without this, a reload in the
   final seconds of a twelve-minute run loses the summary outright. This is
   only safe because the server now stamps finished_at and freezes
   elapsed_sec - before that, a completed payload could not be told from one
   left over from yesterday, so nothing was restored at all. Paints only: no
   toast, no refetch, no ledger rebuild, because none of that is arriving
   news on a page that just loaded. */
const INDEX_RESTORE_MAX_AGE = 120;   // seconds

function restoreFinishedRun(st) {
  const ix = state.index;
  ix.scope = st.collection || null;
  ix.result = st.error ? "bad" : "done";
  if (st.error) {
    const raw = String(st.error).trim().replace(/:\s*$/, "");
    ix.line = `Reindex failed: ${raw || "the index run crashed"}`;
  } else {
    const o = readIndexOutcome(st, ix.scope);
    for (const [k, v] of o.results) ix.results.set(k, v);
    ix.line = o.line;
  }
  ix.embedTotal = 0;                 // a summary carries no bar
  ix.elapsed = st.elapsed_sec || 0;
  paintIndexRun();
}

function beginIndexWatch(st, { mine }) {
  const ix = state.index;
  Object.assign(ix, {
    watching: true, starting: false, mine,
    scope: st.collection || null, curCol: null, phase: "start",
    result: "", line: "", sawRunning: !!st.running, ticks: 0, fails: 0,
    embedDone: 0, embedTotal: 0, elapsed: st.elapsed_sec || 0,
  });
  readIndexStatus(st);
  /* The start response can already be terminal: a collection with nothing to
     do finishes in milliseconds, and then there is no running->done edge
     left for a poller to wait for. Treat it as tick zero. */
  if (!st.running && (st.done || st.error)) {
    finishIndexRun(st, st.error ? "failed" : "done");
    return;
  }
  paintIndexRun();
  paintIndexRows();
  scheduleIndexTick();
}

async function startIndexRun(collection) {
  const ix = state.index;
  if (ix.watching || ix.starting) return;   // set synchronously: a second
  ix.starting = true;                       // click must not outrun the POST
  paintIndexRows();                         // acknowledge the click now
  let st;
  try {
    st = await api("/api/index/start",
      { method: "POST", body: collection ? { collection } : {} });
  } catch (e) {
    ix.starting = false;
    /* 409 is not a user error. The lock is server-global, so another window
       - or /api/sync/news/apply, which starts its own background index -
       already holds it. The user's question is "is my library indexing?"
       and the answer is yes, so we adopt that run instead of reporting a
       failure on a healthy server. */
    if (e.status === 409) {
      let live = null;
      try { live = await api("/api/index/status?brief=1"); } catch { /* really down */ }
      if (live && live.running) {
        beginIndexWatch(live, { mine: false });
        toast(live.collection && live.collection !== collection
          ? `${live.collection} is indexing already. ${collection || "A full reindex"} can run when it lands.`
          : "An index run is already going; watching it.");
        return;
      }
      paintIndexRows();
      toast("An index run was already in progress. Try again.");
      return;
    }
    paintIndexRows();
    // A fetch that never reached the server has no status at all.
    toast(e.status === undefined
      ? "Could not reach Command Center. Is the app still running?"
      : `Could not start the index: ${e.message}`, { danger: true });
    return;
  }
  if (collection) ix.results.delete(collection);
  beginIndexWatch(st, { mine: true });
}

/* The refusal that is really an offer. The server sends
   reason:"store_out_of_sync" with the collection to repair (null when the
   whole store is at fault, e.g. a corrupt manifest - then an empty body
   reindexes everything). We branch on `reason` only: `detail` ends in a
   CLI command, which is exactly what the person reading this cannot use.
   Returns null for every other reason so each call site keeps its own
   existing copy unchanged. */
function storeSyncNotice(r) {
  if (!r || r.reason !== "store_out_of_sync") return null;
  const n = document.createElement("div");
  n.className = "notice notice-warn";
  const what = r.collection ? `"${r.collection}"` : "your library";
  const say = document.createElement("span");
  // Composed here, not echoed from the server: says what is wrong, why it
  // happened, and what the button will do. No chunk counts - the number is
  // the server's diagnostic, not the reader's problem.
  say.textContent = `The search index for ${what} is incomplete, so an answer `
    + `now would miss your own material. This happens when the app is closed `
    + `while it is still indexing. `;
  n.appendChild(say);
  const fix = document.createElement("button");
  fix.type = "button";
  fix.className = "text-action";
  const busy = () => state.index.watching || state.index.starting;
  fix.textContent = busy() ? "Indexing now…" : "Rebuild the index";
  fix.disabled = busy();
  fix.addEventListener("click", () => {
    fix.disabled = true;
    fix.textContent = "Indexing now…";
    // Exactly the flow the Library tab uses, including its 409 adoption.
    startIndexRun(r.collection || null);
    toast("Indexing started - the Library tab shows progress.");
  });
  n.appendChild(fix);
  return n;
}

/* Attach to whatever the server is already doing. A live run is watched; a
   run that finished within the last two minutes is restored as a summary.
   The age check is the whole safety property - _index_job keeps done and
   report forever, so without finished_at every page load would open with
   whatever ran last, whenever that was. */
async function attachIndexRun() {
  const ix = state.index;
  if (ix.watching || ix.starting) return;
  let st;
  try { st = await api("/api/index/status?brief=1"); } catch { return; }
  if (st.running) { beginIndexWatch(st, { mine: false }); return; }
  if (st.done && st.finished_at != null
      && (Date.now() / 1000) - st.finished_at <= INDEX_RESTORE_MAX_AGE) {
    restoreFinishedRun(st);
  }
}

function initLibrary() {
  $("#reindex-all").addEventListener("click", () => startIndexRun(null));
  $("#reimport-btn").addEventListener("click", async () => {
    const btn = $("#reimport-btn");
    btn.disabled = true;
    $("#reimport-result").textContent = "Importing…";
    try {
      const rep = await api("/api/calendar/reimport", { method: "POST" });
      const errs = rep.sources.flatMap(s => s.errors.map(e => `${basename(s.detail)}: ${e}`));
      $("#reimport-result").textContent = errs.length
        ? `Import finished with ${plural(errs.length, "error")} `
          + `(${rep.total_stored ?? rep.total_imported} events stored):\n${errs.join("\n")}`
        : `Stored ${rep.total_stored ?? rep.total_imported} events.`;
      state.weekload = [];
      loadLibrary();
      getAnalytics({ fresh: true });
    } catch (e) {
      $("#reimport-result").textContent = `Reimport failed: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

/* ================================================================= init */

/* SVG charts are sized in pixels at draw time; redraw on resize. */
function initChartResize() {
  let timer = null;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const active = $(".tab-panel.active");
      if (!active) return;
      if (active.id === "tab-analytics" && state.analytics) {
        // A resize repaint is not an arrival either.
        active.classList.remove("entering");
        clearTimeout(active._enterTimer);
        renderAnalytics(state.analytics);
      }
    }, 180);
  });
}

/* Ledger-row hover lights that course's segments across the 28-day ribbon.
   Delegated and attached exactly ONCE - renderAnalytics re-runs on every
   sync poll and theme flip, so per-render listeners would stack. */
function initAnalyticsCross() {
  const ledger = $("#course-ledger");
  const ribbon = $("#an-ribbon");
  if (!ledger || !ribbon) return;
  ledger.addEventListener("mouseover", e => {
    const row = e.target.closest(".cl-row[data-course]");
    if (!row) return;
    ribbon.classList.add("course-spot");
    for (const seg of ribbon.querySelectorAll(".rb-stack > span")) {
      seg.classList.toggle("match", seg.dataset.course === row.dataset.course);
    }
  });
  ledger.addEventListener("mouseout", e => {
    if (e.target.closest(".cl-row") && !ledger.contains(e.relatedTarget)) {
      ribbon.classList.remove("course-spot");
    } else if (!e.relatedTarget || !e.relatedTarget.closest(".cl-row")) {
      ribbon.classList.remove("course-spot");
    }
  });
}

async function init() {
  initTheme();
  initTabs();
  initPalette();
  initAskAnything();
  initAnalyticsCross();
  initChartResize();
  initCalendarControls();
  initChat();
  initLibrary();
  renderDateline();

  $("#pip-btn").addEventListener("click", openStatusPop);
  $("#sync-pill").addEventListener("click", openSyncPop);

  let st;
  try { st = await api("/api/state"); }
  catch (e) {
    document.body.insertAdjacentHTML("afterbegin",
      `<div class="notice notice-danger">Failed to load app state: ${escapeHtml(e.message)}</div>`);
    announce("Failed to load app state: " + e.message);
    return;
  }
  state.collections = st.collections;
  state.models = st.models;
  state.defaultModel = st.default_model;
  state.user = st.user || state.user;
  let savedModel = null;
  try { savedModel = localStorage.getItem("cc-model"); } catch { /* private mode */ }
  state.model = state.models.includes(savedModel) ? savedModel : state.defaultModel;
  renderPaletteModel();

  const warnings = [...st.warnings];
  // Only warn about what the CONFIGURED backend actually needs: on the
  // subscription backend an absent API key is the normal, intended state.
  state.backendProblem = (!st.backend_ready && st.backend_problem) ? st.backend_problem : null;
  if (state.backendProblem) warnings.push(state.backendProblem);
  refreshPip();
  if (state.backendProblem) pipEl().title = state.backendProblem;
  if (warnings.length) {
    const box = $("#config-warnings");
    box.innerHTML = "";
    const text = document.createElement("div");
    text.className = "warn-text";
    text.textContent = warnings.join("\n");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "warn-close";
    close.textContent = "×";
    close.title = "Dismiss";
    close.setAttribute("aria-label", "Dismiss warnings");
    close.addEventListener("click", () => box.classList.add("hidden"));
    box.appendChild(text);
    box.appendChild(close);
    box.classList.remove("hidden");
    announce(warnings.join(". "));
  }

  await fetchConversations();
  composerSync();
  pollSync();
  setInterval(pollSync, 5 * 60 * 1000);
  // Unconditional, and NOT tied to the Library tab: the index job is
  // server-global, so a reload that lands on Today has to find a run in
  // progress too, and the completion has to reach whichever tab is open.
  attachIndexRun();
  window.addEventListener("pagehide", () => { stopIndexWatch(); stopSyncWatch(); });
  document.addEventListener("visibilitychange", () => {
    // Background tabs get throttled to a tick a minute (or frozen). Catch up
    // the moment the user looks again; never rely on the interval for the
    // correctness of a terminal transition.
    if (document.hidden) return;
    if (state.index.watching) scheduleIndexTick(0);
    if (state.syncWatch.watching) scheduleSyncTick(0);
  });
  showTab(location.hash.slice(1) || "today", { updateHash: false });
  requestAnimationFrame(positionTabIndicator);
}

init();

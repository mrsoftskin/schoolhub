/* Chart toolkit for Command Center.

   Hand-rolled SVG, no library. Every mark follows one spec so the charts read
   as one system: bars capped at 24px with a 4px rounded data-end and a square
   baseline, a 2px surface gap between touching marks, hairline recessive
   gridlines, and text in ink tokens rather than the series color — identity
   comes from a swatch beside the text, never from coloring the text.

   Colors come from the caller (course hues from config.toml), except the
   single-series sequential blue, which is the one hue used for magnitude. */

"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

const CHART = {
  maxBarThickness: 22,
  barRadius: 4,
  gap: 2,               // surface gap between touching marks
};

/* Colors are read from the CSS custom properties AT DRAW TIME, never cached
   at load: the theme toggle swaps the tokens on <html>, and the next redraw
   (app.js re-renders the active tab on toggle) picks up the new set.
   Gridlines and labels restyle through their CSS classes, so only the
   colors that land in fill/stroke attributes are read here. The fallbacks
   are light-theme literals; a renamed token would silently paint light
   colors on a dark page, hence the one-time console warning. */
const _warnedTokens = new Set();
function chartTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => {
    const val = (cs.getPropertyValue(name) || "").trim();
    if (!val && !_warnedTokens.has(name)) {
      _warnedTokens.add(name);
      console.warn(`chartTheme: token ${name} is missing; falling back to ${fallback}`);
    }
    return val || fallback;
  };
  return {
    ink: v("--chart-ink", "#16181d"),
    seq: v("--seq", "#2a78d6"),
  };
}

/* Text set INSIDE a colored fill picks white or ink by that fill's luminance,
   so a light course hue (yellow, aqua) never carries white text. Everywhere
   else, labels wear ink tokens and identity comes from a swatch beside them.
   The pair is DELIBERATELY theme-independent: contrast is computed against
   the fill, not the page, so a light-yellow pill keeps dark text in dark
   mode too. Do not swap these for --text tokens. */
function inkOn(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  const L = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  // Contrast against white vs against near-black; pick the better one.
  return (1.05 / (L + 0.05)) >= ((L + 0.05) / 0.05) ? "#ffffff" : "#16181d";
}

function el(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

function clear(svg) {
  if (svg && svg.classList) svg.classList.remove("spot");
  // Re-rendering under the cursor removes the hit rect whose mouseleave
  // would have hidden the tooltip; hide it here so it can never go stale.
  hideTip();
  while (svg.firstChild) svg.removeChild(svg.firstChild);
}

/* A rect with rounded corners only at the data end, square at the baseline.
   Drawn as a path because SVG rx rounds all four corners. */
function barPath(x, y, w, h, r, orient = "up") {
  const rad = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0.5) return `M${x},${y + h} h${w}`;
  if (orient === "up") {
    return `M${x},${y + h} V${y + rad} Q${x},${y} ${x + rad},${y}`
         + ` H${x + w - rad} Q${x + w},${y} ${x + w},${y + rad}`
         + ` V${y + h} Z`;
  }
  // horizontal, growing right; rounded on the right edge
  const rr = Math.max(0, Math.min(r, h / 2, w));
  return `M${x},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr}`
       + ` V${y + h - rr} Q${x + w},${y + h} ${x + w - rr},${y + h}`
       + ` H${x} Z`;
}

/* ------------------------------------------------------------- tooltip */

const tip = () => document.getElementById("chart-tip");

function showTip(html, evt) {
  const t = tip();
  if (!t) return;
  t.innerHTML = html;
  t.classList.remove("hidden");
  const r = t.getBoundingClientRect();
  let x = evt.clientX + 14;
  let y = evt.clientY - r.height - 10;
  if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y < 8) y = evt.clientY + 16;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
}

function hideTip() {
  const t = tip();
  if (t) t.classList.add("hidden");
}

function tipRow(color, label, value) {
  const sw = color
    ? `<span class="legend-swatch" style="background:${color}"></span>` : "";
  return `<div class="tip-row">${sw}<span>${label}</span>`
       + `<span class="tip-val">${value}</span></div>`;
}

/* Attach hover to a mark, with a generous transparent hit area on top.
   With a plain-text label the mark is also keyboard-reachable (Tab focuses
   it and shows the tip) and tappable on touch, not mouse-only. */
function attachHover(svg, hit, html, label) {
  hit.addEventListener("mousemove", e => showTip(html, e));
  hit.addEventListener("mouseleave", hideTip);
  hit.addEventListener("click", e => showTip(html, e));
  if (label) {
    hit.setAttribute("tabindex", "0");
    hit.setAttribute("role", "img");
    hit.setAttribute("aria-label", label);
    hit.addEventListener("focus", () => {
      const r = hit.getBoundingClientRect();
      showTip(html, { clientX: r.left + r.width / 2, clientY: r.top });
    });
    hit.addEventListener("blur", hideTip);
  }
  svg.appendChild(hit);
}

/* ---------------------------------------------------- y-axis scaffolding */

function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  // Every chart here counts events: an axis labeled "0.5 deadlines" is
  // nonsense, so small maxima tick every integer instead of fractionally.
  if (max <= count) {
    const ticks = [];
    for (let v = 0; v <= max; v++) ticks.push(v);
    return ticks;
  }
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(Math.round(v * 100) / 100);
  return ticks;
}

function drawGrid(svg, { x0, x1, yOf, ticks }) {
  // Stroke comes from the .axis-line/.grid-line CSS classes (theme tokens),
  // so gridlines restyle with the theme like every other piece of chrome.
  for (const t of ticks) {
    const y = yOf(t);
    svg.appendChild(el("line", {
      x1: x0, x2: x1, y1: y, y2: y,
      class: t === 0 ? "axis-line" : "grid-line",
    }));
    const label = el("text", {
      x: x0 - 6, y: y + 3, "text-anchor": "end", class: "axis-label",
    });
    label.textContent = String(t);
    svg.appendChild(label);
  }
}

/* =====================================================================
   Column chart, single series (magnitude over time) - sequential hue.
   data: [{ label, value, tipHtml?, emphasis? }]
   ===================================================================== */

function columnChart(svg, data, opts = {}) {
  const theme = chartTheme();
  const {
    height = 150, padTop = 12, padBottom = 26, padLeft = 30, padRight = 8,
    labelEvery = 1, color = theme.seq, emphasisColor = theme.ink,
  } = opts;
  clear(svg);
  const width = svg.clientWidth || svg.parentElement.clientWidth || 640;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("height", height);

  const x0 = padLeft, x1 = width - padRight;
  const y0 = height - padBottom, y1 = padTop;
  const max = Math.max(1, ...data.map(d => d.value));
  const ticks = niceTicks(max);
  const top = Math.max(max, ticks[ticks.length - 1]);
  const yOf = v => y0 - (v / top) * (y0 - y1);

  drawGrid(svg, { x0, x1, yOf, ticks });

  const band = (x1 - x0) / Math.max(1, data.length);
  const thickness = Math.min(CHART.maxBarThickness, Math.max(2, band - CHART.gap));

  data.forEach((d, i) => {
    const cx = x0 + band * i + band / 2;
    const x = cx - thickness / 2;
    const h = d.value > 0 ? Math.max(2, y0 - yOf(d.value)) : 0;
    if (h > 0) {
      svg.appendChild(el("path", {
        d: barPath(x, y0 - h, thickness, h, CHART.barRadius, "up"),
        fill: d.emphasis ? emphasisColor : color,
        class: "mark",
      }));
    }
    // Hit area spans the whole band and full height, so hovering is easy
    // even where the value is zero.
    const hit = el("rect", {
      x: x0 + band * i, y: y1, width: band, height: y0 - y1, class: "bar-hit",
    });
    attachHover(svg, hit, d.tipHtml
      || `<div class="tip-head">${d.label}</div>${tipRow(color, "count", d.value)}`,
      `${d.label}: ${d.value}`);

    const labelTxt = d.short ?? d.label;
    if (labelEvery && i % labelEvery === 0 && labelTxt) {
      const t = el("text", {
        x: cx, y: y0 + 13, "text-anchor": "middle", class: "axis-label",
      });
      t.textContent = labelTxt;
      svg.appendChild(t);
    }
  });
}

/* =====================================================================
   Stacked column chart (part-to-whole over time) - categorical by series.
   data:   [{ label, short?, parts: [{key, value}] }]
   series: [{ key, color }]
   ===================================================================== */

function stackedColumnChart(svg, data, series, opts = {}) {
  const theme = chartTheme();
  const {
    height = 190, padTop = 12, padBottom = 30, padLeft = 32, padRight = 8,
    labelEvery = 1, markerIndex = -1,
  } = opts;
  clear(svg);
  const width = svg.clientWidth || svg.parentElement.clientWidth || 640;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("height", height);

  const colorOf = Object.fromEntries(series.map(s => [s.key, s.color]));
  const x0 = padLeft, x1 = width - padRight;
  const y0 = height - padBottom, y1 = padTop;
  const totals = data.map(d => d.parts.reduce((a, p) => a + p.value, 0));
  const max = Math.max(1, ...totals);
  const ticks = niceTicks(max);
  const top = Math.max(max, ticks[ticks.length - 1]);
  const scale = (y0 - y1) / top;

  drawGrid(svg, { x0, x1, yOf: v => y0 - v * scale, ticks });

  const band = (x1 - x0) / Math.max(1, data.length);
  const thickness = Math.min(CHART.maxBarThickness, Math.max(3, band - CHART.gap));

  data.forEach((d, i) => {
    const cx = x0 + band * i + band / 2;
    const x = cx - thickness / 2;
    let base = y0;
    const stacked = d.parts.filter(p => p.value > 0);

    stacked.forEach((p, j) => {
      const rawH = p.value * scale;
      const rawTop = base - rawH;
      const isTop = j === stacked.length - 1;
      // Shave the top of every segment that has one above it: the resulting
      // 2px of surface IS the separator. Never a stroke around the mark.
      const drawTop = isTop ? rawTop : rawTop + CHART.gap;
      const h = Math.max(1.5, base - drawTop);
      svg.appendChild(el("path", {
        d: isTop
          ? barPath(x, drawTop, thickness, h, CHART.barRadius, "up")
          : `M${x},${drawTop} h${thickness} v${h} h${-thickness} Z`,
        fill: colorOf[p.key] || theme.seq,
        class: "mark",
      }));
      base = rawTop;
    });

    if (markerIndex === i) {
      svg.appendChild(el("line", {
        x1: cx, x2: cx, y1: y1 - 4, y2: y0,
        stroke: theme.ink, "stroke-width": 1, "stroke-opacity": .45,
      }));
      // An unlabeled hairline is a puzzle; one word solves it. Above the
      // plot area, where no bar can collide with it.
      const now = el("text", {
        x: cx + 4, y: y1 - 7, class: "axis-label",
      });
      now.textContent = "now";
      svg.appendChild(now);
    }

    const rows = stacked
      .slice().sort((a, b) => b.value - a.value)
      .map(p => tipRow(colorOf[p.key], p.key, p.value)).join("");
    const hit = el("rect", {
      x: x0 + band * i, y: y1, width: band, height: y0 - y1, class: "bar-hit",
    });
    attachHover(svg, hit,
      `<div class="tip-head">${d.label}</div>`
      + (rows || `<div class="tip-row"><span>nothing scheduled</span></div>`)
      + tipRow(null, "<b>total</b>", `<b>${totals[i]}</b>`),
      `${d.label}: ${totals[i]} total`);
    // Spotlight: attention on one week recedes the others (CSS .spot rules).
    const lit = on => () => {
      svg.classList.toggle("spot", on);
      group.classList.toggle("hot", on);
    };
    hit.addEventListener("mouseenter", lit(true));
    hit.addEventListener("mouseleave", lit(false));
    hit.addEventListener("focus", lit(true));
    hit.addEventListener("blur", lit(false));

    const labelTxt = d.short ?? d.label;
    if (labelEvery && i % labelEvery === 0 && labelTxt) {
      const t = el("text", {
        x: cx, y: y0 + 14, "text-anchor": "middle", class: "axis-label",
      });
      t.textContent = labelTxt;
      svg.appendChild(t);
    }
  });
}

/* =====================================================================
   Horizontal bar chart - one bar per entity, colored by that entity.
   data: [{ label, value, color, note? }]
   ===================================================================== */

function barChartH(svg, data, opts = {}) {
  const { rowHeight = 30, padLeft = 74, padRight = 44, padTop = 4, padBottom = 4 } = opts;
  clear(svg);
  const width = svg.clientWidth || svg.parentElement.clientWidth || 480;
  const height = padTop + padBottom + rowHeight * Math.max(1, data.length);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("height", height);

  // Size the label gutter to the longest label instead of assuming course
  // codes stay short; capped at 40% so bars always keep the floor.
  let gutter = padLeft;
  const probe = el("text", { class: "mark-label", visibility: "hidden" });
  svg.appendChild(probe);
  try {
    let maxW = 0;
    for (const d of data) {
      probe.textContent = d.label;
      maxW = Math.max(maxW, probe.getComputedTextLength());
    }
    if (maxW > 0) gutter = Math.min(Math.max(padLeft, maxW + 22), width * 0.4);
  } catch { /* keep the default gutter */ }
  probe.remove();

  const x0 = gutter, x1 = width - padRight;
  const max = Math.max(1, ...data.map(d => d.value));
  const thickness = Math.min(CHART.maxBarThickness, rowHeight - 10);

  data.forEach((d, i) => {
    const y = padTop + rowHeight * i + (rowHeight - thickness) / 2;
    const w = Math.max(d.value > 0 ? 3 : 0, (d.value / max) * (x1 - x0));

    // Category label in ink, with a swatch carrying identity beside it.
    svg.appendChild(el("rect", {
      x: 0, y: y + thickness / 2 - 4, width: 9, height: 9, rx: 2, fill: d.color,
    }));
    const lab = el("text", {
      x: 14, y: y + thickness / 2 + 4, class: "mark-label",
    });
    lab.textContent = d.label;
    svg.appendChild(lab);

    if (w > 0) {
      svg.appendChild(el("path", {
        d: barPath(x0, y, w, thickness, CHART.barRadius, "right"),
        fill: d.color,
        class: "mark",
      }));
    }
    // Value at the tip, outside the bar so it never collides with the fill.
    const val = el("text", {
      x: x0 + w + 7, y: y + thickness / 2 + 4, class: "mark-label",
    });
    val.textContent = d.value;
    svg.appendChild(val);

    const hit = el("rect", { x: 0, y: padTop + rowHeight * i, width, height: rowHeight, class: "bar-hit" });
    attachHover(svg, hit,
      `<div class="tip-head">${d.label}</div>`
      + tipRow(d.color, "remaining", d.value)
      + (d.note ? `<div class="tip-row"><span>${d.note}</span></div>` : ""),
      `${d.label}: ${d.value} remaining`);
  });
}

/* =====================================================================
   Fused weekload chart (Analytics "Operations Ledger" band 2).
   A semester week-ruler shares slot geometry with the stacked columns:
   elapsed weeks carry an accent-filled ruler segment and 45%-opacity
   columns; the current week gets a full-height now-line. No card, no
   panel: the chart sits directly on the ground.
   data:   [{ label, short?, parts: [{key, value}], _current, _past }]
   series: [{ key, color }]
   ===================================================================== */

function fusedWeekloadChart(svg, data, series, opts = {}) {
  const theme = chartTheme();
  const {
    rulerZone = 26, plotH = 200, labelZone = 18, padLeft = 32, padRight = 8,
    labelEvery = 1, markerIndex = -1, peakIndex = -1, animate = false,
  } = opts;
  clear(svg);
  const width = svg.clientWidth || svg.parentElement.clientWidth || 900;
  const height = rulerZone + plotH + labelZone;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("height", height);

  const colorOf = Object.fromEntries(series.map(s => [s.key, s.color]));
  const x0 = padLeft, x1 = width - padRight;
  const y0 = rulerZone + plotH;      // baseline
  const y1 = rulerZone;              // plot top
  const totals = data.map(d => d.parts.reduce((a, p) => a + p.value, 0));
  const top = Math.max(1, ...totals);
  const scale = plotH / top;
  const band = (x1 - x0) / Math.max(1, data.length);
  const thickness = Math.max(3, band * 0.62);
  const elapsedAlpha = document.documentElement.dataset.theme === "dark" ? 0.45 : 0.55;

  // y gridlines at 10 and 20 only, numerals right-aligned in the gutter.
  for (const t of [10, 20]) {
    if (t > top) continue;
    const y = y0 - t * scale;
    svg.appendChild(el("line", { x1: x0, x2: x1, y1: y, y2: y, class: "grid-line" }));
    const lab = el("text", { x: x0 - 6, y: y + 3, "text-anchor": "end", class: "axis-label" });
    lab.textContent = String(t);
    svg.appendChild(lab);
  }
  svg.appendChild(el("line", { x1: x0, x2: x1, y1: y0, y2: y0, class: "axis-line" }));

  data.forEach((d, i) => {
    const slotX = x0 + band * i;
    const cx = slotX + band / 2;
    const x = cx - thickness / 2;

    // ---- ruler segment (top zone)
    const rY = 8;
    if (d._past || d._current) {
      const seg = el("rect", { x: slotX, y: rY, width: band, height: 3 });
      seg.style.fill = "var(--accent)";
      seg.style.opacity = d._current ? "1" : String(elapsedAlpha);
      svg.appendChild(seg);
    } else {
      const seg = el("rect", { x: slotX, y: rY + 2, width: band, height: 1 });
      seg.style.fill = "var(--hairline)";
      svg.appendChild(seg);
    }
    if (d._current) {
      const now = el("line", { x1: slotX, x2: slotX, y1: rY, y2: y0 });
      now.style.stroke = "var(--accent)";
      now.setAttribute("stroke-width", "1");
      svg.appendChild(now);
      const nowLab = el("text", { x: slotX + 4, y: rY - 2, class: "axis-label" });
      nowLab.textContent = "now";
      nowLab.style.fill = "var(--accent)";
      svg.appendChild(nowLab);
    }

    // ---- column stack (square corners, 1px ground gaps)
    const group = el("g", {});
    group.classList.add("colgrp");
    if (d._past) group.classList.add("col-past");
    if (animate) {
      group.classList.add("col-anim");
      group.style.animationDelay = `${i * 14}ms`;
    }
    let base = y0;
    const stacked = d.parts.filter(p => p.value > 0);
    stacked.forEach((p, j) => {
      const rawH = p.value * scale;
      const rawTop = base - rawH;
      const isTop = j === stacked.length - 1;
      const drawTop = isTop ? rawTop : rawTop + 1;
      const h = Math.max(1, base - drawTop);
      group.appendChild(el("rect", {
        x, y: drawTop, width: thickness, height: h,
        fill: colorOf[p.key] || theme.seq, class: "mark",
      }));
      base = rawTop;
    });
    svg.appendChild(group);

    // data labels on the current and peak columns only
    if ((i === markerIndex || i === peakIndex) && totals[i] > 0) {
      const lab = el("text", {
        x: cx, y: y0 - totals[i] * scale - 5, "text-anchor": "middle",
        class: "mark-label",
      });
      lab.style.fontWeight = "650";
      lab.style.fontSize = "11px";
      lab.textContent = String(totals[i]);
      svg.appendChild(lab);
    }

    // hover + keyboard hit
    const rows = stacked
      .slice().sort((a, b) => b.value - a.value)
      .map(p => tipRow(colorOf[p.key], p.key, p.value)).join("");
    const hit = el("rect", {
      x: slotX, y: y1, width: band, height: y0 - y1, class: "bar-hit",
    });
    attachHover(svg, hit,
      `<div class="tip-head">${d.label}</div>`
      + (rows || `<div class="tip-row"><span>nothing scheduled</span></div>`)
      + tipRow(null, "<b>total</b>", `<b>${totals[i]}</b>`),
      `${d.label}: ${totals[i]} total`);

    // x labels
    const labelTxt = d.short ?? d.label;
    if (labelEvery && i % labelEvery === 0 && labelTxt) {
      const t = el("text", {
        x: cx, y: y0 + 13, "text-anchor": "middle", class: "axis-label",
      });
      t.textContent = labelTxt;
      svg.appendChild(t);
    }
  });
}

/* ------------------------------------------------------------- legend */

function renderLegend(container, series) {
  container.innerHTML = "";
  for (const s of series) {
    const item = document.createElement("span");
    item.className = "legend-item";
    const sw = document.createElement("span");
    sw.className = "legend-swatch";
    sw.style.background = s.color;
    item.appendChild(sw);
    item.appendChild(document.createTextNode(s.key));
    container.appendChild(item);
  }
}

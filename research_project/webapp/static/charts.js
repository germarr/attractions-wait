"use strict";
/* Figures for the article.
 *
 * Hand-drawn inline SVG against the validated dark palette. Rules followed
 * throughout: thin marks, 2px lines, >=8px hit targets, 4px rounded data-ends
 * anchored to the baseline, a 2px surface gap between adjacent bars, recessive
 * grid and axes, a legend whenever there are two or more series (with direct
 * labels as the secondary encoding, so identity is never colour alone), text in
 * text tokens rather than series colours, and a table view on every figure.
 */

const S = {
  surface: "#1a1a19",
  ink: "#ffffff",
  dim: "#c3c2b7",
  grid: "#333330",
  cat: ["#3987e5", "#d95926", "#199e70", "#c98500"],
  // Ordinal blue ramp. On a dark surface the step nearest the surface must still
  // clear 2:1, so it stops at step 600 (#184f95, 2.15:1) rather than going darker.
  ordinal: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"],
};

const tip = () => document.getElementById("tip");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(Math.round(m) % 60).padStart(2, "0")}`;

function showTip(event, html) {
  const t = tip();
  t.innerHTML = html;
  t.style.opacity = "1";
  const box = t.getBoundingClientRect();
  let x = event.clientX + 14, y = event.clientY - box.height - 10;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
  if (y < 8) y = event.clientY + 16;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
}
const hideTip = () => { tip().style.opacity = "0"; };

/* ---------- small builders ---------- */

function frame(w, h, pad) {
  return {
    w, h, pad,
    x: (v, lo, hi) => pad.l + ((v - lo) / (hi - lo)) * (w - pad.l - pad.r),
    y: (v, lo, hi) => h - pad.b - ((v - lo) / (hi - lo)) * (h - pad.t - pad.b),
  };
}

function caption(figure, html) {
  const el = document.createElement("figcaption");
  el.className = "cap";
  el.innerHTML = html;
  figure.appendChild(el);
}

function legend(figure, items) {
  const el = document.createElement("div");
  el.className = "legend";
  el.innerHTML = items.map((i) =>
    `<span><i style="background:${i.color}"></i>${esc(i.label)}</span>`).join("");
  figure.appendChild(el);
}

function tableView(figure, headers, rows) {
  const button = document.createElement("button");
  button.className = "tbl-toggle";
  button.textContent = "Show data table";
  const table = document.createElement("table");
  table.hidden = true;
  table.innerHTML =
    `<thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  button.onclick = () => {
    table.hidden = !table.hidden;
    button.textContent = table.hidden ? "Show data table" : "Hide data table";
  };
  figure.appendChild(button);
  figure.appendChild(table);
}

function svgEl(html, viewBox) {
  const wrap = document.createElement("div");
  wrap.innerHTML = `<svg viewBox="${viewBox}" role="img">${html}</svg>`;
  return wrap.firstChild;
}

function axisY(f, lo, hi, ticks, fmt = (v) => v) {
  let out = "";
  for (const t of ticks) {
    const y = f.y(t, lo, hi).toFixed(1);
    out += `<line x1="${f.pad.l}" y1="${y}" x2="${f.w - f.pad.r}" y2="${y}" stroke="${S.grid}" stroke-width="1"/>`;
    out += `<text x="${f.pad.l - 7}" y="${y}" fill="${S.dim}" font-size="10" text-anchor="end" dominant-baseline="central">${fmt(t)}</text>`;
  }
  return out;
}


/* Direct labels are the secondary encoding that keeps identity off colour alone,
 * so they must stay legible. Series that finish at similar values would stack on
 * top of each other, so nudge them apart to a minimum spacing while preserving
 * their vertical order. */
function placeLabels(entries, minGap = 12) {
  const sorted = [...entries].sort((a, b) => a.y - b.y);
  for (let i = 1; i < sorted.length; i++) {
    const gap = sorted[i].y - sorted[i - 1].y;
    if (gap < minGap) sorted[i].y = sorted[i - 1].y + minGap;
  }
  return entries;
}

/* ---------- the figures ---------- */

const FIGURES = {

  search_space(figure, d) {
    const f = frame(620, 170, { l: 44, r: 96, t: 10, b: 26 });
    const logs = d.tours.map((t) => Math.log10(t));
    const hi = Math.max(...logs);
    const band = (f.h - f.pad.t - f.pad.b) / d.n.length;
    let g = "";
    d.n.forEach((n, i) => {
      const y = f.pad.t + i * band + 2;
      const h = band - 4;                    // 2px surface gap between bars
      const w = Math.max(3, (logs[i] / hi) * (f.w - f.pad.l - f.pad.r));
      g += `<rect x="${f.pad.l}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}"
        fill="${S.cat[0]}" rx="4"/>`;
      g += `<text x="${f.pad.l - 7}" y="${(y + h / 2).toFixed(1)}" fill="${S.dim}" font-size="10"
        text-anchor="end" dominant-baseline="central">${n} stops</text>`;
      const label = d.tours[i] < 1e6
        ? d.tours[i].toLocaleString()
        : d.tours[i].toExponential(1).replace("e+", " × 10^");
      g += `<text x="${(f.pad.l + w + 7).toFixed(1)}" y="${(y + h / 2).toFixed(1)}" fill="${S.ink}"
        font-size="10.5" font-family="JetBrains Mono,monospace" dominant-baseline="central">${label}</text>`;
    });
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    caption(figure, `Distinct tours of <b>n</b> stops, <b>(n−1)!/2</b>. Bars are log-scaled — each
      one is roughly an order of magnitude, so the bar for 30 is not 6× the bar for
      5, it is <b>10<sup>25</sup></b> times it. Brute force was never on the table.`);
    tableView(figure, ["Stops", "Distinct tours"],
      d.n.map((n, i) => [n, d.tours[i].toExponential(2)]));
  },

  intraday(figure, d) {
    const f = frame(620, 260, { l: 34, r: 122, t: 12, b: 26 });
    const all = d.series.flatMap((s) => s.values);
    const hi = Math.ceil(Math.max(...all) / 20) * 20;
    const [x0, x1] = [d.hours[0], d.hours[d.hours.length - 1]];
    let g = axisY(f, 0, hi, [0, hi / 4, hi / 2, (3 * hi) / 4, hi], (v) => Math.round(v));
    for (const h of d.hours) {
      if (h % 3) continue;
      g += `<text x="${f.x(h, x0, x1).toFixed(1)}" y="${f.h - f.pad.b + 13}" fill="${S.dim}"
        font-size="10" text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`;
    }
    const labels = [];
    d.series.forEach((s, i) => {
      const pts = s.values.map((v, k) => `${f.x(d.hours[k], x0, x1).toFixed(1)},${f.y(v, 0, hi).toFixed(1)}`);
      g += `<polyline points="${pts.join(" ")}" fill="none" stroke="${S.cat[i]}"
        stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      const last = s.values[s.values.length - 1];
      labels.push({ y: f.y(last, 0, hi), anchor: f.y(last, 0, hi), text: s.name.split(" ")[0], color: S.cat[i] });
    });
    // direct labels — the secondary encoding, so identity is never colour alone
    placeLabels(labels).forEach((l) => {
      const x = f.w - f.pad.r + 7;
      if (Math.abs(l.y - l.anchor) > 1.5) {
        g += `<line x1="${(x - 4).toFixed(1)}" y1="${l.anchor.toFixed(1)}" x2="${(x - 1).toFixed(1)}"
          y2="${l.y.toFixed(1)}" stroke="${l.color}" stroke-width="1" stroke-opacity=".6"/>`;
      }
      g += `<text x="${x}" y="${l.y.toFixed(1)}" fill="${S.dim}" font-size="10"
        dominant-baseline="central">${esc(l.text)}</text>`;
    });
    // hover crosshair
    d.hours.forEach((h, k) => {
      const x = f.x(h, x0, x1);
      const rows = d.series.map((s, i) =>
        `<span class="k">${esc(s.name)}</span> <b>${s.values[k]}</b> min`).join("<br>");
      g += `<rect x="${(x - 14).toFixed(1)}" y="${f.pad.t}" width="28"
        height="${f.h - f.pad.t - f.pad.b}" fill="transparent"
        data-tip="${esc(`<b>${String(h).padStart(2, "0")}:00</b><br>${rows}`)}"/>`;
    });
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    svg.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("mousemove", (e) => showTip(e, el.getAttribute("data-tip")));
      el.addEventListener("mouseleave", hideTip);
    });
    figure.appendChild(svg);
    caption(figure, `Mean standby wait by hour, Saturdays. The shapes do not agree:
      <b>Pirates</b> peaks at noon and recovers, <b>TRON</b> climbs all day. A single
      park-wide "crowd curve" would misprice both — which is why the model carries a
      curve per attraction.`);
    legend(figure, d.series.map((s, i) => ({ label: s.name, color: S.cat[i] })));
    tableView(figure, ["Hour", ...d.series.map((s) => s.name)],
      d.hours.map((h, k) => [`${String(h).padStart(2, "0")}:00`, ...d.series.map((s) => s.values[k])]));
  },

  fifo(figure, d) {
    const f = frame(620, 250, { l: 52, r: 14, t: 12, b: 26 });
    const all = [...d.linear, ...d.step];
    const lo = Math.floor(Math.min(...all) / 30) * 30, hi = Math.ceil(Math.max(...all) / 30) * 30;
    const [x0, x1] = [d.minutes[0], d.minutes[d.minutes.length - 1]];
    const ticks = []; for (let v = lo; v <= hi; v += (hi - lo) / 4) ticks.push(v);
    let g = axisY(f, lo, hi, ticks, (v) => clock(v));
    for (let m = x0; m <= x1; m += 30) {
      g += `<text x="${f.x(m, x0, x1).toFixed(1)}" y="${f.h - f.pad.b + 13}" fill="${S.dim}"
        font-size="10" text-anchor="middle">${clock(m)}</text>`;
    }
    const line = (vals, color) => `<polyline points="${vals.map((v, k) =>
      `${f.x(d.minutes[k], x0, x1).toFixed(1)},${f.y(v, lo, hi).toFixed(1)}`).join(" ")}"
      fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>`;
    g += line(d.linear, S.cat[0]);
    g += line(d.step, S.cat[1]);
    // mark the violation
    const worst = d.drops.reduce((a, b) => (b[1] > a[1] ? b : a));
    const wx = f.x(worst[0], x0, x1);
    const k = d.minutes.indexOf(worst[0]);
    g += `<line x1="${wx.toFixed(1)}" y1="${f.y(d.step[k], lo, hi).toFixed(1)}"
      x2="${wx.toFixed(1)}" y2="${f.y(d.step[k + 1], lo, hi).toFixed(1)}"
      stroke="${S.cat[1]}" stroke-width="2" stroke-dasharray="2 2"/>`;
    g += `<circle cx="${wx.toFixed(1)}" cy="${f.y(d.step[k + 1], lo, hi).toFixed(1)}" r="4.5"
      fill="${S.cat[1]}" stroke="${S.surface}" stroke-width="2"/>`;
    g += `<text x="${(wx + 9).toFixed(1)}" y="${(f.y(d.step[k + 1], lo, hi) + 4).toFixed(1)}"
      fill="${S.ink}" font-size="11" font-family="JetBrains Mono,monospace">−${worst[1]} min</text>`;
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    caption(figure, `When you get off <b>${esc(d.ride)}</b> (${esc(d.weekday)}) against when you
      arrive. The line must never fall — falling means arriving <em>later</em> gets you
      out <em>earlier</em>. Treating hourly waits as steps makes it fall
      <b>${worst[1]} minutes</b> at ${clock(worst[0])}; a solver would find that and
      "save" ${worst[1]} minutes by loitering. Interpolating between hour midpoints
      removes every such drop.`);
    legend(figure, [
      { label: "Linear interpolation (used)", color: S.cat[0] },
      { label: "Hourly steps (rejected)", color: S.cat[1] },
    ]);
    tableView(figure, ["Arrive", "Linear departure", "Step departure"],
      d.minutes.filter((_, i) => i % 10 === 0).map((m) => {
        const i = d.minutes.indexOf(m);
        return [clock(m), clock(d.linear[i]), clock(d.step[i])];
      }));
  },

  timing_gain(figure, d) {
    const f = frame(620, 132, { l: 4, r: 4, t: 14, b: 8 });
    const hi = d.sum_of_means;
    const bars = [
      { label: "Sum of average waits", value: d.sum_of_means, color: S.cat[1] },
      { label: "Optimally sequenced", value: d.optimized, color: S.cat[0] },
    ];
    let g = "";
    bars.forEach((b, i) => {
      const y = f.pad.t + i * 52;
      const w = (b.value / hi) * (f.w - 210);
      g += `<text x="0" y="${y + 8}" fill="${S.dim}" font-size="11">${esc(b.label)}</text>`;
      g += `<rect x="0" y="${y + 17}" width="${w.toFixed(1)}" height="17" fill="${b.color}" rx="4"/>`;
      g += `<text x="${(w + 9).toFixed(1)}" y="${y + 26}" fill="${S.ink}" font-size="12.5"
        font-family="JetBrains Mono,monospace" dominant-baseline="central">${b.value} min</text>`;
    });
    const wSaved = ((d.sum_of_means - d.optimized) / hi) * (f.w - 210);
    const xSaved = ((d.optimized) / hi) * (f.w - 210);
    g += `<rect x="${xSaved.toFixed(1)}" y="${f.pad.t + 69}" width="${wSaved.toFixed(1)}" height="17"
      fill="none" stroke="${S.dim}" stroke-width="1" stroke-dasharray="3 3" rx="4"/>`;
    g += `<text x="${(xSaved + wSaved + 9).toFixed(1)}" y="${f.pad.t + 78}" fill="${S.dim}"
      font-size="11" dominant-baseline="central">${d.saved} min saved by ordering alone</text>`;
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    caption(figure, `Time spent queueing on a ${(d.window / 60).toFixed(0)}-hour Saturday while
      riding all ${d.count}. Using each ride's average wait, the day needs more minutes than
      it has. Choosing <em>when</em> to ride recovers <b>${d.saved} minutes</b> — about five
      attractions — and is the entire reason this is modelled as time-dependent.`);
    tableView(figure, ["Measure", "Minutes"], [
      ["Sum of average waits", d.sum_of_means],
      ["Optimally sequenced", d.optimized],
      ["Saved by ordering", d.saved],
    ]);
  },

  distribution(figure, d) {
    const bandColor = { "walk-on": S.cat[2], middle: S.cat[0], headliner: S.cat[1] };
    const n = d.rows.length;
    const f = frame(620, 300, { l: 4, r: 4, t: 10, b: 22 });
    const hi = Math.ceil(Math.max(...d.rows.map((r) => r.wait)) / 10) * 10;
    const bw = (f.w - 8) / n;
    let g = "";
    for (const t of [0, hi / 2, hi]) {
      const y = f.y(t, 0, hi).toFixed(1);
      g += `<line x1="0" y1="${y}" x2="${f.w}" y2="${y}" stroke="${S.grid}" stroke-width="1"/>`;
      g += `<text x="2" y="${y - 4}" fill="${S.dim}" font-size="9.5">${t} min</text>`;
    }
    d.rows.forEach((r, i) => {
      const h = Math.max(2, (r.wait / hi) * (f.h - f.pad.t - f.pad.b));
      const x = 4 + i * bw;
      g += `<rect x="${x.toFixed(1)}" y="${(f.h - f.pad.b - h).toFixed(1)}"
        width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="${bandColor[r.band]}" rx="3"
        data-tip="${esc(`<b>${r.name}</b><br><span class="k">wait</span> ${r.wait} min <span class="k">ride</span> ${r.ride} min`)}"/>`;
    });
    g += `<text x="4" y="${f.h - 6}" fill="${S.dim}" font-size="10">shortest wait</text>`;
    g += `<text x="${f.w - 4}" y="${f.h - 6}" fill="${S.dim}" font-size="10" text-anchor="end">longest wait →</text>`;
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    svg.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("mousemove", (e) => showTip(e, el.getAttribute("data-tip")));
      el.addEventListener("mouseleave", hideTip);
    });
    figure.appendChild(svg);
    caption(figure, `Every attraction by mean wait. The <b>${d.n}</b> longest queues cost
      <b>${d.headliner_total} minutes</b> between them; the <b>${d.n}</b> shortest cost
      <b>${d.cheapest_total} minutes</b> for the same ride count. Trading TRON for the Tiki
      Room buys four more attractions — which a count-maximizer does every time.`);
    legend(figure, [
      { label: `Walk-on, under 10 min (${d.bands["walk-on"]})`, color: S.cat[2] },
      { label: `Middle (${d.bands.middle})`, color: S.cat[0] },
      { label: `Headliner, 25 min+ (${d.bands.headliner})`, color: S.cat[1] },
    ]);
    tableView(figure, ["Attraction", "Mean wait (min)", "Ride (min)", "Band"],
      d.rows.map((r) => [r.name, r.wait, r.ride, r.band]));
  },

  cliff(figure, d) {
    const f = frame(620, 260, { l: 34, r: 84, t: 12, b: 28 });
    const [x0, x1] = [d.windows[0], d.windows[d.windows.length - 1]];
    const lo = 20, hi = d.roster;
    let g = axisY(f, lo, hi, [20, 23, 26, hi], (v) => v);
    for (const w of d.windows) {
      g += `<text x="${f.x(w, x0, x1).toFixed(1)}" y="${f.h - f.pad.b + 14}" fill="${S.dim}"
        font-size="10" text-anchor="middle">${w}h</text>`;
    }
    g += `<line x1="${f.pad.l}" y1="${f.y(hi, lo, hi).toFixed(1)}" x2="${f.w - f.pad.r}"
      y2="${f.y(hi, lo, hi).toFixed(1)}" stroke="${S.dim}" stroke-width="1" stroke-dasharray="3 3"/>`;
    const cliffLabels = [];
    d.series.forEach((s, i) => {
      const color = S.ordinal[i];
      const pts = s.values.map((v, k) => `${f.x(d.windows[k], x0, x1).toFixed(1)},${f.y(v, lo, hi).toFixed(1)}`);
      g += `<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2"
        stroke-linejoin="round"/>`;
      if (i === 0 || i === d.series.length - 1) {
        cliffLabels.push({ y: f.y(s.values[s.values.length - 1], lo, hi), text: `crowd ${s.level}` });
      }
    });
    placeLabels(cliffLabels).forEach((l) => {
      g += `<text x="${f.w - f.pad.r + 7}" y="${l.y.toFixed(1)}" fill="${S.dim}"
        font-size="10" dominant-baseline="central">${esc(l.text)}</text>`;
    });
    d.windows.forEach((w, k) => {
      const rows = d.series.map((s) => `<span class="k">crowd ${s.level}</span> <b>${s.values[k]}</b>`).join("<br>");
      g += `<rect x="${(f.x(w, x0, x1) - 18).toFixed(1)}" y="${f.pad.t}" width="36"
        height="${f.h - f.pad.t - f.pad.b}" fill="transparent"
        data-tip="${esc(`<b>${w}-hour day</b><br>${rows}`)}"/>`;
    });
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    svg.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("mousemove", (e) => showTip(e, el.getAttribute("data-tip")));
      el.addEventListener("mouseleave", hideTip);
    });
    figure.appendChild(svg);
    caption(figure, `Attractions against the length of the operating day, one line per crowd
      level. The response is <b>flat-then-cliff</b>, not a slope: above roughly 13 hours the
      whole roster fits whatever the crowds. Crowd level shifts where the cliff sits by about
      an hour; an after-hours party moves you <em>across</em> it by five.`);
    legend(figure, d.series.map((s, i) => ({ label: `Crowd ${s.level}`, color: S.ordinal[i] })));
    tableView(figure, ["Window", ...d.series.map((s) => `Crowd ${s.level}`)],
      d.windows.map((w, k) => [`${w}h`, ...d.series.map((s) => s.values[k])]));
  },

  variance(figure, d) {
    const f = frame(620, 150, { l: 116, r: 60, t: 8, b: 8 });
    const hi = Math.max(...d.rows.map((r) => r.eta));
    const band = (f.h - f.pad.t - f.pad.b) / d.rows.length;
    let g = "";
    d.rows.forEach((r, i) => {
      const y = f.pad.t + i * band + 2;
      const h = band - 4;
      const w = Math.max(2, (r.eta / hi) * (f.w - f.pad.l - f.pad.r));
      g += `<rect x="${f.pad.l}" y="${y.toFixed(1)}" width="${w.toFixed(1)}"
        height="${h.toFixed(1)}" fill="${i === 0 ? S.cat[0] : "#3a4352"}" rx="4"/>`;
      g += `<text x="${f.pad.l - 8}" y="${(y + h / 2).toFixed(1)}" fill="${S.dim}" font-size="11"
        text-anchor="end" dominant-baseline="central">${esc(r.factor)}</text>`;
      g += `<text x="${(f.pad.l + w + 8).toFixed(1)}" y="${(y + h / 2).toFixed(1)}"
        fill="${i === 0 ? S.ink : S.dim}" font-size="11.5" font-family="JetBrains Mono,monospace"
        dominant-baseline="central">${(r.eta * 100).toFixed(1)}%</text>`;
    });
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    caption(figure, `Share of the variation in attractions-per-day each factor explains, over
      588 scenarios restricted to the realistic crowd range. Window length explains
      <b>93%</b>; crowd level <b>1.9%</b>. Once each weekday's overall busyness is divided
      out, <em>when</em> its peaks fall explains <b>0.4%</b> — nothing.`);
    tableView(figure, ["Factor", "Variance explained"],
      d.rows.map((r) => [r.factor, `${(r.eta * 100).toFixed(2)}%`]));
  },
};

/* ---------- wiring ---------- */

async function init() {
  const mounts = [...document.querySelectorAll("figure.chart")];
  if (!mounts.length) return;
  // The endpoint comes from the page, so a second article can reuse every helper
  // here instead of duplicating ~90 lines of frame/axis/legend/table code.
  const endpoint = document.body.dataset.chartsEndpoint || "/api/charts";
  let data;
  try {
    data = await (await fetch(endpoint)).json();
  } catch (error) {
    mounts.forEach((m) => { m.innerHTML = `<div class="cap">Chart data unavailable.</div>`; });
    return;
  }
  for (const mount of mounts) {
    const name = mount.dataset.chart;
    const render = FIGURES[name];
    if (!render || !data[name]) { mount.remove(); continue; }
    try {
      render(mount, data[name]);
    } catch (error) {
      mount.innerHTML = `<div class="cap">Could not draw “${esc(name)}”: ${esc(error.message)}</div>`;
    }
  }
}

// Exposed rather than self-starting: a page that adds figures loads its own
// script after this one and calls CHARTS.mount() when both registries are ready.
window.CHARTS = {
  FIGURES, mount: init, S, frame, caption, legend, tableView, svgEl, axisY,
  placeLabels, showTip, hideTip, clock, esc,
};

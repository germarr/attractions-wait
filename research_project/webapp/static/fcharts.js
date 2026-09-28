"use strict";
/* Figures for the forecasting article.
 *
 * Registers into the registry that charts.js exposes, so every helper — frame,
 * axisY, caption, legend, tableView, placeLabels, showTip — is the one the route
 * article already uses. Nothing about the house style is re-implemented here.
 *
 * Palette note: the season figure has seven parks, which is more series than the
 * categorical palette has slots. Rather than cycle hues, the parks are drawn as
 * recessive context in one muted colour and the all-park mean carries the only
 * categorical hue. Identity for the parks comes from direct labels and the data
 * table, never from colour.
 */

// Wrapped in an IIFE deliberately. Both this file and charts.js are classic
// scripts sharing one global lexical scope, so a top-level `const S` here would
// collide with charts.js's own `const S` and throw "already been declared" before
// a single figure rendered. Function scope keeps the borrowed helpers local.
(function () {
const { FIGURES, S, frame, caption, legend, tableView, svgEl, axisY, placeLabels,
        showTip, hideTip, esc } = window.CHARTS;

const CONTEXT = "#3a4352";   // recessive: context lines, not a series
const ZERO = "#4a4a45";

const pct = (v) => `${(v * 100).toFixed(1)}%`;
const signed = (v, digits = 3) => `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;

Object.assign(FIGURES, {

  /* 1. What the model's job actually is. */
  variance_ladder(figure, d) {
    const rows = [...d.steps, {
      label: "The day, of what is left", eta: d.day_share_of_residual, focus: true,
    }];
    const f = frame(620, 30 + rows.length * 30, { l: 168, r: 74, t: 8, b: 22 });
    const hi = 1;
    const band = (f.h - f.pad.t - f.pad.b) / rows.length;
    let g = "";
    [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
      const x = f.x(t, 0, hi);
      g += `<line x1="${x.toFixed(1)}" y1="${f.pad.t}" x2="${x.toFixed(1)}"
        y2="${(f.h - f.pad.b).toFixed(1)}" stroke="${S.grid}" stroke-width="1"/>
        <text x="${x.toFixed(1)}" y="${(f.h - f.pad.b + 14).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${(t * 100).toFixed(0)}%</text>`;
    });
    rows.forEach((row, i) => {
      const y = f.pad.t + i * band + 2;
      const h = band - 6;                       // 2px+ surface gap between bars
      const w = f.x(row.eta, 0, hi) - f.pad.l;
      g += `<rect x="${f.pad.l}" y="${y.toFixed(1)}" width="${Math.max(1, w).toFixed(1)}"
        height="${h.toFixed(1)}" rx="4" fill="${row.focus ? S.cat[0] : CONTEXT}"/>
        <text x="${f.pad.l - 8}" y="${(y + h / 2 + 3.5).toFixed(1)}" text-anchor="end"
        font-size="10.5" fill="${row.focus ? S.ink : S.dim}">${esc(row.label)}</text>
        <text x="${(f.pad.l + Math.max(1, w) + 7).toFixed(1)}"
        y="${(y + h / 2 + 3.5).toFixed(1)}" font-size="10.5" fill="${S.ink}"
        font-family="JetBrains Mono,monospace">${pct(row.eta)}</text>`;
    });
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    caption(figure, `Share of variance in hourly log wait explained by each grouping,
      over <b>${d.n_rows.toLocaleString()}</b> observations in hours
      ${d.hours[0]}–${d.hours[1]}. Knowing only <i>which ride and what hour</i> already
      accounts for <b>${pct(d.steps[2].eta)}</b>; adding the weekday buys
      <b>${((d.steps[3].eta - d.steps[2].eta) * 100).toFixed(1)} points</b>. The day
      itself explains <b>${pct(d.day_share_of_residual)}</b> of what remains — and that
      residual is the only thing a forecast can compete for.`);
    tableView(figure, ["Grouping", "Variance explained"],
      rows.map((r) => [r.label, pct(r.eta)]));
  },

  /* 2. The target moves seasonally, not weekly. */
  season(figure, d) {
    const f = frame(620, 232, { l: 34, r: 118, t: 10, b: 30 });
    const all = d.series.flatMap((s) => s.values).filter((v) => v !== null)
      .concat(d.overall);
    const hi = Math.ceil(Math.max(...all) / 10) * 10;
    const n = d.dates.length;
    const x = (i) => f.x(i, 0, n - 1);
    const ticks = [0, hi / 4, hi / 2, (hi * 3) / 4, hi];
    let g = axisY(f, 0, hi, ticks, (v) => v.toFixed(0));

    d.dates.forEach((date, i) => {
      if (date.slice(8) !== "01") return;
      g += `<line x1="${x(i).toFixed(1)}" y1="${f.pad.t}" x2="${x(i).toFixed(1)}"
        y2="${(f.h - f.pad.b).toFixed(1)}" stroke="${S.grid}" stroke-width="1"/>
        <text x="${x(i).toFixed(1)}" y="${(f.h - f.pad.b + 15).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}">${date.slice(0, 7)}</text>`;
    });

    d.series.forEach((s) => {
      let path = "";
      let open = false;
      s.values.forEach((v, i) => {
        if (v === null) { open = false; return; }
        path += `${open ? "L" : "M"}${x(i).toFixed(1)},${f.y(v, 0, hi).toFixed(1)}`;
        open = true;
      });
      g += `<path d="${path}" fill="none" stroke="${CONTEXT}" stroke-width="1.25"
        opacity="0.85"/>`;
    });

    const bold = d.overall.map((v, i) =>
      `${i ? "L" : "M"}${x(i).toFixed(1)},${f.y(v, 0, hi).toFixed(1)}`).join(" ");
    g += `<path d="${bold}" fill="none" stroke="${S.cat[0]}" stroke-width="2"/>`;

    const ti = d.dates.indexOf(d.trough.date);
    g += `<circle cx="${x(ti).toFixed(1)}" cy="${f.y(d.trough.value, 0, hi).toFixed(1)}"
      r="3.5" fill="${S.cat[0]}" stroke="${S.surface}" stroke-width="2"/>
      <text x="${(x(ti) + 8).toFixed(1)}" y="${(f.y(d.trough.value, 0, hi) + 13).toFixed(1)}"
      font-size="10" fill="${S.ink}" font-family="JetBrains Mono,monospace">
      ${d.trough.value} min</text>`;

    const labels = placeLabels([
      { y: f.y(d.overall[n - 1], 0, hi), text: "All parks", colour: S.ink },
      ...d.series.map((s) => {
        const last = [...s.values].reverse().find((v) => v !== null);
        return { y: f.y(last, 0, hi), text: s.park, colour: S.dim, small: true };
      }),
    ]);
    labels.forEach((l) => {
      g += `<text x="${(f.w - f.pad.r + 6).toFixed(1)}" y="${(l.y + 3).toFixed(1)}"
        font-size="${l.small ? 9 : 10.5}" fill="${l.colour}">${esc(l.text.slice(0, 20))}</text>`;
    });

    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    legend(figure, [
      { color: S.cat[0], label: "All parks, mean posted wait" },
      { color: CONTEXT, label: "Each park separately" },
    ]);
    caption(figure, `Mean posted wait per park-day across
      <b>${n}</b> dates. The all-park mean swings <b>${d.span} minutes</b>, bottoming
      at <b>${d.trough.value}</b> on ${d.trough.date}. Weekday, by contrast, spans only
      <b>${d.weekday_span} minutes</b> and explains <b>${pct(d.weekday_eta)}</b> of the
      variance between park-days. The thing worth predicting is seasonal, and there is
      exactly one regime change inside the training window.`);
    tableView(figure, ["Date", "All parks", ...d.series.map((s) => s.park)],
      d.dates.map((date, i) => [date, d.overall[i],
        ...d.series.map((s) => s.values[i] ?? "—")]));
  },

  /* 3. Persistence has a shelf life, and it flips sign. */
  horizon_decay(figure, d) {
    const f = frame(620, 210, { l: 42, r: 26, t: 12, b: 32 });
    const lo = Math.min(-0.2, Math.floor(Math.min(...d.corr) * 10) / 10);
    const hi = Math.max(0.6, Math.ceil(Math.max(...d.corr) * 10) / 10);
    const x = (h) => f.x(h, d.horizons[0], d.horizons[d.horizons.length - 1]);
    const ticks = [lo, lo + (hi - lo) / 4, lo + (hi - lo) / 2,
                   lo + (3 * (hi - lo)) / 4, hi];
    let g = axisY(f, lo, hi, ticks, (v) => v.toFixed(1));

    const zeroY = f.y(0, lo, hi);
    g += `<rect x="${f.pad.l}" y="${zeroY.toFixed(1)}"
      width="${(f.w - f.pad.l - f.pad.r).toFixed(1)}"
      height="${(f.h - f.pad.b - zeroY).toFixed(1)}" fill="${S.cat[1]}" opacity="0.07"/>`;
    g += `<line x1="${f.pad.l}" y1="${zeroY.toFixed(1)}"
      x2="${(f.w - f.pad.r).toFixed(1)}" y2="${zeroY.toFixed(1)}"
      stroke="${ZERO}" stroke-width="1.5"/>`;

    [1, 7, 14, 21, 28].forEach((h) => {
      if (h > d.horizons[d.horizons.length - 1]) return;
      g += `<text x="${x(h).toFixed(1)}" y="${(f.h - f.pad.b + 16).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${h}</text>`;
    });
    g += `<text x="${((f.pad.l + f.w - f.pad.r) / 2).toFixed(1)}"
      y="${(f.h - 4).toFixed(1)}" text-anchor="middle" font-size="10"
      fill="${S.dim}">days ahead</text>`;

    const line = d.corr.map((v, i) =>
      `${i ? "L" : "M"}${x(d.horizons[i]).toFixed(1)},${f.y(v, lo, hi).toFixed(1)}`).join(" ");
    g += `<path d="${line}" fill="none" stroke="${S.cat[0]}" stroke-width="2"/>`;
    d.corr.forEach((v, i) => {
      g += `<circle cx="${x(d.horizons[i]).toFixed(1)}" cy="${f.y(v, lo, hi).toFixed(1)}"
        r="4" fill="${S.cat[0]}" opacity="0.001"
        data-t="${d.horizons[i]}|${v}|${d.n[i]}"/>`;
    });
    if (d.crossing) {
      g += `<line x1="${x(d.crossing).toFixed(1)}" y1="${f.pad.t}"
        x2="${x(d.crossing).toFixed(1)}" y2="${(f.h - f.pad.b).toFixed(1)}"
        stroke="${S.cat[1]}" stroke-width="1.5" stroke-dasharray="3 3"/>
        <text x="${(x(d.crossing) + 6).toFixed(1)}" y="${(f.pad.t + 12).toFixed(1)}"
        font-size="10" fill="${S.cat[1]}">crosses zero at ${d.crossing} days</text>`;
    }
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    figure.appendChild(svg);
    svg.querySelectorAll("circle[data-t]").forEach((c) => {
      c.onmousemove = (event) => {
        const [h, v, n] = c.dataset.t.split("|");
        showTip(event, `<b>${h} days ahead</b><br>correlation ${Number(v).toFixed(3)}
          <br><span class="k">n = ${Number(n).toLocaleString()} park-days</span>`);
      };
      c.onmouseleave = hideTip;
    });
    caption(figure, `Correlation between a park's trailing-7-day crowd level at the
      forecast origin and its actual level <i>h</i> days later, on park-demeaned
      levels — pooling the parks raw would just show that busy parks stay busy.
      It starts at <b>${d.at_1}</b>, decays steadily, and
      ${d.crossing ? `crosses zero at <b>${d.crossing} days</b>, reaching
      <b>${d.at_end}</b>` : `reaches <b>${d.at_end}</b>`} — past three weeks, last
      week's crowds are not merely uninformative but faintly misleading. This is why
      the model shrinks its persistence term toward the park average as the horizon
      grows, and why a linear time trend was deliberately left out.`);
    tableView(figure, ["Days ahead", "Correlation", "n park-days"],
      d.horizons.map((h, i) => [h, d.corr[i].toFixed(3), d.n[i]]));
  },

  /* 4. The operator publishes its own demand forecast. */
  hours_signal(figure, d) {
    const f = frame(620, 250, { l: 44, r: 20, t: 12, b: 36 });
    const xs = d.points.map((p) => p.hours);
    const ys = d.points.map((p) => p.wait);
    const xlo = Math.floor(Math.min(...xs)) - 0.5;
    const xhi = Math.ceil(Math.max(...xs)) + 0.5;
    const yhi = Math.ceil(Math.max(...ys) / 10) * 10;
    const yticks = [0, yhi / 4, yhi / 2, (yhi * 3) / 4, yhi];
    let g = axisY(f, 0, yhi, yticks, (v) => v.toFixed(0));
    for (let h = Math.ceil(xlo); h <= Math.floor(xhi); h += 2) {
      g += `<text x="${f.x(h, xlo, xhi).toFixed(1)}" y="${(f.h - f.pad.b + 16).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${h}h</text>`;
    }
    g += `<text x="${((f.pad.l + f.w - f.pad.r) / 2).toFixed(1)}"
      y="${(f.h - 4).toFixed(1)}" text-anchor="middle" font-size="10"
      fill="${S.dim}">published operating window</text>`;

    d.points.filter((p) => p.park !== d.focus).forEach((p) => {
      g += `<circle cx="${f.x(p.hours, xlo, xhi).toFixed(1)}"
        cy="${f.y(p.wait, 0, yhi).toFixed(1)}" r="2.2" fill="${CONTEXT}" opacity="0.75"
        data-t="${esc(p.park)}|${p.date}|${p.hours}|${p.wait}"/>`;
    });
    d.points.filter((p) => p.park === d.focus).forEach((p) => {
      g += `<circle cx="${f.x(p.hours, xlo, xhi).toFixed(1)}"
        cy="${f.y(p.wait, 0, yhi).toFixed(1)}" r="3.1" fill="${S.cat[0]}"
        stroke="${S.surface}" stroke-width="1"
        data-t="${esc(p.park)}|${p.date}|${p.hours}|${p.wait}"/>`;
    });
    const fx1 = xlo;
    const fx2 = xhi;
    g += `<line x1="${f.x(fx1, xlo, xhi).toFixed(1)}"
      y1="${f.y(d.fit.slope * fx1 + d.fit.intercept, 0, yhi).toFixed(1)}"
      x2="${f.x(fx2, xlo, xhi).toFixed(1)}"
      y2="${f.y(d.fit.slope * fx2 + d.fit.intercept, 0, yhi).toFixed(1)}"
      stroke="${S.cat[0]}" stroke-width="2" stroke-dasharray="5 3"/>`;

    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    figure.appendChild(svg);
    svg.querySelectorAll("circle[data-t]").forEach((c) => {
      c.onmousemove = (event) => {
        const [park, date, hours, wait] = c.dataset.t.split("|");
        showTip(event, `<b>${esc(park)}</b><br>${date}<br>${hours}h open · ${wait} min`);
      };
      c.onmouseleave = hideTip;
    });
    legend(figure, [
      { color: S.cat[0], label: `${d.focus} (r = ${d.correlations[0].corr})` },
      { color: CONTEXT, label: "The other six parks" },
    ]);
    caption(figure, `Each dot is one park-day: how long the park published that it
      would be open, against how busy it turned out. At <b>${d.focus}</b> the
      correlation is <b>${d.correlations[0].corr}</b>. This is the model's strongest
      knowable-in-advance signal, and the reason is worth stating plainly: the
      operator sets opening hours from its <i>own</i> demand forecast weeks ahead, so
      the published schedule is a leaked forecast. It is not uniform — see the table:
      it runs from <b>${d.correlations[0].corr}</b> down to
      <b>${d.correlations[d.correlations.length - 1].corr}</b>.`);
    tableView(figure, ["Park", "Correlation with day mean", "n park-days"],
      d.correlations.map((c) => [c.park, c.corr, c.n]));
  },

  /* 5. The mechanism, in one picture. */
  decomposition(figure, d) {
    const hours = d.hours;
    const panelH = 96;
    const f = frame(620, panelH * 2 + 54, { l: 44, r: 96, t: 14, b: 26 });
    const x = (i) => f.x(i, 0, hours.length - 1);
    let g = "";

    /* top: the centered shape */
    const slo = Math.min(...d.shape) - 0.08;
    const shi = Math.max(...d.shape) + 0.08;
    const sy = (v) => f.pad.t + panelH - ((v - slo) / (shi - slo)) * (panelH - 22);
    g += `<text x="${f.pad.l}" y="${(f.pad.t - 2).toFixed(1)}" font-size="10.5"
      fill="${S.ink}">1 · The shape: how this ride sits against its park's day</text>`;
    g += `<line x1="${f.pad.l}" y1="${sy(0).toFixed(1)}"
      x2="${(f.w - f.pad.r).toFixed(1)}" y2="${sy(0).toFixed(1)}"
      stroke="${ZERO}" stroke-width="1" stroke-dasharray="3 3"/>`;
    g += `<path d="${d.shape.map((v, i) =>
      `${i ? "L" : "M"}${x(i).toFixed(1)},${sy(v).toFixed(1)}`).join(" ")}"
      fill="none" stroke="${S.cat[2]}" stroke-width="2"/>`;
    g += `<text x="${(f.w - f.pad.r + 6).toFixed(1)}" y="${(sy(d.shape[d.shape.length - 1]) + 3).toFixed(1)}"
      font-size="10" fill="${S.cat[2]}">shape</text>`;

    /* bottom: the two composed curves */
    const top = f.pad.t + panelH + 30;
    const curves = d.levels.map((l) => l.curve);
    const chi = Math.ceil(Math.max(...curves.flat()) / 10) * 10;
    const cy = (v) => top + panelH - (v / chi) * (panelH - 20);
    g += `<text x="${f.pad.l}" y="${(top - 6).toFixed(1)}" font-size="10.5"
      fill="${S.ink}">2 · The same shape, lifted by each day's predicted level</text>`;
    [0, chi / 2, chi].forEach((v) => {
      g += `<line x1="${f.pad.l}" y1="${cy(v).toFixed(1)}"
        x2="${(f.w - f.pad.r).toFixed(1)}" y2="${cy(v).toFixed(1)}"
        stroke="${S.grid}" stroke-width="1"/>
        <text x="${(f.pad.l - 7).toFixed(1)}" y="${(cy(v) + 3.5).toFixed(1)}"
        text-anchor="end" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${v.toFixed(0)}</text>`;
    });
    const labels = [];
    d.levels.forEach((l, k) => {
      const colour = S.cat[k];
      g += `<path d="${l.curve.map((v, i) =>
        `${i ? "L" : "M"}${x(i).toFixed(1)},${cy(v).toFixed(1)}`).join(" ")}"
        fill="none" stroke="${colour}" stroke-width="2"/>`;
      const obs = l.observed.map((v, i) => (v === null ? null : [x(i), cy(v)]));
      let path = "";
      let open = false;
      obs.forEach((pt) => {
        if (!pt) { open = false; return; }
        path += `${open ? "L" : "M"}${pt[0].toFixed(1)},${pt[1].toFixed(1)}`;
        open = true;
      });
      if (path) {
        g += `<path d="${path}" fill="none" stroke="${colour}" stroke-width="1.5"
          stroke-dasharray="4 3" opacity="0.8"/>`;
      }
      labels.push({ y: cy(l.curve[l.curve.length - 1]), text: l.label, colour });
    });
    placeLabels(labels).forEach((l) => {
      g += `<text x="${(f.w - f.pad.r + 6).toFixed(1)}" y="${(l.y + 3).toFixed(1)}"
        font-size="10" fill="${l.colour}">${esc(l.text)}</text>`;
    });
    hours.forEach((h, i) => {
      if (i % 2) return;
      g += `<text x="${x(i).toFixed(1)}" y="${(f.h - 8).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${h}</text>`;
    });

    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    legend(figure, [
      { color: S.cat[2], label: "Centered shape (log minutes)" },
      { color: S.cat[0], label: `${d.levels[0].label} — ${d.levels[0].date}` },
      { color: S.cat[1], label: `${d.levels[1].label} — ${d.levels[1].date}` },
    ]);
    caption(figure, `<b>${esc(d.ride)}</b>. The whole model is these two panels: a
      shape that barely changes, and a single number per park-day that lifts it. Solid
      lines are the reconstruction, dashed lines what was actually observed on those
      two dates. Predicting a day therefore means predicting <i>one number</i> — the
      level — which is why the day model has a few hundred training rows rather than a
      hundred thousand. A smearing factor of <b>${d.smearing}</b> corrects the
      log-to-minutes back-transform, which is a median and would otherwise read low.`);
    tableView(figure,
      ["Hour", "Shape (log)", ...d.levels.flatMap((l) => [`${l.label} predicted`, `${l.label} observed`])],
      hours.map((h, i) => [h, d.shape[i],
        ...d.levels.flatMap((l) => [l.curve[i], l.observed[i] ?? "—"])]));
  },

  /* 6. The same model, three splits. */
  leakage(figure, d) {
    const rows = d.rows;
    const f = frame(620, 44 + rows.length * 48, { l: 168, r: 84, t: 10, b: 24 });
    const hi = 1;
    const band = (f.h - f.pad.t - f.pad.b) / rows.length;
    let g = "";
    [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
      const x = f.x(t, 0, hi);
      g += `<line x1="${x.toFixed(1)}" y1="${f.pad.t}" x2="${x.toFixed(1)}"
        y2="${(f.h - f.pad.b).toFixed(1)}" stroke="${S.grid}" stroke-width="1"/>
        <text x="${x.toFixed(1)}" y="${(f.h - f.pad.b + 14).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${t.toFixed(2)}</text>`;
    });
    rows.forEach((row, i) => {
      const y = f.pad.t + i * band + 3;
      const h = band - 12;
      const w = f.x(row.r2, 0, hi) - f.pad.l;
      const colour = row.honest ? S.cat[0] : S.cat[1];
      g += `<rect x="${f.pad.l}" y="${y.toFixed(1)}" width="${Math.max(1, w).toFixed(1)}"
        height="${h.toFixed(1)}" rx="4" fill="${colour}"/>
        <text x="${f.pad.l - 8}" y="${(y + h / 2 - 1).toFixed(1)}" text-anchor="end"
        font-size="10.5" fill="${S.ink}">${esc(row.label)}</text>
        <text x="${f.pad.l - 8}" y="${(y + h / 2 + 11).toFixed(1)}" text-anchor="end"
        font-size="9" fill="${row.honest ? S.cat[0] : S.cat[1]}">
        ${row.honest ? "the actual task" : "not a valid estimate"}</text>
        <text x="${(f.pad.l + Math.max(1, w) + 7).toFixed(1)}"
        y="${(y + h / 2 + 3.5).toFixed(1)}" font-size="10.5" fill="${S.ink}"
        font-family="JetBrains Mono,monospace">${row.r2.toFixed(3)}${
          row.honest ? "" : ` &nbsp;×${row.inflation.toFixed(2)}`}</text>`;
    });
    figure.appendChild(svgEl(g, `0 0 ${f.w} ${f.h}`));
    legend(figure, [
      { color: S.cat[0], label: "Honest protocol" },
      { color: S.cat[1], label: "Leaks information about the target" },
    ]);
    caption(figure, `R² on the park-day level for the <i>same model and the same
      features</i>, scored three ways. Splitting rows at random scores
      <b>${rows[0].r2.toFixed(3)}</b> — about ${Math.round(
        rows[0].mae && rows[2].mae ? rows[2].mae / rows[0].mae : 0)}× less error than the
      honest protocol — because roughly 130 rows share each park-day, so every test row
      has its own day's crowd level sitting in the training set. Splitting whole
      <i>days</i> at random still scores <b>${rows[1].r2.toFixed(3)}</b>: no row of the
      target day is used, but the days either side of it are, so the model interpolates
      a bracketed date instead of extrapolating past the last one it has seen. Only
      <b>${rows[2].r2.toFixed(3)}</b> answers the question a visitor actually asks.`);
    tableView(figure, ["Protocol", "R²", "Level MAE", "n", "Why"],
      rows.map((r) => [r.label, r.r2.toFixed(4), r.mae.toFixed(4),
        r.n.toLocaleString(), r.note]));
  },

  /* 7. The headline result. */
  skill_by_horizon(figure, d) {
    const groups = d.bands;
    const series = d.series;
    const f = frame(620, 66 + groups.length * 62, { l: 92, r: 30, t: 22, b: 30 });
    const flat = series.flatMap((s) => s.values.flatMap((v) => [v.lo, v.hi, v.skill]));
    const lo = Math.min(-0.05, Math.floor(Math.min(...flat) * 20) / 20);
    const hi = Math.max(0.3, Math.ceil(Math.max(...flat) * 20) / 20);
    const bandH = (f.h - f.pad.t - f.pad.b) / groups.length;
    const rowH = Math.min(15, (bandH - 12) / series.length);
    let g = "";
    [lo, 0, hi].concat([-0.2, -0.1, 0.1, 0.2, 0.3].filter((t) => t > lo && t < hi))
      .forEach((t) => {
        const x = f.x(t, lo, hi);
        g += `<line x1="${x.toFixed(1)}" y1="${f.pad.t}" x2="${x.toFixed(1)}"
          y2="${(f.h - f.pad.b).toFixed(1)}"
          stroke="${t === 0 ? ZERO : S.grid}" stroke-width="${t === 0 ? 1.5 : 1}"/>
          <text x="${x.toFixed(1)}" y="${(f.h - f.pad.b + 15).toFixed(1)}"
          text-anchor="middle" font-size="10" fill="${S.dim}"
          font-family="JetBrains Mono,monospace">${t.toFixed(2)}</text>`;
      });
    g += `<text x="${f.x(0, lo, hi).toFixed(1)}" y="${(f.pad.t - 8).toFixed(1)}"
      text-anchor="middle" font-size="9.5" fill="${S.dim}">no better than the baseline</text>`;

    groups.forEach((band, gi) => {
      const y0 = f.pad.t + gi * bandH;
      g += `<text x="${(f.pad.l - 10).toFixed(1)}" y="${(y0 + bandH / 2).toFixed(1)}"
        text-anchor="end" font-size="10.5" fill="${S.ink}">${band} days</text>`;
      series.forEach((s, si) => {
        const v = s.values.find((x) => x.band === band);
        if (!v) return;
        const y = y0 + 8 + si * rowH;
        const x0 = f.x(Math.min(0, v.skill), lo, hi);
        const x1 = f.x(Math.max(0, v.skill), lo, hi);
        const colour = S.cat[si];
        g += `<line x1="${f.x(v.lo, lo, hi).toFixed(1)}" y1="${(y + rowH / 2).toFixed(1)}"
          x2="${f.x(v.hi, lo, hi).toFixed(1)}" y2="${(y + rowH / 2).toFixed(1)}"
          stroke="${colour}" stroke-width="1.25" opacity="0.65"/>
          <rect x="${x0.toFixed(1)}" y="${y.toFixed(1)}"
          width="${Math.max(1.5, x1 - x0).toFixed(1)}" height="${(rowH - 5).toFixed(1)}"
          rx="3" fill="${colour}" opacity="${v.spans_zero ? 0.45 : 1}"
          data-t="${esc(s.baseline)}|${band}|${v.skill}|${v.lo}|${v.hi}|${v.n_rows}|${v.n_origins}|${v.spans_zero}"/>
          <text x="${(f.x(v.hi, lo, hi) + 6).toFixed(1)}"
          y="${(y + rowH / 2 + 2).toFixed(1)}" font-size="9" fill="${S.dim}"
          font-family="JetBrains Mono,monospace">n=${(v.n_rows / 1000).toFixed(0)}k</text>`;
      });
    });
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    figure.appendChild(svg);
    svg.querySelectorAll("rect[data-t]").forEach((r) => {
      r.onmousemove = (event) => {
        const [base, band, skill, low, high, n, origins, spans] = r.dataset.t.split("|");
        showTip(event, `<b>${esc(base)}</b><br>${band} days ahead
          <br>skill ${signed(Number(skill))}
          <br><span class="k">90% CI ${Number(low).toFixed(3)} to ${Number(high).toFixed(3)}</span>
          <br><span class="k">${Number(n).toLocaleString()} predictions, ${origins} origins</span>
          ${spans === "true" ? `<br><span class="k">interval spans zero</span>` : ""}`);
      };
      r.onmouseleave = hideTip;
    });
    legend(figure, series.map((s, i) => ({ color: S.cat[i], label: s.baseline })));
    const head = series[0].values;
    caption(figure, `Skill score — the share of squared error removed — against each
      baseline, by how far ahead the prediction is. Whiskers are a 90% interval from a
      bootstrap over <b>${head[0].n_origins}</b> retraining origins, which is the right
      resampling unit because all the predictions from one origin share a fitted model.
      Against the weekday-by-hour estimate the model removes
      <b>${pct(head[0].skill)}</b> to <b>${pct(Math.max(...head.map((v) => v.skill)))}</b>
      of squared error and the interval excludes zero at every horizon. Against simply
      carrying last week forward, the intervals mostly <i>do</i> span zero — a faded bar
      means the honest answer is "not measurably better."`);
    tableView(figure,
      ["Baseline", "Days ahead", "Skill", "90% low", "90% high", "n predictions", "origins"],
      series.flatMap((s) => s.values.map((v) => [s.baseline, v.band,
        v.skill.toFixed(4), v.lo.toFixed(4), v.hi.toFixed(4),
        v.n_rows.toLocaleString(), v.n_origins])));
  },

  /* 8. Where the error lives, and whether the bands mean anything. */
  calibration_bands(figure, d) {
    const f = frame(620, 260, { l: 44, r: 108, t: 12, b: 34 });
    const pts = d.series.flatMap((s) => s.points);
    const hi = Math.ceil(Math.max(...pts.flatMap((p) => [p.predicted, p.actual])) / 10) * 10;
    const ticks = [0, hi / 4, hi / 2, (hi * 3) / 4, hi];
    let g = axisY(f, 0, hi, ticks, (v) => v.toFixed(0));
    for (let v = 0; v <= hi; v += hi / 5) {
      g += `<text x="${f.x(v, 0, hi).toFixed(1)}" y="${(f.h - f.pad.b + 16).toFixed(1)}"
        text-anchor="middle" font-size="10" fill="${S.dim}"
        font-family="JetBrains Mono,monospace">${v.toFixed(0)}</text>`;
    }
    g += `<text x="${((f.pad.l + f.w - f.pad.r) / 2).toFixed(1)}"
      y="${(f.h - 4).toFixed(1)}" text-anchor="middle" font-size="10"
      fill="${S.dim}">predicted minutes</text>`;
    g += `<line x1="${f.x(0, 0, hi).toFixed(1)}" y1="${f.y(0, 0, hi).toFixed(1)}"
      x2="${f.x(hi, 0, hi).toFixed(1)}" y2="${f.y(hi, 0, hi).toFixed(1)}"
      stroke="${ZERO}" stroke-width="1.5" stroke-dasharray="4 4"/>
      <text x="${(f.x(hi, 0, hi) - 4).toFixed(1)}" y="${(f.y(hi, 0, hi) + 14).toFixed(1)}"
      text-anchor="end" font-size="9.5" fill="${S.dim}">perfect</text>`;

    const labels = [];
    d.series.forEach((s, si) => {
      const colour = S.cat[si];
      s.points.forEach((p) => {
        g += `<circle cx="${f.x(p.predicted, 0, hi).toFixed(1)}"
          cy="${f.y(p.actual, 0, hi).toFixed(1)}" r="4" fill="${colour}"
          stroke="${S.surface}" stroke-width="1.5"
          data-t="${esc(s.band)}|${p.predicted}|${p.actual}|${p.n}"/>`;
      });
      const last = s.points[s.points.length - 1];
      if (last) labels.push({ y: f.y(last.actual, 0, hi), text: s.band, colour });
    });
    placeLabels(labels).forEach((l) => {
      g += `<text x="${(f.w - f.pad.r + 8).toFixed(1)}" y="${(l.y + 3).toFixed(1)}"
        font-size="10" fill="${l.colour}">${esc(l.text)}</text>`;
    });
    const svg = svgEl(g, `0 0 ${f.w} ${f.h}`);
    figure.appendChild(svg);
    svg.querySelectorAll("circle[data-t]").forEach((c) => {
      c.onmousemove = (event) => {
        const [band, predicted, actual, n] = c.dataset.t.split("|");
        showTip(event, `<b>${esc(band)}</b><br>predicted ${predicted} min
          <br>actual ${actual} min
          <br><span class="k">n = ${Number(n).toLocaleString()}</span>`);
      };
      c.onmouseleave = hideTip;
    });
    legend(figure, d.series.map((s, i) => ({ color: S.cat[i], label: s.band })));
    const cover = d.coverage.map((c) => c.covered);
    const err = Object.fromEntries(d.band_error.map((b) => [b.band, b]));
    caption(figure, `Binned predicted wait against what actually happened, per band.
      Points on the dashed line are perfectly calibrated. The absolute error is wildly
      unequal across bands — MAE <b>${err["walk-on"] ? err["walk-on"].mae : "—"}</b>
      minutes on walk-ons against
      <b>${err["headliner"] ? err["headliner"].mae : "—"}</b> on headliners — so a single
      MAE figure for the whole park describes neither. The stated
      ${Math.round(d.nominal * 100)}% bands actually contained the outcome
      <b>${pct(Math.min(...cover))}</b>–<b>${pct(Math.max(...cover))}</b> of the time,
      measured on <b>${d.tested_on_origins}</b> origins held out from the ones that
      defined them — the same check against the bands' own rows returns exactly
      ${Math.round(d.nominal * 100)}% by construction and proves nothing.`);
    tableView(figure, ["Band", "Horizon", "Coverage", "Nominal", "n"],
      d.coverage.map((c) => [c.band, `${c.horizon_band} days`, pct(c.covered),
        pct(d.nominal), c.n.toLocaleString()]));
  },
});
}());

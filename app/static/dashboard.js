/* QUEUESCOPE dashboard — fetch + ChartJS. Talks to /api/{attractions,stats,series}. */
(() => {
  "use strict";

  const COLORS = {
    accent: "#ffb454",   // mean
    accent2: "#56d4c4",  // median
    band: "rgba(255,180,84,0.12)",
    grid: "rgba(255,255,255,0.05)",
    gridX: "rgba(255,255,255,0.04)",
    tick: "#8a93a6",
    faint: "#5b6376",
    text: "#e9ecf2",
    panel: "#0d111a",
    temp: "#d98f63",     // temperature line (secondary axis)
    tempTick: "#a8754f",
  };

  // WMO weather code → { label, icon } for the header chip.
  const WMO = {
    0: ["Clear", "☀"], 1: ["Mostly clear", "🌤"], 2: ["Partly cloudy", "⛅"],
    3: ["Overcast", "☁"], 45: ["Fog", "🌫"], 48: ["Rime fog", "🌫"],
    51: ["Light drizzle", "🌦"], 53: ["Drizzle", "🌦"], 55: ["Heavy drizzle", "🌦"],
    56: ["Freezing drizzle", "🌧"], 57: ["Freezing drizzle", "🌧"],
    61: ["Light rain", "🌧"], 63: ["Rain", "🌧"], 65: ["Heavy rain", "🌧"],
    66: ["Freezing rain", "🌧"], 67: ["Freezing rain", "🌧"],
    71: ["Light snow", "🌨"], 73: ["Snow", "🌨"], 75: ["Heavy snow", "🌨"], 77: ["Snow grains", "🌨"],
    80: ["Light showers", "🌦"], 81: ["Showers", "🌧"], 82: ["Violent showers", "⛈"],
    85: ["Snow showers", "🌨"], 86: ["Snow showers", "🌨"],
    95: ["Thunderstorm", "⛈"], 96: ["Thunderstorm, hail", "⛈"], 99: ["Thunderstorm, hail", "⛈"],
  };
  const wmo = (c) => WMO[c] || ["—", "○"];

  // Rain shading: cool-blue vertical bands behind buckets where it rained.
  const rainBands = {
    id: "rainBands",
    beforeDatasetsDraw(chart) {
      const s = chart.$series;
      if (!s || !s.length) return;
      const x = chart.scales.x, area = chart.chartArea, ctx = chart.ctx;
      if (!x || !area) return;
      const half = (area.right - area.left) / s.length / 2;
      ctx.save();
      s.forEach((p, i) => {
        const r = p.rain || 0;
        if (r <= 0) return;
        const cx = x.getPixelForValue(i);
        ctx.fillStyle = `rgba(111,159,216,${Math.min(0.06 + 0.2 * r, 0.26)})`;
        ctx.fillRect(cx - half, area.top, half * 2, area.bottom - area.top);
      });
      ctx.restore();
    },
  };
  if (window.Chart) Chart.register(rainBands);

  const STATS_MS = 60 * 1000;        // cards every 60s
  const SERIES_MS = 5 * 60 * 1000;   // charts every 5min

  const $ = (id) => document.getElementById(id);
  const charts = {};
  let target = null;
  let statsTimer = null;
  let seriesTimer = null;

  // ── formatting helpers ────────────────────────────────
  const fmt = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  };
  const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + fmt(Math.abs(v));

  function setStatus(ok, label) {
    const el = $("status");
    el.classList.toggle("is-stale", !ok);
    $("status-text").textContent = label;
  }
  function stampUpdated() {
    const t = new Date();
    const hh = String(t.getHours()).padStart(2, "0");
    const mm = String(t.getMinutes()).padStart(2, "0");
    const ss = String(t.getSeconds()).padStart(2, "0");
    $("updated").textContent = `${hh}:${mm}:${ss}`;
  }

  // ── selector ──────────────────────────────────────────
  async function loadAttractions() {
    const res = await fetch("/api/attractions");
    const parks = await res.json();
    const sel = $("target");
    sel.innerHTML = "";
    for (const park of parks) {
      const group = document.createElement("optgroup");
      group.label = park.destination ? `${park.destination} · ${park.park}` : park.park;
      for (const opt of park.options) {
        const o = document.createElement("option");
        o.value = opt.id;
        o.textContent = opt.name;
        group.appendChild(o);
      }
      sel.appendChild(group);
    }
    // default: first real selectable option
    const first = sel.querySelector("option");
    if (first) {
      target = first.value;
      sel.value = target;
    }
  }

  // ── cards ─────────────────────────────────────────────
  function renderCards(s) {
    const closed = s.current === null || s.current === undefined;

    const numEl = $("c-current-num");
    const valEl = $("c-current");
    const unit = valEl.querySelector(".unit");
    if (closed) {
      numEl.textContent = "Closed";
      valEl.classList.add("muted");
      if (unit) unit.style.display = "none";
      $("c-current-foot").textContent = "no standby wait right now";
    } else {
      numEl.textContent = fmt(s.current);
      valEl.classList.remove("muted");
      if (unit) unit.style.display = "";
      $("c-current-foot").innerHTML = "&nbsp;";
    }

    $("c-mean").textContent = fmt(s.mean);
    $("c-median").textContent = fmt(s.median);

    renderDelta("card-dmean", "c-dmean", "c-dmean-foot", s.delta_mean,
      s.mean, "vs today's average");
    renderDelta("card-dmedian", "c-dmedian", "c-dmedian-foot", s.delta_median,
      s.median, "vs today's typical");

    renderWeatherChip(s.weather);
    renderDowntime(s.downtime);
    renderHours(s.park_hours);
  }

  function renderHours(ph) {
    const el = $("hours");
    if (!ph) {
      el.hidden = true;
      return;
    }
    el.classList.toggle("open", ph.is_open_now);
    el.classList.toggle("closed", !ph.is_open_now);
    $("h-state").textContent = ph.is_open_now ? "Open" : "Closed";
    $("h-range").textContent = `${ph.opening}–${ph.closing}`;
    el.hidden = false;
  }

  function renderDowntime(dt) {
    const card = $("card-downtime");
    if (!dt) {
      $("c-downtime").textContent = "—";
      card.classList.remove("is-down");
      $("down-tag").hidden = true;
      $("c-downtime-foot").textContent = "unplanned outages";
      return;
    }
    $("c-downtime").textContent = dt.down_minutes;
    const down = dt.currently_down > 0;
    card.classList.toggle("is-down", down);
    $("down-tag").hidden = !down;
    if (down) {
      $("c-downtime-foot").textContent = dt.is_park
        ? `${dt.currently_down} ride${dt.currently_down > 1 ? "s" : ""} down now`
        : "down right now";
    } else if (dt.outages > 0) {
      $("c-downtime-foot").textContent = `${dt.outages} outage${dt.outages > 1 ? "s" : ""} today`;
    } else {
      $("c-downtime-foot").textContent = "no outages today";
    }
  }

  function renderWeatherChip(w) {
    const chip = $("wx");
    if (!w || w.temperature_c === null || w.temperature_c === undefined) {
      chip.hidden = true;
      return;
    }
    const [label, icon] = wmo(w.weather_code);
    $("wx-icon").textContent = icon;
    $("wx-temp").textContent = `${Math.round(w.temperature_c)}°C`;
    $("wx-label").textContent = label;
    chip.classList.toggle("raining", !!w.raining);
    chip.hidden = false;
  }

  function renderDelta(cardId, valId, footId, delta, base, fallbackFoot) {
    const card = $(cardId);
    const valEl = $(valId);
    card.classList.remove("good", "bad", "flat");
    if (delta === null || delta === undefined) {
      valEl.textContent = "—";
      card.classList.add("flat");
      $(footId).textContent = fallbackFoot;
      return;
    }
    // negative delta = below typical = good; positive = busier = bad
    const arrow = delta > 0 ? "▲" : delta < 0 ? "▼" : "■";
    card.classList.add(delta > 0 ? "bad" : delta < 0 ? "good" : "flat");
    valEl.innerHTML =
      `<span class="arrow">${arrow}</span>${signed(delta)}<span class="unit"> min</span>`;
    const word = delta > 0 ? "busier than" : delta < 0 ? "calmer than" : "right at";
    $(footId).textContent = `${word} ${fmt(base)} typical`;
  }

  // ── charts ────────────────────────────────────────────
  function datasets(series) {
    const upper = series.map((p) => p.upper);
    const lower = series.map((p) => p.lower);
    return [
      // band: upper fills down to the next dataset (lower)
      {
        label: "+1σ", data: upper, order: 4,
        borderColor: "rgba(0,0,0,0)", backgroundColor: COLORS.band,
        pointRadius: 0, borderWidth: 0, fill: "+1", tension: 0.35,
      },
      {
        label: "−1σ", data: lower, order: 4,
        borderColor: "rgba(0,0,0,0)", pointRadius: 0, borderWidth: 0,
        fill: false, tension: 0.35,
      },
      {
        label: "Mean", data: series.map((p) => p.mean), order: 1,
        borderColor: COLORS.accent, backgroundColor: COLORS.accent,
        pointRadius: 0, pointHoverRadius: 4, borderWidth: 2.25, tension: 0.35,
      },
      {
        label: "Median", data: series.map((p) => p.median), order: 2,
        borderColor: COLORS.accent2, backgroundColor: COLORS.accent2,
        pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, borderDash: [5, 3],
        tension: 0.35,
      },
      // temperature on the secondary (right) axis — visually subordinate
      {
        label: "°C", data: series.map((p) => (p.temp ?? null)), order: 5,
        yAxisID: "y1", borderColor: COLORS.temp, backgroundColor: COLORS.temp,
        pointRadius: 0, pointHoverRadius: 3, borderWidth: 1.5, borderDash: [2, 3],
        tension: 0.35, spanGaps: true,
      },
    ];
  }

  function afterBody(series) {
    return (items) => {
      const p = series[items[0].dataIndex];
      if (!p) return "";
      const lines = [
        `  ±1σ band  ${fmt(p.lower)}–${fmt(p.upper)} min`,
        `  samples   ${p.n}`,
      ];
      if (p.temp !== null && p.temp !== undefined) lines.push(`  temp      ${fmt(p.temp)} °C`);
      if (p.rain > 0) lines.push(`  raining   ${Math.round(p.rain * 100)}% of bucket`);
      return lines.join("\n");
    };
  }

  function chartOptions(series) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      animation: { duration: 350 },
      layout: { padding: { top: 6, right: 4, bottom: 0, left: 0 } },
      scales: {
        x: {
          grid: { color: COLORS.gridX, drawTicks: false },
          border: { display: false },
          ticks: {
            color: COLORS.tick, font: { family: "JetBrains Mono", size: 10 },
            maxRotation: 0, autoSkip: true, maxTicksLimit: 12,
          },
        },
        y: {
          beginAtZero: true,
          grace: "8%",
          grid: { color: COLORS.grid, drawTicks: false },
          border: { display: false },
          ticks: {
            color: COLORS.tick, font: { family: "JetBrains Mono", size: 10 },
            padding: 8, maxTicksLimit: 6,
          },
        },
        y1: {
          position: "right",
          beginAtZero: false,
          grace: "40%",
          grid: { drawOnChartArea: false },
          border: { display: false },
          ticks: {
            color: COLORS.tempTick, font: { family: "JetBrains Mono", size: 10 },
            padding: 6, maxTicksLimit: 5, callback: (v) => `${v}°`,
          },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#0a0d13",
          borderColor: "rgba(255,255,255,0.1)",
          borderWidth: 1,
          titleColor: COLORS.text,
          bodyColor: COLORS.tick,
          titleFont: { family: "JetBrains Mono", size: 11, weight: "600" },
          bodyFont: { family: "JetBrains Mono", size: 11 },
          padding: 11,
          displayColors: true,
          callbacks: {
            label: (ctx) => {
              const L = ctx.dataset.label;
              if (L === "+1σ" || L === "−1σ") return null;
              if (L === "°C") return `  Temp   ${fmt(ctx.parsed.y)} °C`;
              return `  ${L}   ${fmt(ctx.parsed.y)} min`;
            },
            afterBody: afterBody(series),
          },
        },
      },
    };
  }

  function renderChart(key, canvasId, series) {
    const panel = $(`panel-${key}`);
    const empty = series.length === 0;
    panel.classList.toggle("is-empty", empty);

    if (empty) {
      if (charts[key]) { charts[key].destroy(); delete charts[key]; }
      return;
    }

    const labels = series.map((p) => p.label);
    if (charts[key]) {
      const c = charts[key];
      c.$series = series;
      c.data.labels = labels;
      c.data.datasets = datasets(series);
      c.options.plugins.tooltip.callbacks.afterBody = afterBody(series);
      c.update();
      return;
    }

    const ctx = $(canvasId).getContext("2d");
    charts[key] = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: datasets(series) },
      options: chartOptions(series),
    });
    charts[key].$series = series;
  }

  // ── live trace (raw 90-min wait line) ─────────────────
  function renderRecent(points) {
    const panel = $("panel-recent");
    const empty = !points || points.length === 0;
    panel.classList.toggle("is-empty", empty);
    if (empty) {
      if (charts.recent) { charts.recent.destroy(); delete charts.recent; }
      return;
    }
    const labels = points.map((p) => p.t);
    const data = points.map((p) => p.wait);

    if (charts.recent) {
      charts.recent.data.labels = labels;
      charts.recent.data.datasets[0].data = data;
      charts.recent.update("none"); // smooth minute tick, no re-animation
      return;
    }

    const ctx = $("chart-recent").getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, 252);
    grad.addColorStop(0, "rgba(255,180,84,0.18)");
    grad.addColorStop(1, "rgba(255,180,84,0)");
    charts.recent = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Wait", data, borderColor: COLORS.accent, backgroundColor: grad,
          fill: true, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, tension: 0.3,
          spanGaps: false, // break the line where the ride was closed (null)
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        animation: { duration: 300 },
        layout: { padding: { top: 6, right: 4, bottom: 0, left: 0 } },
        scales: {
          x: {
            grid: { color: COLORS.gridX, drawTicks: false },
            border: { display: false },
            ticks: {
              color: COLORS.tick, font: { family: "JetBrains Mono", size: 10 },
              maxRotation: 0, autoSkip: true, maxTicksLimit: 10,
            },
          },
          y: {
            beginAtZero: true, grace: "10%",
            grid: { color: COLORS.grid, drawTicks: false },
            border: { display: false },
            ticks: {
              color: COLORS.tick, font: { family: "JetBrains Mono", size: 10 },
              padding: 8, maxTicksLimit: 6,
            },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0a0d13", borderColor: "rgba(255,255,255,0.1)", borderWidth: 1,
            titleColor: COLORS.text, bodyColor: COLORS.tick,
            titleFont: { family: "JetBrains Mono", size: 11, weight: "600" },
            bodyFont: { family: "JetBrains Mono", size: 11 },
            padding: 11, displayColors: false,
            callbacks: { label: (c) => `  Wait   ${fmt(c.parsed.y)} min` },
          },
        },
      },
    });
  }

  // ── reliability (table + correlation heatmap) ─────────
  const WINDOWS_REL = ["today", "yesterday", "week", "month", "historic"];

  const pct = (v) => (v === null || v === undefined ? "—" : `${v.toFixed(1)}%`);
  const zfmt = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}`);

  function relCell(v) {
    const td = document.createElement("td");
    td.textContent = pct(v);
    if (v !== null && v !== undefined) {
      const a = Math.min(v / 40, 1) * 0.28; // redder = more downtime
      td.style.background = `rgba(251,111,111,${a.toFixed(3)})`;
    }
    return td;
  }

  function relRow(r, isTotal) {
    const tr = document.createElement("tr");
    if (isTotal) tr.className = "total";
    const name = document.createElement("td");
    name.className = "a";
    name.textContent = r.attraction;
    tr.appendChild(name);
    for (const w of WINDOWS_REL) tr.appendChild(relCell(r.rates[w]));
    const z = document.createElement("td");
    z.className = "z";
    z.textContent = zfmt(r.z);
    if (r.z !== null && r.z !== undefined) {
      z.style.color = r.z > 0.3 ? "var(--bad)" : r.z < -0.3 ? "var(--good)" : "var(--muted)";
    }
    tr.appendChild(z);
    return tr;
  }

  function renderReliability(d) {
    $("rel-park").textContent = d.park ? `· ${d.park}` : "";
    const body = $("rel-body");
    body.innerHTML = "";
    if (d.park_total) {
      body.appendChild(relRow({ attraction: "All attractions", rates: d.park_total.rates, z: d.park_total.z }, true));
    }
    if (!d.rows || !d.rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.className = "rel-empty";
      td.textContent = "No operating data in window yet";
      tr.appendChild(td);
      body.appendChild(tr);
    } else {
      for (const r of d.rows) body.appendChild(relRow(r, false));
    }
    renderCorrelation(d.correlation);
  }

  function corrCell(text, cls) {
    const div = document.createElement("div");
    div.className = `cell ${cls}`;
    div.textContent = text;
    return div;
  }

  function renderCorrelation(c) {
    const grid = $("corr-grid");
    grid.innerHTML = "";
    const labels = c.labels;
    const k = labels.length;
    grid.style.gridTemplateColumns = `auto repeat(${k}, minmax(58px,86px))`;
    const insufficient = !c.matrix || c.n < 50;
    grid.classList.toggle("insufficient", insufficient);
    $("corr-n").textContent = `n=${c.n}`;
    $("corr-note").textContent = insufficient
      ? "Not enough data yet — correlations need ~50 hourly observations to be meaningful."
      : "Pearson r across hourly observations (last 30 days).";

    grid.appendChild(corrCell("", "corner"));
    for (const l of labels) grid.appendChild(corrCell(l, "chead"));
    for (let i = 0; i < k; i++) {
      grid.appendChild(corrCell(labels[i], "rhead"));
      for (let j = 0; j < k; j++) {
        const r = c.matrix ? c.matrix[i][j] : null;
        const cell = corrCell(r === null || r === undefined ? "—" : r.toFixed(2), `rcell${i === j ? " diag" : ""}`);
        if (r !== null && r !== undefined && i !== j) {
          // diverging tint with a visible floor so weak correlations still read
          const a = 0.25 + Math.min(Math.abs(r), 1) * 0.5;
          cell.style.background = r > 0
            ? `rgba(238,124,90,${a})`   // warm coral = positive
            : `rgba(86,160,230,${a})`;  // cool blue = negative
        }
        grid.appendChild(cell);
      }
    }
  }

  // ── fetch cycles ──────────────────────────────────────
  async function refreshStats() {
    if (!target) return;
    try {
      const res = await fetch(`/api/stats?attraction=${encodeURIComponent(target)}`);
      if (!res.ok) throw new Error(res.status);
      renderCards(await res.json());
      stampUpdated();
      setStatus(true, "live");
    } catch (e) {
      setStatus(false, "offline");
    }
    // Live trace rides the same 60s beat, independently guarded.
    try {
      const r = await fetch(`/api/recent?attraction=${encodeURIComponent(target)}`);
      if (r.ok) renderRecent(await r.json());
    } catch (e) {
      /* leave last trace in place */
    }
  }

  async function refreshSeries() {
    if (!target) return;
    const windows = ["today", "week", "month"];
    try {
      const results = await Promise.all(
        windows.map((w) =>
          fetch(`/api/series?attraction=${encodeURIComponent(target)}&window=${w}`)
            .then((r) => {
              if (!r.ok) throw new Error(r.status);
              return r.json();
            })
        )
      );
      windows.forEach((w, i) => renderChart(w, `chart-${w}`, results[i]));
      setStatus(true, "live");
    } catch (e) {
      setStatus(false, "offline");
    }
    // Reliability rides the same slow (5-min) cycle; independently guarded.
    try {
      const rr = await fetch(`/api/reliability?attraction=${encodeURIComponent(target)}`);
      if (rr.ok) renderReliability(await rr.json());
    } catch (e) {
      /* leave last table in place */
    }
  }

  function refreshAll() {
    refreshStats();
    refreshSeries();
  }

  function startTimers() {
    if (statsTimer) clearInterval(statsTimer);
    if (seriesTimer) clearInterval(seriesTimer);
    statsTimer = setInterval(refreshStats, STATS_MS);
    seriesTimer = setInterval(refreshSeries, SERIES_MS);
  }

  // ── boot ──────────────────────────────────────────────
  async function init() {
    $("target").addEventListener("change", (e) => {
      target = e.target.value;
      refreshAll();
    });
    try {
      await loadAttractions();
    } catch (e) {
      setStatus(false, "offline");
      return;
    }
    refreshAll();
    startTimers();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* Daily Performance page — talks to /api/day/live (60s) and /api/day/summary (5m). */
(() => {
  "use strict";
  const C = {
    wait: "#ffb454", mean: "#9b8cff", median: "#56d4c4", temp: "#d98f63",
    today: "#ffb454", yesterday: "#fb6f6f", wow: "#56d4c4", mom: "#9b8cff",
    grid: "rgba(255,255,255,0.05)", gridX: "rgba(255,255,255,0.04)",
    tick: "#8a93a6", text: "#e9ecf2", bar: "rgba(255,255,255,0.14)",
  };
  const LIVE_MS = 60 * 1000, SUM_MS = 5 * 60 * 1000;
  const $ = (id) => document.getElementById(id);
  const charts = {};
  let target = null, liveTimer = null, sumTimer = null;

  const fmt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : (Number.isInteger(v) ? String(v) : v.toFixed(1)));
  const signed = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt(Math.abs(v))}`);

  function setStatus(ok, label) { $("status").classList.toggle("is-stale", !ok); $("status-text").textContent = label; }
  // Freshness comes from the data, not the browser clock (ADR-0007). Behind the
  // Serving Store a successful fetch no longer implies fresh data: if a Publish
  // dies, the Function keeps returning 200 with the last good rows. So the badge
  // reads the Watermark's observed-at and ages it, instead of stamping now().
  const FRESH_MS = 12 * 60 * 1000, STALE_MS = 60 * 60 * 1000;
  async function stampFreshness() {
    try {
      const w = await (await fetch("/api/watermark")).json();
      const iso = w && w.live && w.live.observed_at;
      if (!iso) { setStatus(false, "no data"); return; }
      const t = new Date(iso), age = Date.now() - t.getTime();
      $("updated").textContent = [t.getHours(), t.getMinutes(), t.getSeconds()]
        .map((x) => String(x).padStart(2, "0")).join(":");
      if (age < FRESH_MS) setStatus(true, "live");
      else if (age < STALE_MS) setStatus(false, "stale");
      else setStatus(false, "offline");
    } catch (e) { setStatus(false, "offline"); }
  }

  async function loadAttractions() {
    const parks = await (await fetch("/api/attractions")).json();
    const sel = $("target"); sel.innerHTML = "";
    for (const park of parks) {
      const g = document.createElement("optgroup");
      g.label = park.destination ? `${park.destination} · ${park.park}` : park.park;
      for (const o of park.options) { const opt = document.createElement("option"); opt.value = o.id; opt.textContent = o.name; g.appendChild(opt); }
      sel.appendChild(g);
    }
    const first = sel.querySelector("option");
    if (first) { target = first.value; sel.value = target; }
  }

  // ── cards ─────────────────────────────────────────────
  function setDelta(cardId, valId, footId, delta, baseLabel) {
    const card = $(cardId), v = $(valId);
    card.classList.remove("good", "bad", "flat");
    if (delta === null || delta === undefined) { v.textContent = "—"; card.classList.add("flat"); $(footId).textContent = baseLabel; return; }
    card.classList.add(delta > 0 ? "bad" : delta < 0 ? "good" : "flat");
    v.innerHTML = `${signed(delta)}<span class="unit"> min</span>`;
    $(footId).textContent = baseLabel;
  }
  function setPill(id, v) {
    const el = $(id), b = el.querySelector("b");
    el.classList.remove("up", "down");
    if (v === null || v === undefined) { b.textContent = "—"; return; }
    el.classList.add(v > 0 ? "up" : v < 0 ? "down" : "");
    b.textContent = `${v > 0 ? "+" : ""}${v}%`;
  }

  function renderLive(d) {
    const cur = $("d-current"), val = cur.closest(".value"), unit = val.querySelector(".unit");
    if (d.current === null || d.current === undefined) { cur.textContent = "Closed"; val.classList.add("muted"); if (unit) unit.style.display = "none"; }
    else { cur.textContent = fmt(d.current); val.classList.remove("muted"); if (unit) unit.style.display = ""; }
    setPill("pill-wow", d.wow); setPill("pill-mom", d.mom); setPill("pill-yoy", d.yoy);

    const dt = d.downtime || {};
    $("d-downtime").textContent = dt.today === undefined || dt.today === null ? "—" : fmt(dt.today);
    const down = (dt.currently_down || 0) > 0;
    $("card-downtime").classList.toggle("is-down", down);
    $("d-downtag").hidden = !down;
    renderDayChart(d);
  }

  function renderSummary(d) {
    const dl = d.deltas || {};
    setDelta("card-mean", "d-mean", "foot-mean", dl.mean, "today's average");
    setDelta("card-median", "d-median", "foot-median", dl.median, "today's typical");
    setDelta("card-week", "d-week", "foot-week", dl.week, `7-day mean ${d.means.week ?? "—"}`);
    setDelta("card-month", "d-month", "foot-month", dl.month, `30-day mean ${d.means.month ?? "—"}`);
    setDelta("card-historic", "d-historic", "foot-historic", dl.historic, `all-time mean ${d.means.historic ?? "—"}`);

    const dt = d.downtime || {};
    const card = $("card-downtime"); card.classList.remove("good", "bad");
    if (dt.delta !== null && dt.delta !== undefined) {
      card.classList.add(dt.delta > 0 ? "bad" : "good");
      $("foot-downtime").textContent = `${signed(dt.delta)} pts vs historic ${fmt(dt.historic)}%`;
    } else $("foot-downtime").textContent = "vs historic";

    renderCompareChart(d.compare);
    renderWeekdayChart(d.weekday);
    renderCorrelation(d.correlation);
  }

  // ── charts ────────────────────────────────────────────
  const axisX = { grid: { color: C.gridX, drawTicks: false }, border: { display: false }, ticks: { color: C.tick, font: { family: "JetBrains Mono", size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } };
  const axisY = { beginAtZero: true, grace: "8%", grid: { color: C.grid, drawTicks: false }, border: { display: false }, ticks: { color: C.tick, font: { family: "JetBrains Mono", size: 10 }, padding: 8, maxTicksLimit: 6 } };
  const baseTooltip = { backgroundColor: "#0a0d13", borderColor: "rgba(255,255,255,0.1)", borderWidth: 1, titleColor: C.text, bodyColor: C.tick, titleFont: { family: "JetBrains Mono", size: 11, weight: "600" }, bodyFont: { family: "JetBrains Mono", size: 11 }, padding: 10 };

  function setEmpty(panelId, empty) { $(panelId).classList.toggle("is-empty", empty); }
  function lineDS(label, data, color, opts) { return Object.assign({ label, data, borderColor: color, backgroundColor: color, pointRadius: 0, pointHoverRadius: 3, borderWidth: 2, tension: 0.3, spanGaps: true }, opts || {}); }

  function renderDayChart(d) {
    const empty = !d.trace || !d.trace.length;
    setEmpty("panel-day", empty);
    if (empty) { if (charts.day) { charts.day.destroy(); delete charts.day; } return; }
    const labels = d.trace.map((p) => p.t);
    const datasets = [
      lineDS("Wait", d.trace.map((p) => p.wait), C.wait, { spanGaps: false, borderWidth: 2 }),
      lineDS("Mean", labels.map(() => d.mean), C.mean, { borderDash: [5, 4], borderWidth: 1.5 }),
      lineDS("Median", labels.map(() => d.median), C.median, { borderDash: [3, 3], borderWidth: 1.5 }),
      lineDS("°C", d.trace.map((p) => p.temp), C.temp, { borderDash: [2, 3], borderWidth: 1.5, yAxisID: "y1" }),
    ];
    const opts = {
      responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
      interaction: { mode: "index", intersect: false },
      scales: { x: axisX, y: axisY, y1: { position: "right", grace: "40%", grid: { drawOnChartArea: false }, border: { display: false }, ticks: { color: "#a8754f", font: { family: "JetBrains Mono", size: 10 }, callback: (v) => `${v}°`, maxTicksLimit: 5 } } },
      plugins: { legend: { display: false }, tooltip: Object.assign({ displayColors: true }, baseTooltip) },
    };
    if (charts.day) { charts.day.data.labels = labels; charts.day.data.datasets = datasets; charts.day.update("none"); }
    else charts.day = new Chart($("chart-day"), { type: "line", data: { labels, datasets }, options: opts });
  }

  function renderCompareChart(c) {
    const empty = !c || !c.labels || !c.labels.length;
    setEmpty("panel-compare", empty);
    // Tell the user which prior-period lines have no data yet (and why).
    const note = $("compare-note");
    if (!empty) {
      const missing = ["yesterday", "wow", "mom"].filter((k) => !(c[k] || []).some((v) => v !== null));
      const map = { yesterday: "Yesterday (1d)", wow: "WoW (7d)", mom: "MoM (28d)" };
      note.textContent = missing.length
        ? `No history yet for ${missing.map((k) => map[k]).join(" · ")} — these lines appear once data reaches that far back.`
        : "";
    } else note.textContent = "";
    if (empty) { if (charts.compare) { charts.compare.destroy(); delete charts.compare; } return; }
    const datasets = [
      lineDS("Today", c.today, C.today, { borderWidth: 2.25 }),
      lineDS("Yesterday", c.yesterday, C.yesterday, { borderWidth: 1.5 }),
      lineDS("WoW", c.wow, C.wow, { borderWidth: 1.5 }),
      lineDS("MoM", c.mom, C.mom, { borderWidth: 1.5 }),
    ];
    const opts = { responsive: true, maintainAspectRatio: false, animation: { duration: 300 }, interaction: { mode: "index", intersect: false }, scales: { x: axisX, y: axisY }, plugins: { legend: { display: false }, tooltip: Object.assign({}, baseTooltip) } };
    if (charts.compare) charts.compare.destroy();
    charts.compare = new Chart($("chart-compare"), { type: "line", data: { labels: c.labels, datasets }, options: opts });
  }

  function renderWeekdayChart(w) {
    const empty = !w || !w.rows || !w.rows.some((r) => r.mean !== null);
    setEmpty("panel-weekday", empty);
    if (empty) { if (charts.weekday) { charts.weekday.destroy(); delete charts.weekday; } return; }
    const labels = w.rows.map((r) => r.day);
    const data = w.rows.map((r) => r.mean);
    const bg = w.rows.map((_, i) => (i === w.today_index ? C.today : C.bar));
    const datasets = [
      { type: "bar", label: "Mean wait", data, backgroundColor: bg, borderRadius: 4, maxBarThickness: 46 },
      { type: "line", label: "Today's mean", data: labels.map(() => w.today_mean), borderColor: C.median, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, spanGaps: true },
    ];
    const opts = { responsive: true, maintainAspectRatio: false, animation: { duration: 300 }, scales: { x: { grid: { display: false }, border: { display: false }, ticks: { color: C.tick, font: { family: "JetBrains Mono", size: 11 } } }, y: axisY }, plugins: { legend: { display: false }, tooltip: Object.assign({}, baseTooltip) } };
    if (charts.weekday) charts.weekday.destroy();
    charts.weekday = new Chart($("chart-weekday"), { type: "bar", data: { labels, datasets }, options: opts });
  }

  function corrCell(text, cls) { const d = document.createElement("div"); d.className = `cell ${cls}`; d.textContent = text; return d; }
  function renderCorrelation(c) {
    const grid = $("corr-grid"); grid.innerHTML = "";
    const labels = c.labels, k = labels.length;
    grid.style.gridTemplateColumns = `auto repeat(${k}, minmax(58px,86px))`;
    const insufficient = !c.matrix || c.n < 50;
    grid.classList.toggle("insufficient", insufficient);
    $("corr-n").textContent = `n=${c.n}`;
    $("corr-note").textContent = insufficient ? "Not enough data yet — correlations need ~50 hourly observations to be meaningful." : "Pearson r across hourly observations (last 30 days).";
    grid.appendChild(corrCell("", "corner"));
    for (const l of labels) grid.appendChild(corrCell(l, "chead"));
    for (let i = 0; i < k; i++) {
      grid.appendChild(corrCell(labels[i], "rhead"));
      for (let j = 0; j < k; j++) {
        const r = c.matrix ? c.matrix[i][j] : null;
        const cell = corrCell(r === null || r === undefined ? "—" : r.toFixed(2), `rcell${i === j ? " diag" : ""}`);
        if (r !== null && r !== undefined && i !== j) { const a = 0.25 + Math.min(Math.abs(r), 1) * 0.5; cell.style.background = r > 0 ? `rgba(238,124,90,${a})` : `rgba(86,160,230,${a})`; }
        grid.appendChild(cell);
      }
    }
  }

  // ── cycles ────────────────────────────────────────────
  async function refreshLive() {
    if (!target) return;
    try { const r = await fetch(`/api/day/live?attraction=${encodeURIComponent(target)}`); if (!r.ok) throw 0; renderLive(await r.json()); stampFreshness(); }
    catch (e) { setStatus(false, "offline"); }
  }
  async function refreshSummary() {
    if (!target) return;
    try { const r = await fetch(`/api/day/summary?attraction=${encodeURIComponent(target)}`); if (r.ok) renderSummary(await r.json()); } catch (e) { /* keep */ }
  }
  function refreshAll() { refreshLive(); refreshSummary(); }

  async function init() {
    $("target").addEventListener("change", (e) => { target = e.target.value; refreshAll(); });
    try { await loadAttractions(); } catch (e) { setStatus(false, "offline"); return; }
    refreshAll();
    liveTimer = setInterval(refreshLive, LIVE_MS);
    sumTimer = setInterval(refreshSummary, SUM_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();

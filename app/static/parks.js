/* Parks comparison page — /api/destinations + /api/parks/compare.
   Two radars (Busyness, Operations) overlay every park in the chosen
   Destination, on absolute fixed axes (a quiet day draws a small polygon).
   The window toggle drives the radars; the live strip is always "now". */
(() => {
  "use strict";
  const C = {
    grid: "rgba(255,255,255,0.07)", angle: "rgba(255,255,255,0.06)",
    tick: "#8a93a6", text: "#e9ecf2", faint: "#5b6376",
  };
  // Up to a destination's worth of parks; distinct, legible on dark.
  const PALETTE = ["#ffb454", "#56d4c4", "#9b8cff", "#fb6f6f", "#6cc8ff", "#c6d44e"];
  const REFRESH_MS = 60 * 1000;
  const $ = (id) => document.getElementById(id);
  const charts = {};
  let destinations = [], destId = null, windowSel = "today", timer = null;

  const fmt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : (Number.isInteger(v) ? String(v) : v.toFixed(1)));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const color = (i) => PALETTE[i % PALETTE.length];

  function setStatus(ok, label) { $("status").classList.toggle("is-stale", !ok); $("status-text").textContent = label; }
  function stamp() { const t = new Date(); $("updated").textContent = [t.getHours(), t.getMinutes(), t.getSeconds()].map((x) => String(x).padStart(2, "0")).join(":"); }

  // ── radar axis definitions (raw → unit; cap = absolute rim value) ──────
  const busyAxes = (caps) => [
    { label: "Avg wait", key: "avg_wait", cap: caps.avg_wait, unit: " min" },
    { label: "Crowd index", key: "crowd_index", cap: caps.crowd_index, unit: "%" },
    { label: "Headliner", key: "headliner_wait", cap: caps.headliner, unit: " min" },
  ];
  const opsAxes = (caps) => [
    { label: "Uptime", key: "uptime", cap: 100, unit: "%" },
    { label: "Open now", key: "open_pct", cap: 100, unit: "%" },
    { label: "Roster", key: "roster", cap: caps.roster, unit: "" },
  ];

  function derive(p) {
    // Live "open now" share for the Operations radar (distinct from windowed Uptime).
    return Object.assign({}, p, {
      open_pct: p.roster ? Math.round((1000 * p.open_now) / p.roster) / 10 : null,
    });
  }

  // ── controls ──────────────────────────────────────────────────────────
  function buildDestToggle() {
    const box = $("dest-toggle"); box.innerHTML = "";
    for (const d of destinations) {
      const b = document.createElement("button");
      b.textContent = d.name; b.dataset.id = d.id;
      b.classList.toggle("active", d.id === destId);
      b.addEventListener("click", () => { if (destId === d.id) return; destId = d.id; syncToggles(); refresh(); });
      box.appendChild(b);
    }
  }
  function syncToggles() {
    for (const b of $("dest-toggle").children) b.classList.toggle("active", b.dataset.id === destId);
    for (const b of $("window-toggle").children) b.classList.toggle("active", b.dataset.w === windowSel);
  }

  // ── live strip ────────────────────────────────────────────────────────
  function momentumChip(m) {
    if (m === null || m === undefined) return `<span class="pt-mom flat">— flat</span>`;
    const cls = m > 0.2 ? "up" : m < -0.2 ? "down" : "flat";
    const arrow = m > 0.2 ? "▲" : m < -0.2 ? "▼" : "▪";
    const sign = m > 0 ? "+" : m < 0 ? "−" : "";
    return `<span class="pt-mom ${cls}">${arrow} ${sign}${fmt(Math.abs(m))}</span>`;
  }
  function renderLive(parks) {
    const strip = $("livestrip"); strip.innerHTML = "";
    parks.forEach((p, i) => {
      const tile = document.createElement("div"); tile.className = "ptile";
      const down = (p.down_now || 0) > 0
        ? `<span class="pt-down">● ${p.down_now} down</span>` : "";
      tile.innerHTML = `
        <div class="pt-head"><span class="dot" style="background:${color(i)}"></span><span class="pt-name">${p.name}</span></div>
        <div class="pt-stats">
          <span><b>${p.open_now}</b>/${p.roster} open</span>
          ${down}
          ${momentumChip(p.momentum)}
        </div>`;
      strip.appendChild(tile);
    });
  }

  function renderLegend(parks) {
    const el = $("legend"); el.innerHTML = "";
    parks.forEach((p, i) => {
      const s = document.createElement("span");
      s.innerHTML = `<i style="background:${color(i)}"></i>${p.name}`;
      el.appendChild(s);
    });
  }

  // ── radars ────────────────────────────────────────────────────────────
  function setEmpty(panelId, empty) { $(panelId).classList.toggle("is-empty", empty); }

  function radarData(parks, axes) {
    const labels = axes.map((a) => a.label);
    const datasets = parks.map((p, i) => {
      const raw = axes.map((a) => p[a.key]);
      const scaled = axes.map((a, k) => {
        const v = raw[k];
        return v === null || v === undefined ? 0 : clamp((v / a.cap) * 100, 0, 100);
      });
      const c = color(i);
      return {
        label: p.name, data: scaled, _raw: raw,
        borderColor: c, backgroundColor: c + "22",
        pointBackgroundColor: c, pointBorderColor: c,
        borderWidth: 2, pointRadius: 3, pointHoverRadius: 5,
      };
    });
    return { labels, datasets };
  }

  function radarOptions(axes) {
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
      scales: {
        r: {
          min: 0, max: 100, beginAtZero: true,
          grid: { color: C.grid }, angleLines: { color: C.angle },
          pointLabels: { color: C.text, font: { family: "JetBrains Mono", size: 11 } },
          ticks: { display: false, stepSize: 25, backdropColor: "transparent" },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#0a0d13", borderColor: "rgba(255,255,255,0.1)", borderWidth: 1,
          titleColor: C.text, bodyColor: C.tick, padding: 10,
          titleFont: { family: "JetBrains Mono", size: 11, weight: "600" },
          bodyFont: { family: "JetBrains Mono", size: 11 },
          callbacks: {
            title: (items) => (items.length ? axes[items[0].dataIndex].label : ""),
            label: (ctx) => {
              const a = axes[ctx.dataIndex], raw = ctx.dataset._raw[ctx.dataIndex];
              return ` ${ctx.dataset.label}: ${raw === null || raw === undefined ? "—" : fmt(raw)}${a.unit}`;
            },
          },
        },
      },
    };
  }

  function renderRadar(id, panelId, parks, axes) {
    const empty = !parks.length || !parks.some((p) => axes.some((a) => p[a.key] !== null && p[a.key] !== undefined));
    setEmpty(panelId, empty);
    if (empty) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } return; }
    const data = radarData(parks, axes);
    if (charts[id]) { charts[id].data = data; charts[id].update("none"); }
    else charts[id] = new Chart($(id), { type: "radar", data, options: radarOptions(axes) });
  }

  function render(res) {
    const parks = (res.parks || []).map(derive);
    const caps = res.caps || {};
    $("dest-label").textContent = res.destination ? res.destination.full_name : "";
    const wlabel = { today: "today", week: "7-day", month: "30-day" }[res.window] || res.window;
    $("busy-meta").textContent = `${wlabel} · normalized`;
    renderLive(parks);
    renderLegend(parks);
    renderRadar("chart-busy", "panel-busy", parks, busyAxes(caps));
    renderRadar("chart-ops", "panel-ops", parks, opsAxes(caps));
    $("note").textContent =
      "Crowd index = wait vs each ride's own typical wait for the hour (100 = normal). " +
      "Radar rims are fixed absolute values — small polygons mean a quiet/healthy park.";
  }

  // ── data flow ─────────────────────────────────────────────────────────
  async function refresh() {
    if (!destId) return;
    try {
      const r = await fetch(`/api/parks/compare?destination=${encodeURIComponent(destId)}&window=${windowSel}`);
      if (!r.ok) throw 0;
      render(await r.json()); stamp(); setStatus(true, "live");
    } catch (e) { setStatus(false, "offline"); }
  }

  async function init() {
    for (const b of $("window-toggle").children) {
      b.addEventListener("click", () => { if (windowSel === b.dataset.w) return; windowSel = b.dataset.w; syncToggles(); refresh(); });
    }
    try {
      destinations = await (await fetch("/api/destinations")).json();
    } catch (e) { setStatus(false, "offline"); return; }
    if (!destinations.length) { setStatus(false, "no data"); return; }
    destId = destinations[0].id;
    buildDestToggle(); syncToggles();
    await refresh();
    timer = setInterval(refresh, REFRESH_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();

"use strict";
/* Front end for the forecast page.
 *
 * Deliberately parallel to route.js: no framework, no charting library, inline
 * SVG. The one rule this file enforces beyond drawing is that the model's number
 * never appears without its context — the horizon banner renders before the cards
 * and carries the measured skill at that horizon, and the ride table always shows
 * the incumbent estimate beside the prediction.
 */

const ORD = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"];
const DIM = "#98a0b3";
const INK = "#e8eaf0";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hhmm = (h) => `${String(h).padStart(2, "0")}:00`;

let PAYLOAD = null;
let SELECTED = null;

function showTip(event, html) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.style.opacity = "1";
  const box = tip.getBoundingClientRect();
  let x = event.clientX + 13;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 13;
  tip.style.left = `${x}px`;
  tip.style.top = `${Math.max(8, event.clientY - box.height - 11)}px`;
}
const hideTip = () => { $("tip").style.opacity = "0"; };

/* ---------- the horizon banner ---------- */

function banner(d) {
  const el = $("banner");
  const s = d.skill;
  const trained = d.trained_on;
  let verdict;
  let tone;
  if (!s.available) {
    verdict = "No skill measurement exists for this horizon.";
    tone = "warn";
  } else if (s.spans_zero) {
    verdict = `Measured skill against the ${s.baseline.replace(/_/g, " ")} estimate is `
      + `<span class="num">${s.value >= 0 ? "+" : ""}${s.value.toFixed(3)}</span>, but its `
      + `90% interval <span class="num">${s.lo.toFixed(3)} to ${s.hi.toFixed(3)}</span> `
      + `spans zero — at this horizon the model is not measurably better.`;
    tone = "warn";
  } else {
    verdict = `Measured skill against the ${s.baseline.replace(/_/g, " ")} estimate is `
      + `<span class="num">+${s.value.toFixed(3)}</span> `
      + `(90% interval <span class="num">${s.lo.toFixed(3)} to ${s.hi.toFixed(3)}</span>, `
      + `excluding zero), from <span class="num">${s.n_rows.toLocaleString()}</span> `
      + `predictions over <span class="num">${s.n_origins}</span> retraining origins.`;
    tone = "ok";
  }
  el.className = `banner ${tone}`;
  el.innerHTML = `
    <div class="head">${d.horizon}-day-ahead forecast for ${esc(d.weekday)} ${esc(d.date)}</div>
    <div class="body">
      The model's information ends <span class="num">${esc(d.origin)}</span> — the last
      date with completed hourly rollups — so today's date is not the starting point.
      ${verdict}
      <br>Trained on <span class="num">${trained.park_days}</span> park-days across
      <span class="num">${trained.attractions}</span> attractions,
      <span class="num">${esc(trained.first_date)}</span> to
      <span class="num">${esc(trained.last_date)}</span>.
      Bands are the ${Math.round(d.interval_level * 100)}% range of this model's own
      past error at this horizon, not a probability about your day.
    </div>`;
}

/* ---------- park cards, each with a sparkline of its predicted day ---------- */

function sparkline(park) {
  const rides = park.attractions.filter((a) => a.estimate && a.hourly.length);
  if (!rides.length) return "";
  const hours = rides[0].hourly.map((h) => h.hour);
  const mean = hours.map((_, i) => {
    const vals = rides.map((r) => (r.hourly[i] ? r.hourly[i].wait : null))
      .filter((v) => v !== null);
    return vals.reduce((a, b) => a + b, 0) / (vals.length || 1);
  });
  const lo = 0;
  const hi = Math.max(...mean) * 1.15 || 1;
  const w = 200;
  const h = 34;
  const x = (i) => (i / Math.max(1, mean.length - 1)) * w;
  const y = (v) => h - ((v - lo) / (hi - lo)) * h;
  const line = mean.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  const area = `${line} L${w},${h} L0,${h} Z`;
  return `<svg viewBox="0 0 ${w} ${h}" role="img"
      aria-label="predicted park-wide wait by hour">
    <path d="${area}" fill="${ORD[3]}" opacity="0.16"/>
    <path d="${line}" fill="none" stroke="${ORD[3]}" stroke-width="2"/>
  </svg>`;
}

function cards(d) {
  const host = $("cards");
  host.innerHTML = "";
  d.parks.forEach((park) => {
    const delta = park.day_mean !== null && park.baseline_day_mean !== null
      ? park.day_mean - park.baseline_day_mean : null;
    const el = document.createElement("div");
    el.className = `card${park.park_id === SELECTED ? " sel" : ""}`;
    el.innerHTML = `
      <div class="k">${esc(park.park)}</div>
      <div class="v">${park.day_mean === null ? "—" : park.day_mean.toFixed(1)}
        <span style="font-size:12px;font-weight:400;color:${DIM}">min</span></div>
      <div class="n">${park.open}–${park.close} · ${park.hours}h
        ${park.party_night ? '· <span class="pill warn">party</span>' : ""}</div>
      <div class="n">incumbent ${park.baseline_day_mean === null ? "—"
        : park.baseline_day_mean.toFixed(1)}${delta === null ? ""
        : ` · ${delta >= 0 ? "+" : ""}${delta.toFixed(1)}`}</div>
      ${sparkline(park)}`;
    el.onclick = () => { SELECTED = park.park_id; render(); };
    host.appendChild(el);
  });
}

/* ---------- the ride table ---------- */

function rideTable(park) {
  const body = document.querySelector("#rides tbody");
  body.innerHTML = "";
  const rides = park.attractions.filter((a) => a.estimate);
  const worst = Math.max(...rides.map((r) => r.day_hi), 1);
  rides.forEach((r) => {
    const delta = r.baseline === null ? null : r.day_mean - r.baseline;
    const tone = r.band === "headliner" ? "bad" : r.band === "walk-on" ? "ok" : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(r.name)} <span class="pill ${tone}">${r.band}</span></td>
      <td class="num">${r.day_mean.toFixed(1)}</td>
      <td class="num" style="white-space:nowrap">
        ${r.day_lo.toFixed(0)}–${r.day_hi.toFixed(0)}
        <span class="bar" style="width:${(r.day_hi / worst * 46).toFixed(1)}px;
          background:${ORD[Math.min(5, Math.floor(r.day_mean / 12))]}"></span></td>
      <td class="num">${hhmm(r.peak_hour)}</td>
      <td class="num">${r.baseline === null ? "—" : r.baseline.toFixed(1)}</td>
      <td class="num" style="color:${delta === null ? DIM
        : delta > 0 ? "var(--warm)" : "var(--good)"}">
        ${delta === null ? "—" : (delta >= 0 ? "+" : "") + delta.toFixed(1)}</td>
      <td><span class="pill">${esc(r.tier)}</span></td>`;
    body.appendChild(tr);
  });
  const missing = park.attractions.length - rides.length;
  $("rides-note").innerHTML =
    `${rides.length} attractions with an estimate`
    + (missing ? `; <b>${missing}</b> without one — a ride that opened after
       ${esc(PAYLOAD.origin)} has no history to build a shape from, and is reported
       as having no estimate rather than given a number.` : ".")
    + ` &quot;Incumbent&quot; is the weekday-by-hour average over the 28 days before
       the origin — the estimate the route optimizer uses, and the one the skill
       score above is measured against. &quot;Evidence&quot; is how specific the
       underlying history is: <code>cell</code> means this ride at this hour.`;
}

/* ---------- attraction x hour heatmap ---------- */

function heatmap(park) {
  const host = $("heatmap");
  host.innerHTML = "";
  const rides = park.attractions.filter((a) => a.estimate && a.hourly.length);
  if (!rides.length) { host.innerHTML = `<div class="note">No estimates.</div>`; return; }
  const hours = rides[0].hourly.map((h) => h.hour);
  const labelW = 210;
  const cell = Math.max(16, Math.min(34, Math.floor((860 - labelW) / hours.length)));
  const rowH = 15;
  const top = 18;
  const w = labelW + hours.length * cell + 8;
  const h = top + rides.length * rowH + 6;
  const peak = Math.max(...rides.flatMap((r) => r.hourly.map((x) => x.wait)), 1);

  let g = "";
  hours.forEach((hour, i) => {
    g += `<text x="${(labelW + i * cell + cell / 2).toFixed(1)}" y="11"
      text-anchor="middle" font-size="9.5" fill="${DIM}"
      font-family="JetBrains Mono,monospace">${hour}</text>`;
  });
  rides.forEach((r, row) => {
    const y = top + row * rowH;
    g += `<text x="${labelW - 7}" y="${y + rowH - 4.5}" text-anchor="end"
      font-size="10" fill="${INK}">${esc(r.name.slice(0, 30))}</text>`;
    r.hourly.forEach((pt, i) => {
      const t = Math.min(0.999, pt.wait / peak);
      const colour = ORD[Math.floor(t * ORD.length)];
      g += `<rect x="${(labelW + i * cell).toFixed(1)}" y="${y}"
        width="${cell - 2}" height="${rowH - 2}" rx="3" fill="${colour}"
        data-t="${esc(r.name)}|${pt.hour}|${pt.wait}|${pt.lo}|${pt.hi}"/>`;
    });
  });
  host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img"
     aria-label="predicted wait per attraction per hour">${g}</svg>`;
  host.querySelectorAll("rect[data-t]").forEach((rect) => {
    rect.onmousemove = (event) => {
      const [name, hour, wait, lo, hi] = rect.dataset.t.split("|");
      showTip(event, `<b>${esc(name)}</b><br>${hhmm(hour)} &nbsp; ${wait} min`
        + `<br><span style="color:${DIM}">80% band ${lo}–${hi}</span>`);
    };
    rect.onmouseleave = hideTip;
  });
}

/* ---------- wiring ---------- */

function render() {
  const d = PAYLOAD;
  if (!d) return;
  banner(d);
  cards(d);
  if (!d.parks.some((p) => p.park_id === SELECTED)) SELECTED = d.parks[0].park_id;
  const park = d.parks.find((p) => p.park_id === SELECTED);
  $("park-name").textContent = park.park;
  $("park").value = SELECTED;
  rideTable(park);
  heatmap(park);
  $("footer").innerHTML =
    `Model: ridge on the ${esc(d.horizon_band)}-day horizon, trained at origin
     ${esc(d.origin)}; smearing correction ${d.smearing} applied to the log
     back-transform. ${esc(d.disclosure)}
     <br>Predictions come from committed model coefficients, a per-attraction hourly
     shape, and the published park schedule — nothing is refitted per request.`;
}

async function load(date) {
  document.body.classList.add("busy");
  try {
    const response = await fetch(`/api/forecast?date=${encodeURIComponent(date)}`);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      $("banner").className = "banner warn";
      $("banner").innerHTML = `<div class="head">No forecast for ${esc(date)}</div>
        <div class="body">${esc(detail.detail || response.statusText)}</div>`;
      return;
    }
    PAYLOAD = await response.json();
    const park = $("park");
    if (!park.options.length) {
      PAYLOAD.parks.forEach((p) => {
        const option = document.createElement("option");
        option.value = p.park_id;
        option.textContent = p.park;
        park.appendChild(option);
      });
      park.onchange = () => { SELECTED = park.value; render(); };
    }
    render();
  } finally {
    document.body.classList.remove("busy");
  }
}

async function init() {
  const today = new Date().toISOString().slice(0, 10);
  const info = await (await fetch(`/api/forecast/dates?not_before=${today}`)).json();
  const select = $("date");
  info.dates.forEach((row) => {
    const option = document.createElement("option");
    option.value = row.date;
    option.textContent = `${row.weekday.slice(0, 3)} ${row.date} — ${row.horizon} days ahead`;
    select.appendChild(option);
  });
  $("limit").innerHTML = esc(info.limit_reason);
  select.onchange = () => load(select.value);
  if (info.dates.length) load(info.dates[0].date);
}

init();

"use strict";
/* Route optimizer front end.
 *
 * Everything is hand-drawn inline SVG rather than a charting library: the park
 * map is a projection, not a chart, and the timeline needs stacked wait/ride
 * segments positioned on a clock axis. Both are a few lines of geometry, and
 * avoiding a CDN keeps this runnable offline alongside the rest of the package.
 */

const $ = (id) => document.getElementById(id);
const C = {
  wait: "#7c6ae0", ride: "#6ea8fe", skip: "#4a5266", route: "#6ea8fe",
  warm: "#f0a35e",
  must: "#5ecf8f",   // a must-do ride, so the commitments are findable at a glance
  show: "#f0a35e",   // a performance
  hold: "#6b552e",   // holding a spot for a show, which is not queueing
  meal: "#2b3040",   // the break band
};

const state = {
  dates: [], busy: false,
  attractions: [], maxPicks: 5,
  ranked: [],            // attraction ids, highest priority first
  shows: [],             // show keys the visitor ticked
  showInfo: [],          // what /api/shows said about this date
  planned: false,
};

const clock = (m) => {
  const t = Math.round(m);
  return String(Math.floor(t / 60) % 24).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
};

/* ---------- park map ---------- */

function drawMap(data) {
  const pts = [...data.stops, ...data.skipped];
  if (!pts.length) return "";
  const lats = pts.map((p) => p.lat), lons = pts.map((p) => p.lon);
  const pad = 0.0006;
  const minLat = Math.min(...lats) - pad, maxLat = Math.max(...lats) + pad;
  const minLon = Math.min(...lons) - pad, maxLon = Math.max(...lons) + pad;
  // Longitude degrees are shorter than latitude degrees away from the equator;
  // without the cosine correction the park comes out stretched east-west.
  const midLat = (minLat + maxLat) / 2;
  const lonScale = Math.cos((midLat * Math.PI) / 180);
  const W = 560, H = 430, m = 26;
  const spanX = (maxLon - minLon) * lonScale, spanY = maxLat - minLat;
  const scale = Math.min((W - 2 * m) / spanX, (H - 2 * m) / spanY);
  const X = (lon) => m + (lon - minLon) * lonScale * scale;
  const Y = (lat) => H - m - (lat - minLat) * scale;   // north is up

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Route through the park">`;
  svg += `<rect width="${W}" height="${H}" fill="#11141a" rx="8"/>`;

  // skipped attractions first, so the route draws over them
  for (const s of data.skipped) {
    svg += `<circle cx="${X(s.lon).toFixed(1)}" cy="${Y(s.lat).toFixed(1)}" r="4.5"
      fill="none" stroke="${C.skip}" stroke-width="1.5"><title>${esc(s.name)} — skipped, ${s.mean_wait} min mean wait</title></circle>`;
  }

  // the walked path, entrance first
  const path = [data.entrance, ...data.stops];
  const d = path.map((p, i) => `${i ? "L" : "M"}${X(p.lon).toFixed(1)},${Y(p.lat).toFixed(1)}`).join(" ");
  svg += `<path d="${d}" fill="none" stroke="${C.route}" stroke-width="1.6"
    stroke-opacity=".55" stroke-linejoin="round"/>`;

  svg += `<circle cx="${X(data.entrance.lon).toFixed(1)}" cy="${Y(data.entrance.lat).toFixed(1)}"
    r="6" fill="none" stroke="${C.warm}" stroke-width="2"><title>Park entrance</title></circle>`;

  data.stops.forEach((s) => {
    const x = X(s.lon).toFixed(1), y = Y(s.lat).toFixed(1);
    svg += `<circle cx="${x}" cy="${y}" r="9.5" fill="#1b2030" stroke="${C.route}" stroke-width="1.4"/>`;
    svg += `<text x="${x}" y="${y}" fill="#dfe5f5" font-size="9" font-family="JetBrains Mono,monospace"
      text-anchor="middle" dominant-baseline="central">${s.n}</text>`;
    svg += `<title>${s.n}. ${esc(s.name)} — arrive ${s.arrive}, wait ${s.wait} min</title>`;
  });

  svg += "</svg>";
  return svg;
}

/* ---------- day timeline ---------- */

function drawTimeline(data) {
  const open = data.open_minute, close = Math.max(data.close_minute, data.finish_minute);
  const W = 520, rowH = 15, m = { l: 8, r: 10, t: 20, b: 6 };
  const H = m.t + m.b + data.stops.length * rowH;
  const X = (min) => m.l + ((min - open) / (close - open)) * (W - m.l - m.r);

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Timeline of the day">`;
  // hour gridlines
  for (let h = Math.ceil(open / 60) * 60; h <= close; h += 60) {
    svg += `<line x1="${X(h).toFixed(1)}" y1="${m.t - 6}" x2="${X(h).toFixed(1)}" y2="${H - m.b}"
      stroke="#232833" stroke-width="1"/>`;
    svg += `<text x="${X(h).toFixed(1)}" y="${m.t - 10}" fill="#6f7789" font-size="8"
      font-family="JetBrains Mono,monospace" text-anchor="middle">${clock(h)}</text>`;
  }
  // The break, drawn behind everything as a band: it is a property of the whole day
  // rather than a stop, which is exactly how the model treats it.
  if (data.lunch) {
    const xa = X(data.lunch.start_minute);
    const xb = X(data.lunch.start_minute + data.lunch.minutes);
    svg += `<rect x="${xa.toFixed(1)}" y="${m.t - 6}" width="${Math.max(1, xb - xa).toFixed(1)}"
      height="${(H - m.b - m.t + 6).toFixed(1)}" fill="${C.meal}" opacity="0.5"
      ><title>Break ${data.lunch.start}–${data.lunch.end}</title></rect>`;
  }
  data.stops.forEach((s, i) => {
    const y = m.t + i * rowH + 3;
    const xw = X(s.arrive_minute), xb = X(s.arrive_minute + s.wait), xe = X(s.leave_minute);
    // For a show the first block is holding a spot, not queueing, and the second is
    // the performance. Same geometry, different meaning, so a different colour.
    const holdColour = s.kind === "show" ? C.hold : C.wait;
    const mainColour = s.kind === "show" ? C.show : (s.must_do ? C.must : C.ride);
    const holdLabel = s.kind === "show" ? "holding a spot" : "queue";
    svg += `<rect x="${xw.toFixed(1)}" y="${y}" width="${Math.max(0.8, xb - xw).toFixed(1)}"
      height="8" fill="${holdColour}" rx="1.5"><title>${esc(s.name)} — ${holdLabel} ${s.wait} min</title></rect>`;
    svg += `<rect x="${xb.toFixed(1)}" y="${y}" width="${Math.max(0.8, xe - xb).toFixed(1)}"
      height="8" fill="${mainColour}" rx="1.5"><title>${esc(s.name)} — ${s.kind === "show" ? "performance" : "ride"} ${s.ride} min</title></rect>`;
  });
  // closing time
  svg += `<line x1="${X(data.close_minute).toFixed(1)}" y1="${m.t - 6}"
    x2="${X(data.close_minute).toFixed(1)}" y2="${H - m.b}" stroke="${C.warm}"
    stroke-width="1.2" stroke-dasharray="3 3"/>`;
  svg += "</svg>";
  return svg;
}

/* ---------- rendering ---------- */

const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function render(data) {
  const optimal = data.proven_optimal
    ? `<span class="pill ok">proven optimal</span>`
    : `<span class="pill warn">bound ${data.upper_bound}, gap ${data.gap}</span>`;

  $("cards").innerHTML = `
    <div class="card"><div class="k">Attractions</div>
      <div class="v">${data.count}<span style="font-size:15px;color:var(--dim)">/${data.roster}</span></div>
      <div class="n">${optimal}</div></div>
    <div class="card"><div class="k">Window</div><div class="v">${data.window_hours}h</div>
      <div class="n">${data.open}–${data.close}${data.party_night ? " · party" : ""}</div></div>
    <div class="card"><div class="k">Finish</div><div class="v">${data.finish}</div>
      <div class="n">${data.slack_minutes} min before last queue</div></div>
    <div class="card"><div class="k">Queueing</div><div class="v">${data.waiting_minutes}</div>
      <div class="n">minutes in line${data.must_do_wait ? ` · ${data.must_do_wait} on your picks` : ""}</div></div>
    <div class="card"><div class="k">Walking</div><div class="v">${data.walking_minutes}</div>
      <div class="n">minutes on foot</div></div>
    <div class="card"><div class="k">Riding</div><div class="v">${data.riding_minutes}</div>
      <div class="n">minutes on rides</div></div>
    ${data.show_minutes || data.break_minutes ? `
    <div class="card"><div class="k">Committed</div>
      <div class="v">${Math.round(data.show_minutes + data.break_minutes)}</div>
      <div class="n">${data.show_minutes ? `${Math.round(data.show_minutes)} min shows` : ""}${
        data.show_minutes && data.break_minutes ? " · " : ""}${
        data.break_minutes ? `${Math.round(data.break_minutes)} min break` : ""}</div></div>` : ""}`;

  $("map").innerHTML = drawMap(data);
  $("timeline").innerHTML = drawTimeline(data);

  const cell = (data.support.cell || 0) * 100;
  $("map-note").innerHTML =
    `Numbered stops in order from the entrance (amber). Hollow circles are skipped.
     Straight lines connect stops — the distances behind them are routed over real footpaths,
     but the drawing does not trace them.`;

  const legend = [
    `<span style="color:${C.must}">&#9632;</span> your must-dos`,
    `<span style="color:${C.ride}">&#9632;</span> rides the plan added`,
    `<span style="color:${C.wait}">&#9632;</span> queueing`,
  ];
  if (data.shows.some((x) => x.scheduled)) {
    legend.push(`<span style="color:${C.show}">&#9632;</span> performance`);
    legend.push(`<span style="color:${C.hold}">&#9632;</span> holding a spot`);
  }
  if (data.lunch) legend.push(`<span style="color:#4a5266">&#9632;</span> break`);
  // Written into a dedicated element, not inserted after the chart: appending on
  // every refresh would stack a new legend under the old ones.
  $("timeline-legend").innerHTML =
    `${legend.join(" &nbsp; ")}<br>Gaps between bars are walking.`;

  $("stops").querySelector("tbody").innerHTML = data.stops.map((s) => {
    // A commitment and a filler stop read identically in a plain table, which is
    // the one distinction a visitor scanning this actually needs.
    const tag = s.must_do
      ? `<span class="pill ok">must-do</span>`
      : s.kind === "show"
        ? `<span class="pill warn">show</span>`
        : "";
    return `<tr><td class="num">${s.n}</td><td class="num">${s.arrive}</td>
      <td>${esc(s.name)} ${tag}</td>
      <td class="num" style="color:${s.wait > 30 ? C.warm : "inherit"}">${s.wait}</td>
      <td class="num">${s.ride}</td>
      <td><span class="pill">${esc(s.tier)}</span></td></tr>`;
  }).join("");

  $("skip-count").textContent = `${data.skipped.length}`;
  const bySize = [...data.skipped].sort((a, b) => (b.mean_wait + b.ride) - (a.mean_wait + a.ride));
  $("skipped").innerHTML = bySize.map((s) =>
    `<li>${esc(s.name)} — <span class="w">${s.mean_wait}</span> min mean wait,
     <span class="w">${s.ride}</span> min ride</li>`).join("")
    || "<li>nothing — the whole roster fits</li>";
  $("skip-note").innerHTML = data.skipped.length
    ? `Sorted by total time cost. A count-maximizer discards on <em>wait plus ride</em>,
       which is why long shows go even when their queues are short.`
    : "";

  $("footer").innerHTML =
    `Solved in ${data.seconds}s over ${data.evaluations.toLocaleString()} evaluations ·
     wait estimates ${cell.toFixed(0)}% direct evidence on the stops used ·
     assumptions <code>${data.assumptions_key}</code><br>
     Walking distances © OpenStreetMap contributors. Ride durations are estimates, not data.`;
}

/* ---------- wiring ---------- */


/* ---------- onboarding ---------- */

function renderPicks() {
  const chosen = new Set(state.ranked);
  const full = state.ranked.length >= state.maxPicks;
  $("picks").innerHTML = state.attractions.map((a) => {
    const on = chosen.has(a.id);
    const rank = on ? state.ranked.indexOf(a.id) + 1 : null;
    const cls = `pick${on ? " on" : ""}${!on && full ? " full" : ""}`;
    return `<div class="${cls}" data-id="${esc(a.id)}">
      ${on ? `<span class="rk">${rank}</span>` : ""}
      <span class="nm">${esc(a.name)}</span>
      <span class="mw">${a.mean_wait}m</span></div>`;
  }).join("");
  $("picks").querySelectorAll(".pick").forEach((el) => {
    el.addEventListener("click", () => togglePick(el.dataset.id));
  });
  renderRanked();
  $("build-note").textContent = state.ranked.length
    ? `${state.ranked.length} of ${state.maxPicks} picked`
    : "Pick at least one, or build a plan with no commitments at all.";
}

function togglePick(id) {
  const at = state.ranked.indexOf(id);
  if (at >= 0) state.ranked.splice(at, 1);
  else if (state.ranked.length < state.maxPicks) state.ranked.push(id);
  else return;
  renderPicks();
}

function move(index, delta) {
  const to = index + delta;
  if (to < 0 || to >= state.ranked.length) return;
  const [item] = state.ranked.splice(index, 1);
  state.ranked.splice(to, 0, item);
  renderPicks();
}

function renderRanked() {
  const names = Object.fromEntries(state.attractions.map((a) => [a.id, a.name]));
  if (!state.ranked.length) { $("ranked").innerHTML = ""; return; }
  $("ranked").innerHTML = state.ranked.map((id, i) => `<li>
    <span class="pos">${i + 1}</span>
    <span class="nm">${esc(names[id] || id)}</span>
    <button data-i="${i}" data-d="-1" ${i === 0 ? "disabled" : ""} title="More important">&uarr;</button>
    <button data-i="${i}" data-d="1" ${i === state.ranked.length - 1 ? "disabled" : ""} title="Less important">&darr;</button>
    <button data-i="${i}" data-d="0" title="Remove">&times;</button>
  </li>`).join("");
  $("ranked").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      const i = Number(b.dataset.i), d = Number(b.dataset.d);
      if (d === 0) { state.ranked.splice(i, 1); renderPicks(); } else move(i, d);
    });
  });
}

async function loadShows() {
  const date = $("date").value;
  const info = await (await fetch(`/api/shows?date=${encodeURIComponent(date)}`)).json();
  state.showInfo = info.shows;
  // A refused show must not stay ticked from a previous date.
  state.shows = state.shows.filter(
    (k) => (info.shows.find((s) => s.key === k) || {}).available
  );
  $("show-options").innerHTML = info.shows.map((sh) => {
    const on = state.shows.includes(sh.key);
    const evidence = sh.measured
      ? `start time seen on ${sh.n_dates} captured date${sh.n_dates === 1 ? "" : "s"}`
      : `start time is a single observed reference, not yet measured history`;
    return `<label class="showrow${sh.available ? "" : " off"}">
      <input type="checkbox" data-key="${esc(sh.key)}" ${on ? "checked" : ""}
        ${sh.available ? "" : "disabled"}>
      <span>
        <b>${esc(sh.name)}</b> — ${sh.start}, be in position by ${sh.arrive_by}
        <div class="meta">${sh.available
          ? `${esc(evidence)} · routed via ${esc(sh.proxy_attraction)}, ${sh.proxy_offset_m}m from the real spot`
          : `<span style="color:var(--warm)">Not available: ${esc(sh.reason)}</span>`}</div>
      </span></label>`;
  }).join("");
  $("show-options").querySelectorAll("input").forEach((box) => {
    box.addEventListener("change", () => {
      const key = box.dataset.key;
      if (box.checked) { if (!state.shows.includes(key)) state.shows.push(key); }
      else state.shows = state.shows.filter((k) => k !== key);
    });
  });
}

function renderAsked(data) {
  const kept = data.must_do.filter((m) => m.honoured);
  const lost = data.must_do.filter((m) => !m.honoured);
  const bits = [];
  if (kept.length) bits.push(`${kept.length} must-do${kept.length === 1 ? "" : "s"}`);
  if (data.lunch) bits.push(`break ${data.lunch.start}`);
  const scheduled = data.shows.filter((s) => s.scheduled);
  if (scheduled.length) bits.push(scheduled.map((s) => s.name).join(" + "));
  $("asked").textContent = bits.length
    ? `Planned with: ${bits.join(" · ")}`
    : "Planned with no commitments — this is the pure count-maximizing route.";

  const out = [];
  if (lost.length) {
    out.push(`<div class="notice bad"><b>${lost.length} of your picks did not
      fit.</b> Ranked lowest-first, so the bottom of your list went first.<br>` +
      lost.map((m) => `&middot; <b>${esc(m.name)}</b> (rank ${m.rank}) — ${esc(m.reason)}`).join("<br>") +
      `</div>`);
  } else if (kept.length) {
    out.push(`<div class="notice ok">All <b>${kept.length}</b> of your must-dos fit,
      costing <b>${data.must_do_wait}</b> minutes of queueing between them.</div>`);
  }
  data.shows.filter((s) => !s.scheduled).forEach((s) => {
    out.push(`<div class="notice warn"><b>${esc(s.name)}</b> is not in this plan —
      ${esc(s.reason || "unavailable on this date")}.</div>`);
  });
  (data.risks || []).forEach((r) => {
    out.push(`<div class="notice warn">${esc(r.message)}</div>`);
  });
  if (!data.shows.some((s) => s.scheduled && s.measured) &&
      data.shows.some((s) => s.scheduled)) {
    out.push(`<div class="notice"><b>Show times are a reference, not history yet.</b>
      The feed publishes performances for today only, so we started recording them
      on first capture; until enough dates accrue, the parade and fireworks times
      here come from a single observation.</div>`);
  }
  $("notices").innerHTML = out.join("");
  $("narrative").innerHTML = (data.narrative || [])
    .map((line) => `<li>${esc(line)}</li>`).join("");
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  document.querySelector("main").classList.add("busy");
  const q = new URLSearchParams({
    date: $("date").value,
    walk_speed: $("walk").value,
    ride_scale: $("ride").value,
    crowd: $("crowd").value,
  });
  if (state.ranked.length) q.set("must_do", state.ranked.join(","));
  if (state.shows.length) q.set("shows", state.shows.join(","));
  if (!$("skip-lunch").checked) {
    q.set("lunch_start", $("lunch").value);
    q.set("lunch_minutes", $("lunch-min").value);
  }
  try {
    const response = await fetch(`/api/route?${q}`);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || response.statusText);
    }
    const data = await response.json();
    renderAsked(data);
    render(data);
  } catch (error) {
    $("cards").innerHTML = `<div class="card"><div class="k">Error</div>
      <div class="n">${esc(error.message)}</div></div>`;
  } finally {
    state.busy = false;
    document.querySelector("main").classList.remove("busy");
  }
}

function bindSlider(id, format) {
  const input = $(id), out = $(`${id}-v`);
  input.addEventListener("input", () => { out.textContent = format(input.value); });
  input.addEventListener("change", refresh);
}

async function init() {
  state.dates = await (await fetch("/api/dates")).json();
  $("date").innerHTML = state.dates.map((d) =>
    `<option value="${d.date}">${d.date} · ${d.weekday} · ${d.window_hours}h${d.party_night ? " · party" : ""}</option>`
  ).join("");
  // Default to the longest window, which is the most interesting starting point.
  const longest = state.dates.reduce((a, b) => (b.window_hours > a.window_hours ? b : a));
  $("date").value = longest.date;

  // Lunch options across the middle of the day, in 15-minute steps.
  const slots = [];
  for (let m = 11 * 60; m <= 15 * 60; m += 15) slots.push(m);
  $("lunch").innerHTML = slots
    .map((m) => `<option value="${m}"${m === 12 * 60 + 30 ? " selected" : ""}>${clock(m)}</option>`)
    .join("");

  const menu = await (await fetch("/api/attractions")).json();
  state.attractions = menu.attractions;
  state.maxPicks = menu.max_must_do;
  $("max-picks").textContent = menu.max_must_do;
  renderPicks();
  await loadShows();

  // Changing the date changes which shows exist, so the step reloads. While still
  // in onboarding it must NOT replan — nothing has been asked for yet.
  $("date").addEventListener("change", async () => {
    await loadShows();
    if (state.planned) refresh();
  });
  bindSlider("walk", (v) => `${Number(v).toFixed(2)} m/s`);
  bindSlider("ride", (v) => `${Number(v).toFixed(2)}x`);
  bindSlider("crowd", (v) => `${Number(v).toFixed(2)}x`);

  $("build").addEventListener("click", async () => {
    state.planned = true;
    $("onboarding").classList.add("hide");
    $("plan").classList.remove("hide");
    await refresh();
  });
  $("reopen").addEventListener("click", () => {
    $("plan").classList.add("hide");
    $("onboarding").classList.remove("hide");
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

init();

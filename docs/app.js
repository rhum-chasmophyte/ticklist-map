/* Bouldering ticklist map. Loads data/sends.json (written nightly by the pipeline). */
(function () {
  "use strict";

  // ---------------------------------------------------------------- settings
  const DATA_URL = "data/sends.json";
  const STYLE = {
    light: "https://tiles.openfreemap.org/styles/positron",
    dark: "https://tiles.openfreemap.org/styles/dark",
  };
  // Map grouping by zoom: below 2 = globe, 2-4 = nation, 4-6 = state/region, 6+ = crag
  const LEVELS = [
    { key: "globe", label: "Globe", below: 2 },
    { key: "nation", label: "Nation", below: 4 },
    { key: "state", label: "State", below: 6 },
    { key: "crag", label: "Crag", below: Infinity },
  ];
  const COUNTRY = { US: "United States", CA: "Canada", NO: "Norway", ZA: "South Africa" };
  const DEFAULT_VARS = ["tmax", "rmin", "dewpoint_spread", "days_since_rain"];
  const TEMP_VARS = new Set(["tmax", "tmin", "dewpoint"]);     // °C <-> °F with offset
  const TEMP_DIFF_VARS = new Set(["dewpoint_spread"]);         // a difference: scale only
  // What a low / high percentile means, per variable
  const WORDS = {
    tmax: ["Cooler afternoons", "Warmer afternoons"],
    tmin: ["Colder nights", "Milder nights"],
    rmax: ["Drier air (overnight)", "More humid air (overnight)"],
    rmin: ["Drier air (afternoon)", "More humid air (afternoon)"],
    sph: ["Drier air", "More humid air"],
    vpd: ["Less drying power", "More drying power"],
    dewpoint: ["Lower dewpoints", "Higher dewpoints"],
    dewpoint_spread: ["Damper air (small spread)", "Drier air (large spread)"],
    pr: ["Less rain on the day", "More rain on the day"],
    days_since_rain: ["Sooner after rain", "Longer after rain"],
    srad: ["Cloudier days", "Sunnier days"],
    pet: ["Lower evaporative demand", "Higher evaporative demand"],
    etr: ["Lower evaporative demand", "Higher evaporative demand"],
    wind: ["Calmer days", "Windier days"],
    erc: ["Lower fire danger", "Higher fire danger"],
    bi: ["Lower fire danger", "Higher fire danger"],
    fm100: ["Drier fuels", "Moister fuels"],
    fm1000: ["Drier fuels", "Moister fuels"],
  };
  const MIN_DAYS = 8;

  // ---------------------------------------------------------------- state
  const S = {
    window: "all", faOnly: false, unit: "F", tab: "top", mode: "pct",
    vars: new Set(DEFAULT_VARS), selected: null, level: null,
  };
  let D, CRAGS = {}, SENDS = [], VARS = [], map, markers = [], popup = null, today = new Date();

  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 0) => (n == null || !isFinite(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const isDark = () => {
    const t = document.documentElement.getAttribute("data-theme");
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };

  // ---------------------------------------------------------------- data prep
  function prep(d) {
    D = d;
    VARS = d.variables;
    d.crags.forEach((c) => (CRAGS[c.id] = c));
    // Approximate dates: use the pipeline's flag; fall back to the same rule for older data
    const perDate = {};
    d.sends.forEach((s) => s.date && (perDate[s.date] = (perDate[s.date] || 0) + 1));
    SENDS = d.sends
      .filter((s) => s.crag_id && CRAGS[s.crag_id])
      .map((s) => {
        const t = s.date ? new Date(s.date + "T12:00:00") : null;
        const approx = s.date_approx ?? (!!s.date && (perDate[s.date] > 20 || s.date.slice(5) === "01-01"));
        const g = parseInt(s.grade, 10);
        return Object.assign({}, s, { c: CRAGS[s.crag_id], t, approx, g: isNaN(g) ? null : g, pts: s.points || 0 });
      });
  }

  function inWindow(s) {
    if (S.window === "all") return true;
    return s.t && today - s.t <= 365 * 864e5;
  }
  const base = () => SENDS.filter((s) => inWindow(s) && (!S.faOnly || s.is_fa));

  function inView(s) {
    if (!map || map.getZoom() < 2) return true;
    return map.getBounds().contains([s.c.lon, s.c.lat]);
  }

  const score = (list) => list.map((s) => s.pts).sort((a, b) => b - a).slice(0, 10).reduce((a, b) => a + b, 0);
  const hardest = (list) => list.reduce((m, s) => (s.g != null && s.g > m ? s.g : m), -1);
  const grade = (g) => (g == null || g < 0 ? "–" : "V" + g);
  const tempUnit = () => (S.unit === "F" ? "°F" : "°C");

  function fmtDate(s) {
    if (!s.date) return "No date";
    const [y, m, d] = s.date.split("-").map(Number);
    if (s.approx) return s.date.slice(5) === "01-01" ? `${y} (year only)` : `~${MONTHS[m - 1]} ${y}`;
    return `${MONTHS[m - 1]} ${d}, ${y}`;
  }

  // unit conversion for display
  function conv(key, v) {
    if (v == null) return v;
    if (S.unit === "F" && TEMP_VARS.has(key)) return v * 9 / 5 + 32;
    if (S.unit === "F" && TEMP_DIFF_VARS.has(key)) return v * 9 / 5;
    return v;
  }
  function unitOf(v) {
    if (TEMP_VARS.has(v.key) || TEMP_DIFF_VARS.has(v.key)) return tempUnit();
    return v.unit;
  }
  const varMeta = (k) => VARS.find((v) => v.key === k);

  // ---------------------------------------------------------------- map
  function levelFor(z) { return LEVELS.find((l) => z < l.below); }

  function groupKey(c, level) {
    if (level === "crag") return c.id;
    if (level === "state") return c.country + "|" + c.state;
    if (level === "nation") return c.country;
    return "world";
  }
  function groupName(c, level) {
    if (level === "crag") return [c.name, `${c.state}, ${c.country}`];
    if (level === "state") return [c.state === c.country ? COUNTRY[c.country] || c.country : c.state, COUNTRY[c.country] || c.country];
    if (level === "nation") return [COUNTRY[c.country] || c.country, ""];
    return ["World", ""];
  }

  function buildGroups(level) {
    const groups = {};
    D.crags.forEach((c) => {
      const k = groupKey(c, level);
      if (!groups[k]) {
        const [name, sub] = groupName(c, level);
        groups[k] = { key: k, name, sub, crags: [], sends: [] };
      }
      groups[k].crags.push(c);
    });
    const byCrag = {};
    base().forEach((s) => (byCrag[s.crag_id] = byCrag[s.crag_id] || []).push(s));
    Object.values(groups).forEach((g) => {
      g.crags.forEach((c) => g.sends.push(...(byCrag[c.id] || [])));
      g.lon = g.crags.reduce((a, c) => a + c.lon, 0) / g.crags.length;
      g.lat = g.crags.reduce((a, c) => a + c.lat, 0) / g.crags.length;
      g.score = score(g.sends);
      g.fa = g.sends.filter((s) => s.is_fa).length;
      g.hardest = hardest(g.sends);
    });
    return Object.values(groups);
  }

  function renderMarkers() {
    markers.forEach((m) => m.remove());
    markers = [];
    const level = S.level.key;
    const groups = buildGroups(level);
    const max = Math.max(1, ...groups.map((g) => g.score));
    const selCrag = S.selected && S.selected.c;
    // crags crowd together (Front Range), so their labels wait until zoom 9
    const showLabels = level !== "crag" || map.getZoom() >= 9;
    // biggest first, so small circles sit on top and stay clickable
    groups.sort((a, b) => b.score - a.score).forEach((g) => {
      const r = g.score > 0 ? (level === "crag" ? 6 + 14 * Math.sqrt(g.score / max) : 9 + 21 * Math.sqrt(g.score / max)) : 5;
      const el = document.createElement("div");
      el.className = "mk" + (g.score ? "" : " empty") + (g.fa ? " fa" : "") +
        (selCrag && g.crags.includes(selCrag) ? " sel" : "");
      el.style.width = el.style.height = 2 * r + "px";
      el.innerHTML = `<div class="dot"></div>${g.fa ? '<span class="fa-badge" aria-hidden="true">★</span>' : ""}` +
        (showLabels ? `<div class="lbl"><b>${esc(g.name)}</b><br><span>${g.score ? fmt(g.score) : "–"}</span></div>` : "");
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", `${g.name}: 8a score ${fmt(g.score)}, ${g.sends.length} sends`);
      el.addEventListener("mouseenter", (e) => showTip(e, `<b>${esc(g.name)}</b>${g.sub ? ` <span class="m">${esc(g.sub)}</span>` : ""}<br>` +
        `8a score <b>${fmt(g.score)}</b> · ${g.sends.length} sends` + (g.fa ? ` · ★ ${g.fa} FA` : "") +
        (level !== "crag" ? `<br><span class="m">${g.crags.length} crags · click to zoom in</span>` : "")));
      el.addEventListener("mousemove", moveTip);
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("click", (e) => { e.stopPropagation(); hideTip(); clickGroup(g, level); });
      markers.push(new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([g.lon, g.lat]).addTo(map));
    });
    renderLegend();
  }

  function clickGroup(g, level) {
    if (level !== "crag") {
      if (g.crags.length === 1) {
        map.flyTo({ center: [g.crags[0].lon, g.crags[0].lat], zoom: 9 });
      } else {
        const b = new maplibregl.LngLatBounds();
        g.crags.forEach((c) => b.extend([c.lon, c.lat]));
        // Fit the group, but always step at least one level deeper (fitting the whole
        // world, for example, wouldn't change the zoom at all)
        const i = LEVELS.findIndex((l) => l.key === level);
        const minZ = LEVELS[i].below + 0.3;
        const maxZ = LEVELS[i + 1].below === Infinity ? 11 : LEVELS[i + 1].below - 0.3;
        const cam = map.cameraForBounds(b, { padding: 80 }) || { center: [g.lon, g.lat], zoom: minZ };
        map.easeTo({ center: cam.center, zoom: Math.min(maxZ, Math.max(minZ, cam.zoom)), duration: 900 });
      }
      return;
    }
    const c = g.crags[0];
    const list = g.sends;
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ offset: 14, maxWidth: "280px" })
      .setLngLat([c.lon, c.lat])
      .setHTML(`<div class="pop"><h3>${esc(c.name)}</h3><div class="where">${esc(c.state)}, ${esc(COUNTRY[c.country] || c.country)}</div>
        <dl><dt>8a score</dt><dd><b>${fmt(g.score)}</b></dd>
        <dt>Sends</dt><dd>${list.length}</dd>
        <dt>Hardest</dt><dd>${grade(g.hardest)}</dd>
        <dt>First ascents</dt><dd>${g.fa ? "★ " + g.fa : "–"}</dd>
        <dt>Elevation</dt><dd>${fmt(c.z_crag)} m</dd></dl>
        <div class="note">Weather: ${c.source === "gridmet" ? "gridMET (4 km)" : "ERA5-Land (9 km)"}, lapse-corrected ${c.dz >= 0 ? "+" : ""}${fmt(c.dz)} m from the grid cell's mean elevation.</div></div>`)
      .addTo(map);
  }

  function renderLegend() {
    $("#legend").innerHTML =
      `<div class="levels" aria-label="Grouping level">${LEVELS.map((l) => `<span class="${l.key === S.level.key ? "on" : ""}">${l.label}</span>`).join("")}</div>
       <div class="row"><i style="width:18px;height:18px;background:var(--series);opacity:.8"></i>
         <span>Size = 8a score (top 10 sends in the group)${S.faOnly ? ", FAs only" : ""}</span></div>
       <div class="row"><i style="width:12px;height:12px;background:var(--empty)"></i><span>No sends ${S.window === "1y" ? "in the last 365 days" : ""}</span></div>
       <div class="row"><i style="width:14px;height:14px;border:3px solid var(--accent)"></i><span><span class="star">★</span> Has first ascents</span></div>`;
  }

  function initMap() {
    const all = new maplibregl.LngLatBounds();
    D.crags.forEach((c) => all.extend([c.lon, c.lat]));
    map = new maplibregl.Map({
      container: "map", style: isDark() ? STYLE.dark : STYLE.light,
      bounds: all, fitBoundsOptions: { padding: 40 }, renderWorldCopies: false,
      attributionControl: { compact: true }, cooperativeGestures: window.self !== window.top,
    });
    window.ticklistMap = map;   // handy for debugging in the browser console
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    // If the basemap can't load (blocked network, outage), fall back to a plain background
    let styled = false, fellBack = false;
    const fallback = () => {
      if (styled || fellBack) return;
      fellBack = true;
      map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": isDark() ? "#20201f" : "#e9e7e2" } }] });
    };
    map.on("style.load", () => (styled = true));
    map.on("error", () => { if (!map.isStyleLoaded()) fallback(); });
    setTimeout(fallback, 8000);

    S.level = levelFor(map.getZoom());
    renderMarkers();
    map.on("zoomend", () => {
      const l = levelFor(map.getZoom());
      const namesChanged = S.level.key === "crag" && l.key === "crag";
      if (l.key !== S.level.key || namesChanged) { S.level = l; renderMarkers(); }
    });
    let t;
    map.on("moveend", () => { clearTimeout(t); t = setTimeout(renderPanels, 120); });
    map.on("click", () => popup && popup.remove());
  }

  // ---------------------------------------------------------------- header
  function renderHeader() {
    const list = base();
    const crags = new Set(list.map((s) => s.crag_id));
    const countries = new Set(list.map((s) => s.c.country));
    const items = [
      ["Sends", fmt(list.length)], ["Crags", fmt(crags.size)], ["Countries", fmt(countries.size)],
      ["Hardest", grade(hardest(list))], ["8a score", fmt(score(list))],
      ["First ascents", fmt(list.filter((s) => s.is_fa).length)],
    ];
    $("#stats").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
    const gen = D.generated ? new Date(D.generated) : null;
    $("#sub").textContent = `${S.window === "1y" ? "Last 365 days" : "All-time"}${S.faOnly ? " · first ascents only" : ""}` +
      (gen ? ` · updated ${MONTHS[gen.getMonth()]} ${gen.getDate()}, ${gen.getFullYear()}` : "");
  }

  // ---------------------------------------------------------------- panels
  function scoped() { return base().filter(inView); }

  function renderScope(list) {
    const all = base().length;
    const crags = new Set(list.map((s) => s.crag_id)).size;
    const label = list.length === all ? "All areas" : "Map view";
    $("#scope").innerHTML = `<b>${label}</b> · ${fmt(list.length)} sends at ${crags} crag${crags === 1 ? "" : "s"}` +
      (list.length !== all ? `<button type="button" id="reset">Show all</button>` : "");
    const r = $("#reset");
    if (r) r.onclick = () => { const b = new maplibregl.LngLatBounds(); D.crags.forEach((c) => b.extend([c.lon, c.lat])); map.fitBounds(b, { padding: 40 }); };
  }

  function sendRow(s, rank, showPts = true) {
    const sel = S.selected && S.selected.id === s.id;
    const badges = (s.is_fa ? `<span class="badge fa">★ FA</span>` : "") + (/flash/i.test(s.style) ? `<span class="badge">Flash</span>` : "");
    return `<li class="${sel ? "sel" : ""}"><button type="button" class="item" data-send="${s.id}" aria-expanded="${sel}">
      <span class="rank">${rank}</span>
      <span class="name">${esc(s.name)}${badges}</span>
      <span class="pts">${showPts ? fmt(s.pts) : `<span class="grade">${grade(s.g)}</span>`}</span>
      <span class="meta">${showPts ? grade(s.g) + " · " : ""}${esc(s.crag)}, ${esc(s.state)} · ${fmtDate(s)}</span>
    </button>${sel ? detail(s) : ""}</li>`;
  }

  function detail(s) {
    const cl = s.climate || {};
    if (cl.status !== "ok") return `<div class="detail"><div class="note">No weather data for this send.</div></div>`;
    if (s.approx) return `<div class="detail"><div class="note">The date is approximate, so send-day conditions aren't shown.</div></div>`;
    const rows = ["tmax", "rmin", "dewpoint_spread", "days_since_rain", "wind"].map((k) => {
      const v = varMeta(k); const val = cl.values[k];
      if (val == null || !v) return "";
      const p = cl.pct[k];
      const u = k === "days_since_rain" ? (val === 1 ? "day" : "days") : unitOf(v);
      return `<dt>${esc(v.label)}</dt><dd>${fmt(conv(k, val), k === "days_since_rain" ? 0 : 1)} ${esc(u)}</dd><dd class="p">${p == null ? "" : ordinal(Math.round(p)) + " pct"}</dd>`;
    }).join("");
    const grid = cl.tmax_grid != null ? `<div class="note">Max temperature corrected from ${fmt(conv("tmax", cl.tmax_grid), 1)} ${tempUnit()} (grid) to ${fmt(conv("tmax", cl.values.tmax), 1)} ${tempUnit()} at the crag's elevation.</div>` : "";
    return `<div class="detail">Send-day conditions, with percentile against the same crag and season:<dl>${rows}</dl>${grid}</div>`;
  }
  const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");

  function bindRows(el) {
    el.querySelectorAll("[data-send]").forEach((b) => (b.onclick = () => {
      const s = SENDS.find((x) => x.id === b.dataset.send);
      S.selected = S.selected && S.selected.id === s.id ? null : s;
      renderMarkers(); renderPanels();
    }));
  }

  function renderTop(list) {
    const top = [...list].sort((a, b) => b.pts - a.pts || (b.g ?? -1) - (a.g ?? -1) || (b.t || 0) - (a.t || 0)).slice(0, 10);
    const el = $("#tab-top");
    if (!top.length) { el.innerHTML = `<p class="empty-note">No sends in this view${S.window === "1y" ? " in the last 365 days" : ""}.</p>`; return; }
    el.innerHTML = `<p class="lead">Highest-scoring sends in view. Their points sum to the 8a score of <b>${fmt(score(list))}</b>.</p>
      <ol class="list">${top.map((s, i) => sendRow(s, i + 1)).join("")}</ol>`;
    bindRows(el);
  }

  function renderFA() {
    const list = SENDS.filter((s) => s.is_fa && inWindow(s) && inView(s))
      .sort((a, b) => (b.g ?? -1) - (a.g ?? -1) || (b.t || 0) - (a.t || 0));
    const el = $("#tab-fa");
    const crags = new Set(list.map((s) => s.crag_id)).size;
    el.innerHTML = `<div class="fa-summary"><div><b>${list.length}</b><span>First ascents</span></div>
        <div><b>${grade(hardest(list))}</b><span>Hardest</span></div><div><b>${crags}</b><span>Crags</span></div></div>
      <p class="lead">Problems climbed first. <button type="button" class="chip" id="faToggle" aria-pressed="${S.faOnly}"><span class="star">★</span> ${S.faOnly ? "Showing FAs only on the map" : "Show only FAs on the map"}</button></p>` +
      (list.length ? `<ol class="list">${list.map((s, i) => sendRow(s, i + 1, false)).join("")}</ol>` : `<p class="empty-note">No first ascents in this view.</p>`);
    $("#faToggle").onclick = toggleFA;
    bindRows(el);
  }

  // ---------------------------------------------------------------- conditions
  function climbingDays(list) {
    const days = {};
    list.forEach((s) => {
      if (s.approx || !s.climate || s.climate.status !== "ok") return;
      const k = s.crag_id + "|" + s.date;
      (days[k] = days[k] || { key: k, crag: s.c, date: s.date, s, sends: [], cl: s.climate }).sends.push(s);
    });
    return Object.values(days);
  }

  // Kolmogorov-Smirnov test of percentiles against an even spread from 0 to 100
  function ksP(p) {
    const x = p.map((v) => v / 100).sort((a, b) => a - b), n = x.length;
    if (n < MIN_DAYS) return null;
    let d = 0;
    x.forEach((v, i) => (d = Math.max(d, (i + 1) / n - v, v - i / n)));
    const lam = (Math.sqrt(n) + 0.12 + 0.11 / Math.sqrt(n)) * d;
    let s = 0;
    for (let k = 1; k <= 100; k++) s += 2 * (k % 2 ? 1 : -1) * Math.exp(-2 * k * k * lam * lam);
    return Math.min(1, Math.max(0, s));
  }
  const median = (a) => { const b = [...a].sort((x, y) => x - y), m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
  const pText = (p) => (p < 0.001 ? "p < 0.001" : "p = " + p.toFixed(p < 0.01 ? 3 : 2));

  function verdict(key, pcts) {
    if (pcts.length < MIN_DAYS) return { short: `Too few days (${pcts.length})`, sig: false };
    const p = ksP(pcts), med = median(pcts);
    const sig = p < 0.05 && Math.abs(med - 50) >= 5;
    const w = WORDS[key] || ["Lower than typical", "Higher than typical"];
    return {
      sig, p, med,
      short: sig ? `${w[med < 50 ? 0 : 1]} than typical · ${pText(p)}` : `No clear difference · ${pText(p)}`,
    };
  }

  function renderWx(list) {
    const el = $("#tab-wx");
    const days = climbingDays(list);
    const approxN = list.filter((s) => s.approx).length;
    const groups = {};
    VARS.forEach((v) => (groups[v.group] = groups[v.group] || []).push(v));
    const usOnly = (v) => (v.gridmet_only ? ' <small>(US only)</small>' : "");
    el.innerHTML = `
      <div class="wx-controls">
        <div class="seg" role="radiogroup" aria-label="Chart type">
          <button type="button" data-mode="pct" aria-pressed="${S.mode === "pct"}">Percentiles</button>
          <button type="button" data-mode="raw" aria-pressed="${S.mode === "raw"}">Actual vs typical</button>
        </div>
      </div>
      <details class="vars"${S.vars.size ? "" : " open"}><summary>Variables (${S.vars.size} selected)</summary>
        <div class="vargroups">${Object.entries(groups).map(([g, vs]) => `<div class="vargroup"><h4>${esc(g)}</h4>${vs.map((v) =>
          `<label><input type="checkbox" value="${v.key}" ${S.vars.has(v.key) ? "checked" : ""}>${esc(v.label)}${usOnly(v)}</label>`).join("")}</div>`).join("")}</div>
      </details>
      <p class="lead">${fmt(days.length)} climbing days in view${approxN ? `; ${approxN} send${approxN === 1 ? "" : "s"} with approximate dates ${approxN === 1 ? "isn't" : "aren't"} included` : ""}.</p>
      <div id="wxchart"></div>
      <details class="table"><summary>Summary table</summary><div id="wxtable"></div></details>
      <div class="method"><p>Each climbing day is ranked against the same crag's weather on days within ±${D.settings.window_days} days of that date, ${D.settings.baseline[0]}–${D.settings.baseline[1]}.
        The 50th percentile is a typical day for that crag and season. If conditions didn't matter, days would spread evenly from 0 to 100; the p-value (Kolmogorov–Smirnov test) says how unlikely the observed spread would be if that were true.</p>
        <p>Weather from gridMET (contiguous US) and ERA5-Land (elsewhere), corrected to crag elevation with a lapse rate of ${D.settings.lapse_c_per_km.tmax} °C/km for daytime highs and ${D.settings.lapse_c_per_km.tmin} °C/km for overnight lows. Rain days are &gt; ${D.settings.rain_threshold_mm} mm.</p></div>`;

    el.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => { S.mode = b.dataset.mode; renderWx(scoped()); }));
    el.querySelectorAll(".vargroups input").forEach((i) => (i.onchange = () => {
      i.checked ? S.vars.add(i.value) : S.vars.delete(i.value);
      $("details.vars summary", el).textContent = `Variables (${S.vars.size} selected)`;
      drawWx(days);
    }));
    drawWx(days);
  }

  function drawWx(days) {
    const host = $("#wxchart");
    const sel = VARS.filter((v) => S.vars.has(v.key));
    if (!sel.length) { host.innerHTML = `<p class="empty-note">Pick one or more variables above.</p>`; $("#wxtable").innerHTML = ""; return; }
    if (!days.length) { host.innerHTML = `<p class="empty-note">No climbing days with weather data in this view.</p>`; $("#wxtable").innerHTML = ""; return; }
    const selDay = S.selected && !S.selected.approx ? S.selected.crag_id + "|" + S.selected.date : null;
    if (S.mode === "pct") drawStrips(host, days, sel.filter((v) => v.pct !== false), selDay, sel.some((v) => v.pct === false));
    else drawMulti(host, days, sel, selDay);
    drawTable(days, sel.filter((v) => v.pct !== false));
  }

  // deterministic jitter so dots don't jump around between redraws
  function jitter(str) { let h = 0; for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0; return ((h >>> 0) % 1000) / 1000 - 0.5; }

  function drawStrips(host, days, vars, selDay, hasDir) {
    const W = Math.max(280, host.clientWidth || 400), padL = 8, padR = 8, rowH = 76, top = 6;
    const x = (p) => padL + (p / 100) * (W - padL - padR);
    let svg = `<svg class="chart" width="${W}" height="${top + vars.length * rowH + 34}" viewBox="0 0 ${W} ${top + vars.length * rowH + 34}" role="img" aria-label="Percentile of send-day conditions">`;
    const bottom = top + vars.length * rowH;
    [0, 25, 50, 75, 100].forEach((p) => (svg += `<line class="${p === 50 ? "mid" : "gridl"}" x1="${x(p)}" x2="${x(p)}" y1="${top}" y2="${bottom}"/>`));
    vars.forEach((v, i) => {
      const y0 = top + i * rowH, cy = y0 + 52;
      const pts = days.map((d) => ({ d, p: d.cl.pct[v.key] })).filter((o) => o.p != null);
      const vd = verdict(v.key, pts.map((o) => o.p));
      svg += `<text class="lab" x="${padL}" y="${y0 + 13}">${esc(v.label)}</text>`;
      svg += `<text class="verdict" x="${padL}" y="${y0 + 29}"${vd.sig ? ' style="fill:var(--text)"' : ""}>${esc(vd.short)}</text>`;
      svg += `<line class="axis" x1="${padL}" x2="${W - padR}" y1="${cy + 13}" y2="${cy + 13}"/>`;
      pts.forEach((o) => {
        const cx = x(o.p), yy = cy + jitter(o.d.key + v.key) * 18, on = o.d.key === selDay;
        if (on) return;
        svg += `<circle class="hit" cx="${cx}" cy="${yy}" r="7" data-day="${esc(o.d.key)}" data-var="${v.key}"/><circle class="d" cx="${cx}" cy="${yy}" r="4"/>`;
      });
      if (vd.med != null) svg += `<line class="med" x1="${x(vd.med)}" x2="${x(vd.med)}" y1="${cy - 12}" y2="${cy + 12}"><title>Median ${Math.round(vd.med)}</title></line>`;
      const s = pts.find((o) => o.d.key === selDay);
      if (s) svg += `<circle class="hit" cx="${x(s.p)}" cy="${cy}" r="8" data-day="${esc(s.d.key)}" data-var="${v.key}"/><circle class="d on" cx="${x(s.p)}" cy="${cy}" r="6"/>`;
    });
    [[0, "start", "0 · low"], [50, "middle", "50 · typical"], [100, "end", "100 · high"]].forEach(([p, a, t]) =>
      (svg += `<text class="tick" x="${x(p)}" y="${bottom + 16}" text-anchor="${a}">${t}</text>`));
    svg += `<text class="tick" x="${x(50)}" y="${bottom + 30}" text-anchor="middle">Percentile vs the same crag and season · bar = median</text></svg>`;
    host.innerHTML = svg + (hasDir ? `<p class="lead">Wind direction has no percentile; switch to "Actual vs typical" to see it.</p>` : "");
    bindDots(host, days);
  }

  function drawMulti(host, days, vars, selDay) {
    const figs = vars.map((v) => {
      if (v.key === "wind_dir") return windRose(days, v);
      const pts = days.map((d) => ({ d, a: conv(v.key, d.cl.values[v.key]), t: conv(v.key, d.cl.typical[v.key]) })).filter((o) => o.a != null && o.t != null);
      if (!pts.length) return `<figure><figcaption>${esc(v.label)}<small>No data in view</small></figcaption></figure>`;
      const W = 200, H = 160, pl = 34, pb = 22, pt = 6, pr = 6;
      let lo = Math.min(...pts.map((o) => Math.min(o.a, o.t))), hi = Math.max(...pts.map((o) => Math.max(o.a, o.t)));
      if (hi === lo) { hi += 1; lo -= 1; }
      const nonNeg = pts.every((o) => o.a >= 0 && o.t >= 0);
      const pad = (hi - lo) * 0.05; lo = nonNeg ? Math.max(0, lo - pad) : lo - pad; hi += pad;
      const sx = (vv) => pl + ((vv - lo) / (hi - lo)) * (W - pl - pr), sy = (vv) => H - pb - ((vv - lo) / (hi - lo)) * (H - pb - pt);
      const above = pts.filter((o) => o.a > o.t).length;
      const dec = Math.abs(hi - lo) < 10 ? 1 : 0;
      let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(v.label)}: actual versus typical">`;
      svg += `<line class="axis" x1="${pl}" x2="${W - pr}" y1="${H - pb}" y2="${H - pb}"/><line class="axis" x1="${pl}" x2="${pl}" y1="${pt}" y2="${H - pb}"/>`;
      svg += `<line class="diag" x1="${sx(lo)}" y1="${sy(lo)}" x2="${sx(hi)}" y2="${sy(hi)}"/>`;
      svg += `<text class="tick" x="${pl}" y="${H - 6}">${fmt(lo, dec)}</text><text class="tick" x="${W - pr}" y="${H - 6}" text-anchor="end">${fmt(hi, dec)}</text>`;
      svg += `<text class="tick" x="${pl - 4}" y="${sy(hi) + 8}" text-anchor="end">${fmt(hi, dec)}</text><text class="tick" x="${pl - 4}" y="${sy(lo)}" text-anchor="end">${fmt(lo, dec)}</text>`;
      svg += `<text class="tick" x="${(pl + W - pr) / 2}" y="${H - 6}" text-anchor="middle">typical</text>`;
      let selSvg = "";
      pts.forEach((o) => {
        const c = `<circle class="hit" cx="${sx(o.t)}" cy="${sy(o.a)}" r="6" data-day="${esc(o.d.key)}" data-var="${v.key}"/>`;
        if (o.d.key === selDay) selSvg = c + `<circle class="d on" cx="${sx(o.t)}" cy="${sy(o.a)}" r="5"/>`;
        else svg += c + `<circle class="d" cx="${sx(o.t)}" cy="${sy(o.a)}" r="3.5"/>`;
      });
      svg += selSvg + `</svg>`;
      return `<figure><figcaption>${esc(v.label)} <span style="font-weight:400;color:var(--muted)">(${esc(unitOf(v))})</span>
        <small>${Math.round((100 * above) / pts.length)}% of ${pts.length} days above typical</small></figcaption>${svg}</figure>`;
    });
    host.innerHTML = `<p class="lead">Each dot is a climbing day: the day's value (up) against that crag's typical value for the season (across). Dots above the diagonal line were higher than typical.</p><div class="multi">${figs.join("")}</div>`;
    bindDots(host, days);
  }

  function windRose(days, v) {
    const bins = new Array(8).fill(0), names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    days.forEach((d) => { const a = d.cl.values.wind_dir; if (a != null) bins[Math.round(a / 45) % 8]++; });
    const n = bins.reduce((a, b) => a + b, 0);
    if (!n) return `<figure><figcaption>${esc(v.label)}<small>No data in view</small></figcaption></figure>`;
    const R = 62, cx = 100, cy = 80, max = Math.max(...bins);
    let svg = `<svg class="chart" viewBox="0 0 200 160" role="img" aria-label="Wind direction on climbing days">`;
    [0.5, 1].forEach((f) => (svg += `<circle cx="${cx}" cy="${cy}" r="${R * f}" fill="none" class="gridl"/>`));
    bins.forEach((b, i) => {
      if (!b) return;
      const r = R * Math.sqrt(b / max), a0 = ((i * 45 - 20) - 90) * Math.PI / 180, a1 = ((i * 45 + 20) - 90) * Math.PI / 180;
      svg += `<path class="rose" d="M${cx},${cy} L${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 0 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)} Z"><title>${names[i]}: ${b} days</title></path>`;
    });
    ["N", "E", "S", "W"].forEach((t, i) => {
      const a = (i * 90 - 90) * Math.PI / 180;
      svg += `<text class="tick" x="${cx + (R + 9) * Math.cos(a)}" y="${cy + (R + 9) * Math.sin(a) + 4}" text-anchor="middle">${t}</text>`;
    });
    const top = names[bins.indexOf(max)];
    return `<figure><figcaption>${esc(v.label)}<small>Direction the wind came from · most often ${top} (${Math.round(100 * max / n)}% of days)</small></figcaption>${svg + "</svg>"}</figure>`;
  }

  function drawTable(days, vars) {
    const rows = vars.map((v) => {
      const p = days.map((d) => d.cl.pct[v.key]).filter((x) => x != null);
      const vd = verdict(v.key, p);
      return `<tr><td>${esc(v.label)}</td><td class="n">${p.length}</td><td class="n">${vd.med != null ? Math.round(vd.med) : "–"}</td><td class="n">${vd.p != null ? pText(vd.p).replace("p ", "") : "–"}</td><td>${esc(vd.short.split(" · ")[0])}</td></tr>`;
    }).join("");
    $("#wxtable").innerHTML = `<table class="sum"><thead><tr><th>Variable</th><th>Days</th><th>Median pct</th><th>p</th><th>Reading</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function bindDots(host, days) {
    const byKey = {}; days.forEach((d) => (byKey[d.key] = d));
    host.querySelectorAll(".hit").forEach((h) => {
      h.addEventListener("mouseenter", (e) => {
        const d = byKey[h.dataset.day], v = varMeta(h.dataset.var);
        h.nextElementSibling && h.nextElementSibling.classList.add("hov");
        const val = d.cl.values[v.key], typ = d.cl.typical[v.key], p = d.cl.pct[v.key];
        const names = d.sends.slice(0, 4).map((s) => `${esc(s.name)} (${grade(s.g)})`).join(", ") + (d.sends.length > 4 ? ` +${d.sends.length - 4} more` : "");
        showTip(e, `<b>${esc(d.crag.name)}</b> <span class="m">${fmtDate(d.s)}</span><br>${names}<br>` +
          `${esc(v.label)}: <b>${fmt(conv(v.key, val), 1)} ${esc(unitOf(v))}</b>` +
          (typ != null ? ` <span class="m">(typical ${fmt(conv(v.key, typ), 1)})</span>` : "") +
          (p != null ? `<br><span class="m">${ordinal(Math.round(p))} percentile for this crag and season</span>` : ""));
      });
      h.addEventListener("mousemove", moveTip);
      h.addEventListener("mouseleave", () => { hideTip(); h.nextElementSibling && h.nextElementSibling.classList.remove("hov"); });
      h.addEventListener("click", () => {
        const d = byKey[h.dataset.day];
        S.selected = [...d.sends].sort((a, b) => b.pts - a.pts)[0];
        renderMarkers(); renderPanels();
      });
    });
  }

  // ---------------------------------------------------------------- tooltip
  const tip = () => $("#tip");
  function showTip(e, html) { const t = tip(); t.innerHTML = html; t.hidden = false; moveTip(e); }
  function moveTip(e) {
    const t = tip(), pad = 14;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + t.offsetWidth > innerWidth - 8) x = e.clientX - t.offsetWidth - pad;
    if (y + t.offsetHeight > innerHeight - 8) y = e.clientY - t.offsetHeight - pad;
    t.style.left = x + "px"; t.style.top = y + "px";
  }
  function hideTip() { tip().hidden = true; }

  // ---------------------------------------------------------------- wiring
  function renderPanels() {
    const list = scoped();
    renderScope(list);
    if (S.tab === "top") renderTop(list);
    if (S.tab === "fa") renderFA();
    if (S.tab === "wx") renderWx(list);
  }
  function renderAll() { renderHeader(); renderMarkers(); renderPanels(); }

  function toggleFA() {
    S.faOnly = !S.faOnly;
    $("#faOnly").setAttribute("aria-pressed", S.faOnly);
    if (S.selected && S.faOnly && !S.selected.is_fa) S.selected = null;
    renderAll();
  }

  function wire() {
    document.querySelectorAll("[data-window]").forEach((b) => (b.onclick = () => {
      S.window = b.dataset.window;
      document.querySelectorAll("[data-window]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      if (S.selected && !inWindow(S.selected)) S.selected = null;
      renderAll();
    }));
    document.querySelectorAll("[data-unit]").forEach((b) => (b.onclick = () => {
      S.unit = b.dataset.unit;
      document.querySelectorAll("[data-unit]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      renderPanels();
    }));
    $("#faOnly").onclick = toggleFA;
    document.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => {
      S.tab = b.dataset.tab;
      document.querySelectorAll("[data-tab]").forEach((x) => x.setAttribute("aria-selected", x === b));
      ["top", "fa", "wx"].forEach((t) => ($("#tab-" + t).hidden = t !== S.tab));
      renderPanels();
    }));
    let rt;
    addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => S.tab === "wx" && renderPanels(), 200); });
  }

  function renderFoot() {
    $("#foot").innerHTML = `8a score: the sum of the 10 highest-scoring sends, using 8a.nu-style points. Map groups crags into regions, countries and the globe as you zoom out.
      Weather: <a href="https://www.climatologylab.org/gridmet.html">gridMET</a> (Abatzoglou 2013) and
      <a href="https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land">ERA5-Land</a> (Copernicus Climate Change Service), via Google Earth Engine.
      Elevation: Copernicus GLO-30 DEM. Basemap: <a href="https://openfreemap.org">OpenFreeMap</a>, © OpenMapTiles, © OpenStreetMap contributors.`;
  }

  fetch(DATA_URL, { cache: "no-cache" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => { prep(d); wire(); renderFoot(); renderHeader(); initMap(); renderPanels(); })
    .catch((e) => { $("#sub").textContent = "Couldn't load the climbing data (" + e.message + ")."; });
})();

/* What drives my sends. Loads ../data/drivers.json (written nightly by pipeline/drivers.py). */
(function () {
  "use strict";
  const S = { mode: "hard", unit: "F", region: null };
  let D, VAR = {};
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (c) => Math.round(c * 100) + "%";

  // Plain words for "send days ran lower / higher than typical"
  const WORDS = {
    tmax: ["Cooler afternoons", "Warmer afternoons"], tmin: ["Colder nights", "Milder nights"],
    dewpoint: ["Lower dewpoints", "Higher dewpoints"], dewpoint_spread: ["Damper air", "Drier air"],
    rmin: ["Drier afternoons", "More humid afternoons"], rmax: ["Drier nights", "More humid nights"],
    vpd: ["More drying power", "More drying power"], sph: ["Drier air", "More humid air"],
    srad: ["Cloudier days", "Sunnier days"], wind: ["Calmer days", "Windier days"],
    pr: ["No rain that day", "Rain that day"], rain3: ["Dry spells beforehand", "Recent rain"],
    days_since_rain: ["Sooner after rain", "Longer since rain"],
  };
  WORDS.vpd[0] = "Less drying power";
  const NO_TYPICAL = new Set(["pr", "rain3"]);           // phrases that don't take "than typical"
  const TEMP = new Set(["tmax", "tmin", "dewpoint"]), TEMP_DIFF = new Set(["dewpoint_spread"]);
  const DEC = { "%": 0, kPa: 2, "g/kg": 1, "W/m²": 0, "m/s": 1, mm: 1, days: 0 };

  function conv(key, v) {
    if (v == null) return v;
    if (S.unit === "F" && TEMP.has(key)) return v * 9 / 5 + 32;
    if (S.unit === "F" && TEMP_DIFF.has(key)) return v * 9 / 5;
    return v;
  }
  const unit = (key) => (TEMP.has(key) || TEMP_DIFF.has(key) ? (S.unit === "F" ? "°F" : "°C") : VAR[key].unit);
  function fmt(key, v) {
    const u = unit(key);
    const d = u.startsWith("°") ? (S.unit === "F" ? 0 : 1) : (DEC[u] ?? 1);
    return Number(conv(key, v)).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
  }
  const withUnit = (key, s) => `${s}${unit(key).startsWith("°") || unit(key) === "%" ? "" : " "}${unit(key)}`;
  function sweetText(r) {
    if (!r.sweet) return "";
    const [a, b] = r.sweet, [lo, hi] = r.sweet_open;
    if (lo && hi) return "no clear peak";
    if (lo) return withUnit(r.key, "≤ " + fmt(r.key, b));
    if (hi) return withUnit(r.key, "≥ " + fmt(r.key, a));
    return withUnit(r.key, fmt(r.key, a) + "–" + fmt(r.key, b));
  }
  function phrase(r, dir) {
    const w = (WORDS[r.key] || ["Lower", "Higher"])[dir === "lower" ? 0 : 1];
    return NO_TYPICAL.has(r.key) ? w : `${w} than typical`;
  }
  const arrow = (dir) => `<span class="dir ${dir}">${dir === "lower" ? "↓" : "↑"}</span>`;
  const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");

  // current-mode view of an overall result
  function view(r) {
    if (S.mode === "all" && r.all_sends) {
      const a = r.all_sends;
      return Object.assign({}, r, a, { direction: a.median_pct < 50 ? "lower" : "higher" });
    }
    return r;
  }

  // ------------------------------------------------------------------ header + cards
  function renderStats() {
    const enough = D.regions.filter((r) => r.enough).length;
    const items = [["Climbing days", D.n_days], ["Sends", D.n_sends], ["Hard-weighted sample", Math.round(D.n_eff)],
      ["Regions analysed", enough], ["Years", `${D.years[0]}–${D.years[1]}`]];
    $("#stats").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  }

  function card(r, label) {
    return `<div class="card"><div class="k">${esc(label)}</div>
      <div class="what">${arrow(r.direction)} ${esc(phrase(r, r.direction))}</div>
      <div class="big">${pct(r.c)}<small>picks the send day</small></div>
      <div class="why">${r.sweet ? `Best range: <b>${esc(sweetText(r))}</b>. ` : ""}${
        !NO_TYPICAL.has(r.key) && r.median_pct > 0 && r.median_pct < 100 ? `Send days sat at the ${ordinal(Math.round(r.median_pct))} percentile for their crag and season. ` : ""}90% range ${pct(r.lo)}–${pct(r.hi)}; chance is 50%.</div></div>`;
  }

  function renderCards() {
    const top = D.overall.map(view).filter((r) => r.sig).sort((a, b) => b.c - a.c).slice(0, 3);
    const m = D.model && D.model.cv;
    $("#cards").innerHTML = top.map((r) => card(r, VAR[r.key].label)).join("") +
      (m && S.mode === "hard" ? `<div class="card"><div class="k">Combined</div>
        <div class="what">${D.model.vars.map((k) => esc(VAR[k].label)).join(" + ")}</div>
        <div class="big">${pct(m.c)}<small>picks the send day</small></div>
        <div class="why">These ${D.model.vars.length} distinct conditions together, tested on held-out years (90% range ${pct(m.lo)}–${pct(m.hi)}).${
          bestRegional() ? ` Across all areas they add little over dry spells alone, but each region's own model does better (up to ${pct(bestRegional())}), because different places need different conditions. The regional models will drive the live scoreboard.` : ""}</div></div>` : "");
  }

  function bestRegional() {
    const cs = D.regions.filter((r) => r.enough && r.model && r.model.cv).map((r) => r.model.cv.c);
    return cs.length ? Math.max(...cs) : null;
  }

  // ------------------------------------------------------------------ ranking chart
  function renderRank() {
    const rows = D.overall.map(view).sort((a, b) => b.c - a.c);
    const other = (r) => (S.mode === "hard" ? r.all_sends : D.overall.find((o) => o.key === r.key));
    const W = Math.max(520, $("#rank").clientWidth - 28), L = 190, R = 70, rowH = 30, T = 8;
    const hiMax = Math.max(...rows.map((r) => r.hi), ...rows.map((r) => (other(r) || r).hi));
    const x0 = 0.45, x1 = Math.max(0.65, Math.ceil(hiMax * 20) / 20);
    const x = (v) => L + ((v - x0) / (x1 - x0)) * (W - L - R);
    const H = T + rows.length * rowH + 26;
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Conditions ranked by how often they pick the send day">`;
    for (let t = x0; t <= x1 + 1e-9; t += 0.05) {
      s += `<line class="${Math.abs(t - 0.5) < 1e-9 ? "chance" : "gridl"}" x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - 22}"/>`;
      s += `<text class="tick" x="${x(t)}" y="${H - 8}" text-anchor="middle">${Math.round(t * 100)}%${Math.abs(t - 0.5) < 1e-9 ? " chance" : ""}</text>`;
    }
    rows.forEach((r, i) => {
      const y = T + i * rowH + rowH / 2, col = r.sig ? `var(--${r.direction})` : "var(--avail)";
      const o = other(r);
      s += `<text class="lab" x="${L - 12}" y="${y + 4}" text-anchor="end">${esc(VAR[r.key].label)}</text>`;
      s += `<line class="ci" x1="${x(r.lo)}" x2="${x(r.hi)}" y1="${y}" y2="${y}" stroke="${col}"/>`;
      if (o) s += `<circle class="ghost" cx="${x(o.c)}" cy="${y}" r="4"/>`;
      s += `<circle cx="${x(r.c)}" cy="${y}" r="6" fill="${col}" stroke="var(--surface)" stroke-width="2"/>`;
      s += `<text class="tick" x="${W - R + 8}" y="${y + 4}">${r.sig ? (r.direction === "lower" ? "↓ lower" : "↑ higher") : "unclear"}</text>`;
      s += `<rect class="hit" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}" data-key="${r.key}"/>`;
    });
    $("#rank").innerHTML = s + "</svg>";
    $("#rank").querySelectorAll(".hit").forEach((h) => {
      const r = rows.find((q) => q.key === h.dataset.key), o = other(r);
      h.onmousemove = (e) => tip(e, `<b>${esc(VAR[r.key].label)}</b><br>${r.sig ? `${arrow(r.direction)} ${esc(phrase(r, r.direction))}` : "No clear difference"}<br>
        Picks the send day ${pct(r.c)} <span class="m">(90% range ${pct(r.lo)}–${pct(r.hi)})</span>` +
        (r.sweet ? `<br>Best range: ${esc(sweetText(D.overall.find((q) => q.key === r.key)))}` : "") +
        (o ? `<br><span class="m">${S.mode === "hard" ? "All sends equal" : "Hard sends weighted"}: ${pct(o.c)}</span>` : ""));
      h.onmouseleave = hideTip;
    });
    $("#rankLegend").innerHTML = `<span><i style="background:var(--lower)"></i>Send days lower than typical</span>
      <span><i style="background:var(--higher)"></i>Higher than typical</span><span><i style="background:var(--avail)"></i>No clear difference</span>
      <span><i style="border:1.5px solid var(--text-2)"></i>${S.mode === "hard" ? "All sends counted equally" : "Hard sends weighted"}, for comparison</span>`;
    $("#rankTable").innerHTML = `<table><thead><tr><th>Condition</th><th>Picks send day</th><th>90% range</th><th>Send days ran</th><th>Best range</th><th>All sends equal</th></tr></thead><tbody>${
      D.overall.map((r) => `<tr><td>${esc(VAR[r.key].label)}</td><td class="n">${pct(r.c)}</td><td class="n">${pct(r.lo)}–${pct(r.hi)}</td>
        <td>${r.sig ? r.direction : "unclear"}</td><td>${esc(sweetText(r))}</td><td class="n">${r.all_sends ? pct(r.all_sends.c) : "–"}</td></tr>`).join("")}</tbody></table>`;
  }

  // ------------------------------------------------------------------ sweet-spot curves
  function sweetFigure(r, title) {
    const key = r.key, log = VAR[key].log;
    const sx = (v) => (log ? Math.log1p(Math.max(0, v)) : conv(key, v));
    const xs = r.curve.map((p) => sx(p[0]));
    const lo = Math.min(...xs), hi = Math.max(...xs);
    const W = 260, L = 8, R = 8, top = 6, h1 = 82, gap = 14, h2 = 34, H = top + h1 + gap + h2 + 18;
    const X = (v) => L + ((v - lo) / (hi - lo || 1)) * (W - L - R);
    const Y = (v) => top + (1 - v) * h1;
    const col = `var(--${r.direction})`;
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(VAR[key].label)} sweet spot">`;
    const [a, b] = r.sweet;
    s += `<rect class="band" x="${X(sx(a))}" y="${top}" width="${Math.max(2, X(sx(b)) - X(sx(a)))}" height="${h1}"/>`;
    s += `<line class="axis" x1="${L}" x2="${W - R}" y1="${top + h1}" y2="${top + h1}"/>`;
    s += `<path class="curve" stroke="${col}" d="${r.curve.map((p, i) => (i ? "L" : "M") + X(xs[i]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("")}"/>`;
    // distributions: available days (grey) vs send days (colour), paired bars per bin
    const e = r.hist.edges.map(sx), y2 = top + h1 + gap, mx = Math.max(...r.hist.avail, ...r.hist.sends) || 1;
    r.hist.avail.forEach((v, i) => {
      const x0 = X(e[i]), x1 = X(e[i + 1]), w = Math.max(1, (x1 - x0) / 2 - 1);
      const ha = (v / mx) * h2, hs = (r.hist.sends[i] / mx) * h2;
      s += `<rect class="bar-a" x="${x0 + 0.5}" y="${y2 + h2 - ha}" width="${w}" height="${ha}"/>`;
      s += `<rect fill="${col}" x="${x0 + w + 1}" y="${y2 + h2 - hs}" width="${w}" height="${hs}"/>`;
    });
    s += `<line class="axis" x1="${L}" x2="${W - R}" y1="${y2 + h2}" y2="${y2 + h2}"/>`;
    const lab = (v) => (log ? fmt(key, Math.expm1(v)) : Number(v).toLocaleString(undefined, { maximumFractionDigits: unit(key) === "kPa" ? 1 : 0 }));
    [lo, (lo + hi) / 2, hi].forEach((v, i) => (s += `<text class="tick" x="${X(v)}" y="${H - 3}" text-anchor="${["start", "middle", "end"][i]}">${lab(v)}${i === 2 ? " " + esc(unit(key)) : ""}</text>`));
    s += "</svg>";
    return `<figure><figcaption>${esc(title || VAR[key].label)}<small>${arrow(r.direction)} ${esc(phrase(r, r.direction))} · best ${esc(sweetText(r))}</small></figcaption>${s}
      <div class="legend" style="margin:2px 0 0"><span><i class="sq" style="background:var(--avail)"></i>available days</span><span><i class="sq" style="background:${col}"></i>send days${S.mode === "hard" ? " (hard-weighted)" : ""}</span></div></figure>`;
  }

  function renderSweet() {
    const top = D.overall.filter((r) => r.sig && r.curve).sort((a, b) => b.c - a.c).slice(0, 6);
    $("#sweet").innerHTML = top.map((r) => sweetFigure(r)).join("");
  }

  // ------------------------------------------------------------------ region matrix
  function renderMatrix() {
    const regions = D.regions.filter((r) => r.enough);
    // the top overall conditions, plus any condition that clearly matters in some region
    const order = D.overall.slice().sort((a, b) => b.c - a.c).map((r) => r.key);
    const local = new Set(regions.flatMap((reg) => Object.entries(reg.vars).filter(([, r]) => r.sig).map(([k]) => k)));
    const cols = order.filter((k, i) => i < 6 || local.has(k));
    if (!S.region && regions.length) S.region = regions[0].id;
    const cell = (reg, k) => {
      const r = reg.vars[k];
      if (!r) return `<td><div class="cell ns">–</div></td>`;
      if (!r.sig) return `<td><div class="cell ns" data-r="${reg.id}" data-k="${k}" style="background:var(--neutral)">${pct(r.c)}</div></td>`;
      const strength = Math.min(1, Math.max(0.18, (r.c - 0.5) / 0.2));
      const mix = Math.round(18 + strength * 62);
      return `<td><div class="cell" data-r="${reg.id}" data-k="${k}" style="background:color-mix(in srgb, var(--${r.direction}) ${mix}%, var(--surface));${mix > 55 ? "color:#fff" : ""}">${r.direction === "lower" ? "↓" : "↑"} ${pct(r.c)}</div></td>`;
    };
    $("#matrix").innerHTML = `<table class="matrix"><thead><tr><th></th>${cols.map((k) => `<th class="var">${esc(VAR[k].label)}</th>`).join("")}</tr></thead><tbody>${
      regions.map((reg) => `<tr class="row${reg.id === S.region ? " sel" : ""}" data-region="${reg.id}"><td class="rname"><b>${esc(reg.name)}</b>
        <small>${esc(reg.states.join(", "))} · ${reg.days} days · ${reg.crags.length} crags</small></td>${cols.map((k) => cell(reg, k)).join("")}</tr>`).join("")}</tbody></table>`;
    $("#matrix").querySelectorAll("tr.row").forEach((tr) => (tr.onclick = () => { S.region = tr.dataset.region; renderMatrix(); }));
    $("#matrix").querySelectorAll(".cell[data-r]").forEach((c) => {
      const reg = D.regions.find((r) => r.id === c.dataset.r), r = reg.vars[c.dataset.k];
      c.onmousemove = (e) => tip(e, `<b>${esc(reg.name)}</b> · ${esc(VAR[r.key || c.dataset.k].label)}<br>` +
        (r.sig ? `${arrow(r.direction)} ${esc(phrase(Object.assign({ key: c.dataset.k }, r), r.direction))}` : "No clear difference here") +
        `<br>Picks the send day ${pct(r.c)} <span class="m">(90% range ${pct(r.lo)}–${pct(r.hi)})</span>` +
        (r.sweet ? `<br>Best range: ${esc(sweetText(Object.assign({ key: c.dataset.k }, r)))}` : ""));
      c.onmouseleave = hideTip;
    });
    $("#matrixLegend").innerHTML = `<span><i class="sq" style="background:var(--lower)"></i>↓ send days lower than typical</span>
      <span><i class="sq" style="background:var(--higher)"></i>↑ higher than typical</span><span><i class="sq" style="background:var(--neutral)"></i>no clear difference</span>
      <span>Darker = picks the send day more often</span>`;
    renderRegion();
    const small = D.regions.filter((r) => !r.enough);
    $("#smallRegions").innerHTML = small.length ? `Not enough climbing days yet for their own results (they still count overall): ${
      small.map((r) => `<b>${esc(r.name)}</b> (${r.days} day${r.days === 1 ? "" : "s"})`).join(", ")}.` : "";
  }

  function renderRegion() {
    const reg = D.regions.find((r) => r.id === S.region);
    if (!reg) { $("#regionDetail").innerHTML = ""; return; }
    const tmp = (c) => `${fmt("tmax", c)}${S.unit === "F" ? "°F" : "°C"}`;
    const ranked = Object.entries(reg.vars).map(([k, r]) => Object.assign({ key: k }, r)).filter((r) => r.sig).sort((a, b) => b.c - a.c);
    const m = reg.model && reg.model.cv;
    $("#regionDetail").innerHTML = `<h3>${esc(reg.name)} region</h3>
      <div class="facts"><b>${reg.days}</b> climbing days · <b>${reg.crags.length}</b> crags · ${esc(reg.states.join(", "))} ·
        ${Math.round(reg.elev_m[0])}–${Math.round(reg.elev_m[1])} m · typical highs ${tmp(reg.jan_tmax)} in January, ${tmp(reg.jul_tmax)} in July${
        m ? ` · combined model picks the send day <b>${pct(m.c)}</b>` : ""}</div>` +
      (ranked.length ? `<div class="cards">${ranked.slice(0, 3).map((r, i) => card(r, i === 0 ? "Matters most here" : "Also matters")).join("")}</div>
        <div class="multi">${ranked.slice(0, 3).filter((r) => r.curve).map((r) => sweetFigure(r)).join("")}</div>`
        : `<p class="note">No condition stands out clearly here yet; more climbing days will sharpen this.</p>`) +
      `<p class="members">Crags: ${esc(reg.crags.join(", "))}</p>`;
  }

  // ------------------------------------------------------------------ method + footer
  function renderMethod() {
    const m = D.method;
    $("#method").innerHTML = `
      <p><b>Used versus available.</b> I only log sends, not failed days, so each climbing day (a crag and date with a send) is compared with
        every other day at that crag within ±${m.window_days} days of the same date, ${m.baseline[0]}–${m.baseline[1]}. Those are the days I could
        have gone. A conditional logistic regression, with each climbing day matched to its own available days, asks which conditions set send days
        apart. Matching on place and season removes where and when I chose to climb, the same approach as presence-only species models.</p>
      <p><b>Hard sends count more.</b> Each climbing day is weighted by its hardest send relative to my level at the time
        (the ${Math.round(m.level_quantile * 100)}th percentile grade of the previous ${Math.round(m.level_window_days / 365)} years):
        a send at my level counts fully, one grade below counts half, two grades below a quarter, and so on. The hard-weighted sample of
        ${D.n_days} climbing days is worth about ${Math.round(D.n_eff)} full-weight days.</p>
      <p><b>Honest scores.</b> Each condition's score is leave-one-year-out: the model is fitted without a year, then asked to pick that year's
        send days. The percentage is how often the send day out-scores a comparable day; 50% is chance. Ranges are 90% intervals from
        ${m.n_boot.toLocaleString()} bootstrap resamples of whole years. Sweet-spot curves allow a peak (a quadratic term), because friction usually
        has a best temperature rather than "colder is always better".</p>
      <p><b>Regions.</b> Crags are clustered (Ward's method) on their ${m.baseline[1] - m.baseline[0] + 1}-year monthly temperature, dewpoint and rain.
        A region needs ${m.min_area_days}+ climbing days for its own results, and its curves are pulled toward the overall ones in proportion to
        how little data it has, so a thin region can't produce a confident-looking fluke.</p>
      <p><b>Weather.</b> gridMET (contiguous US, 4 km) or ERA5-Land (elsewhere, 9 km) via Google Earth Engine, corrected to crag elevation with
        lapse rates; humidity recomputed at the crag. Days with approximate dates (a whole trip logged on one day) are left out.</p>`;
    const g = new Date(D.generated);
    $("#foot").innerHTML = `Updated ${g.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}. Weather: gridMET (Abatzoglou 2013) and ERA5-Land
      (Copernicus Climate Change Service) via Google Earth Engine; elevation: Copernicus GLO-30 DEM. <a href="../">Back to the ticklist map</a>.`;
  }

  // ------------------------------------------------------------------ tooltip + wiring
  function tip(e, html) {
    const t = $("#tip"); t.innerHTML = html; t.hidden = false;
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + t.offsetWidth > innerWidth - 8) x = e.clientX - t.offsetWidth - 14;
    if (y + t.offsetHeight > innerHeight - 8) y = e.clientY - t.offsetHeight - 14;
    t.style.left = x + "px"; t.style.top = y + "px";
  }
  function hideTip() { $("#tip").hidden = true; }

  function renderAll() { renderStats(); renderCards(); renderRank(); renderSweet(); renderMatrix(); renderMethod(); }

  document.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => {
    S.mode = b.dataset.mode;
    document.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    renderCards(); renderRank();
  }));
  document.querySelectorAll("[data-unit]").forEach((b) => (b.onclick = () => {
    S.unit = b.dataset.unit;
    document.querySelectorAll("[data-unit]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    renderAll();
  }));
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(renderRank, 150); });

  fetch("../data/drivers.json", { cache: "no-cache" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => { D = d; d.variables.forEach((v) => (VAR[v.key] = v)); renderAll(); })
    .catch((e) => { $("main").innerHTML = `<p class="lead">Couldn't load the analysis (${esc(e.message)}). It's created by the nightly run.</p>`; });
})();

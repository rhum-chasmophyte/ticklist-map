/* Send conditions now. Loads ../data/conditions.json (refreshed every 6 hours). */
(function () {
  "use strict";
  const S = { rank: "today", region: "", unit: "F", open: null, day: {} };
  let D;
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const day = (s) => new Date(s + "T12:00:00");
  const dayLabel = (s, i) => (i === 0 ? "Today" : i === 1 ? "Tmrw" : DOW[day(s).getDay()]);
  const longDay = (s, i) => (i === 0 ? "Today" : i === 1 ? "Tomorrow" : `${DOW[day(s).getDay()]} ${MON[day(s).getMonth()]} ${day(s).getDate()}`);

  // ---------------------------------------------------------------- units + score colours
  const T = (c) => (S.unit === "F" ? c * 9 / 5 + 32 : c);
  const TD = (c) => (S.unit === "F" ? c * 9 / 5 : c);
  const deg = () => (S.unit === "F" ? "°F" : "°C");
  const r0 = (v) => (v == null ? "–" : Math.round(v));
  const r1 = (v) => (v == null ? "–" : Math.round(v * 10) / 10);
  function chip(score, big = false, extra = "") {
    const i = score >= 95 ? 7 : score >= 85 ? 6 : score >= 70 ? 5 : score >= 55 ? 4 : score >= 40 ? 3 : score >= 25 ? 2 : 1;
    const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    const fg = dark ? (i >= 6 ? "#0b0b0b" : "#f0efec") : (i >= 4 ? "#fff" : "#0b0b0b");
    return `<span class="chip${big ? " big" : ""}${extra}" style="background:var(--s${i});color:${fg}">${score >= 95 ? "⚡" : ""}${Math.round(score)}</span>`;
  }

  // ---------------------------------------------------------------- reasons in plain words
  function phrase(k, v, typ) {
    const hi = v > typ;
    switch (k) {
      case "rain3": return v < 0.5 ? "Dry for 3 days before" : `${r1(v)} mm rain in the 3 days before`;
      case "pr": return v < 0.5 ? "No rain" : `Rain: ${r1(v)} mm`;
      case "days_since_rain": return `${r0(v)}${v >= 60 ? "+" : ""} day${v === 1 ? "" : "s"} since rain`;
      case "vpd": return `${hi ? "More" : "Less"} drying power than usual`;
      case "srad": return hi ? "Sunnier than usual" : "Cloudier than usual";
      case "wind": return `${hi ? "Windier" : "Calmer"} than usual (${r1(v)} m/s)`;
      case "tmax": return `${hi ? "Warmer" : "Cooler"} than usual, high ${r0(T(v))}${deg()}`;
      case "tmin": return `${hi ? "Milder" : "Colder"} night than usual, low ${r0(T(v))}${deg()}`;
      case "dewpoint": return `${hi ? "More humid" : "Drier"} air than usual (dewpoint ${r0(T(v))}${deg()})`;
      case "dewpoint_spread": return `${hi ? "Drier" : "Damper"} air than usual`;
      case "rmin": return `${hi ? "More humid" : "Drier"} afternoon than usual (${r0(v)}% RH)`;
      case "rmax": return `${hi ? "More humid" : "Drier"} night than usual (${r0(v)}% RH)`;
      case "sph": return `${hi ? "More humid" : "Drier"} air than usual`;
      default: return k;
    }
  }
  function reasons(d) {
    const items = Object.entries(d.contrib).map(([k, c]) => ({ k, c, v: d.vals[k] ?? d.typical[k], typ: d.typical[k] }))
      .filter((x) => x.v != null && Math.abs(x.c) >= 0.02).sort((a, b) => Math.abs(b.c) - Math.abs(a.c));
    const up = items.filter((x) => x.c > 0).slice(0, 3), down = items.filter((x) => x.c < 0).slice(0, 2);
    return { up, down };
  }
  const reasonText = (r) => r.up.map((x) => phrase(x.k, x.v, x.typ));

  // ---------------------------------------------------------------- data views
  const visible = () => D.crags.filter((c) => !S.region || c.region_id === S.region);
  const rankScore = (c) => (S.rank === "today" ? c.days[0].score : Math.max(...c.days.slice(0, 3).map((d) => d.score)));

  function renderStats() {
    const ins = D.crags.filter((c) => c.in_season);
    const prime = ins.reduce((n, c) => n + c.days.filter((d) => d.prime).length, 0);
    const best = ins.slice().sort((a, b) => b.days[0].score - a.days[0].score)[0];
    const g = new Date(D.generated);
    const items = [["Crags in season", ins.length], ["Prime days ahead", prime], ["Best today", best ? esc(best.name) : "–"],
      ["Updated", `${g.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${g.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`]];
    $("#stats").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  }

  function renderPrime() {
    const list = [];
    visible().filter((c) => c.in_season).forEach((c) => {
      c.days.forEach((d, i) => { if (d.prime) list.push({ c, d, i }); });
    });
    // one card per crag (its best prime day), best first
    const best = {};
    list.forEach((x) => { if (!best[x.c.id] || x.d.score > best[x.c.id].d.score) best[x.c.id] = x; });
    const cards = Object.values(best).sort((a, b) => b.d.score - a.d.score || a.i - b.i).slice(0, 8);
    $("#prime").innerHTML = cards.length ? cards.map(({ c, d, i }) => `<div class="card" data-open="${c.id}" data-day="${i}">
        <div class="when">${esc(longDay(d.date, i))}${c.days.filter((x) => x.prime).length > 1 ? ` · ${c.days.filter((x) => x.prime).length} prime days this week` : ""}</div>
        <div class="row">${chip(d.score)}<div><div class="name">${esc(c.name)}</div><div class="why">${esc(reasonText(reasons(d)).slice(0, 2).join(" · "))}</div></div></div></div>`).join("")
      : `<p class="empty">No prime days in the next week${S.region ? " in this region" : ""}. The scoreboard below shows the best of what's coming.</p>`;
    $("#prime").querySelectorAll("[data-open]").forEach((el) => (el.onclick = () => {
      S.open = el.dataset.open; S.day[S.open] = +el.dataset.day; renderBoard();
      document.getElementById("row-" + S.open).scrollIntoView({ behavior: "smooth", block: "center" });
    }));
  }

  function head(c) {
    return `<div class="head" role="row"><span>#</span><span>Crag</span><span>${S.rank === "today" ? "Today" : "Best 3 days"}</span>
      <span class="days">${c.days.map((d, i) => `<span>${dayLabel(d.date, i)}</span>`).join("")}</span></div>`;
  }

  function row(c, i) {
    const open = S.open === c.id, di = S.day[c.id] ?? 0;
    const sub = [c.region ? `${c.region} region` : `${c.state}`, c.model === "region" ? "" : "overall model"].filter(Boolean).join(" · ");
    return `<div class="rowx${open ? " open" : ""}" role="row" id="row-${c.id}" data-id="${c.id}">
      <span class="rank">${i + 1}</span>
      <span class="crag"><b>${esc(c.name)}</b><small>${esc(c.state)} · ${esc(sub)}</small></span>
      <span>${chip(rankScore(c), true)}</span>
      <span class="strip">${c.days.map((d, j) => `<span title="${esc(longDay(d.date, j))}: ${Math.round(d.score)}" data-day="${j}">${chip(d.score, false, open && j === di ? " sel" : "")}</span>`).join("")}</span>
      ${open ? detail(c, di) : ""}</div>`;
  }

  function detail(c, i) {
    const d = c.days[i], v = d.vals, r = reasons(d);
    const off = c.offsets || {};
    const cal = Object.keys(off).length ? `Forecast calibrated against this crag's gridMET/ERA5 record: ${
      off.tmax_c != null ? `high temperature ${off.tmax_c > 0 ? "+" : ""}${r1(TD(off.tmax_c))}${deg()}` : ""}${
      off.td_c != null ? `, dewpoint ${off.td_c > 0 ? "+" : ""}${r1(TD(off.td_c))}${deg()}` : ""}${off.wind_ms != null ? `, wind ${off.wind_ms > 0 ? "+" : ""}${r1(off.wind_ms)} m/s` : ""}.` : "Not enough overlapping days to calibrate this forecast yet.";
    return `<div class="detail" data-stop>
      <div class="daypick"><b>${esc(longDay(d.date, i))}</b> · score ${Math.round(d.score)}: better than ${Math.round(d.score)}% of typical days here at this time of year${d.prime ? " · <b>⚡ prime</b>" : ""}</div>
      <div class="reasons">${r.up.map((x) => `<span class="reason up">${esc(phrase(x.k, x.v, x.typ))}</span>`).join("")}${
        r.down.map((x) => `<span class="reason down">${esc(phrase(x.k, x.v, x.typ))}</span>`).join("")}</div>
      <div class="grid">
        <div class="v">High / low<br><b>${r0(T(v.tmax))}° / ${r0(T(v.tmin))}${deg()}</b></div>
        <div class="v">Afternoon humidity<br><b>${r0(v.rmin)}%</b></div>
        <div class="v">Rain that day<br><b>${r1(v.pr)} mm</b></div>
        <div class="v">Rain, 3 days before<br><b>${r1(v.rain3)} mm</b></div>
        <div class="v">Days since rain<br><b>${r0(v.days_since_rain)}${v.days_since_rain >= 60 ? "+" : ""}</b></div>
        <div class="v">Wind<br><b>${r1(v.wind)} m/s</b></div>
      </div>
      <div class="note">Scored with the ${c.model === "region" ? `${esc(c.region)} region's model` : "overall model (this region doesn't have enough days for its own yet)"},
        which weighs ${c.model_vars.map((k) => esc(VARS[k] || k)).join(", ")}. ${cal}
        ${c.in_season ? `In season: ${c.season_visits} past climbing day${c.season_visits === 1 ? "" : "s"} ${c.season_basis === "region" ? "in this region" : "here"} within ${Math.round(D.season_days / 7)} weeks of this date.` : `Out of season: no climbing days ${c.season_basis === "region" ? "in this region" : "here"} within ${Math.round(D.season_days / 7)} weeks of this date.`}</div>
    </div>`;
  }
  let VARS = {};

  function renderBoard() {
    const all = visible().slice().sort((a, b) => rankScore(b) - rankScore(a));
    const ins = all.filter((c) => c.in_season), outs = all.filter((c) => !c.in_season);
    $("#boardLead").textContent = `${ins.length} crag${ins.length === 1 ? "" : "s"} in season${S.region ? " in this region" : ""}, ranked by ${S.rank === "today" ? "today's score" : "their best score over the next 3 days"}. Pick a crag for its forecast and the reasons behind its score; pick a day to switch.`;
    $("#board").innerHTML = ins.length ? head(ins[0]) + ins.map(row).join("") : `<p class="empty" style="padding:12px 14px">No crags in season here right now.</p>`;
    $("#offWrap").hidden = !outs.length;
    $("#offSum").textContent = `Out of season (${outs.length}): no climbing days within ${Math.round(D.season_days / 7)} weeks of this date`;
    $("#off").innerHTML = outs.length ? head(outs[0]) + outs.map(row).join("") : "";
    document.querySelectorAll(".rowx").forEach((el) => (el.onclick = (e) => {
      if (e.target.closest("[data-stop]")) return;
      const dayEl = e.target.closest("[data-day]");
      const id = el.dataset.id;
      if (dayEl) { S.open = id; S.day[id] = +dayEl.dataset.day; }
      else S.open = S.open === id ? null : id;
      renderBoard();
    }));
    $("#scale").innerHTML = `<span>Score</span><span class="bar">${[1, 2, 3, 4, 5, 6, 7].map((i) => `<span style="background:var(--s${i})"></span>`).join("")}</span>
      <span>0 → 100 · 50 = a typical day here for the season · ⚡95+ prime</span>`;
  }

  function renderMethod() {
    const cvs = D.regions.filter((r) => r.enough && r.cv).map((r) => `${esc(r.name)} ${Math.round(r.cv.c * 100)}%`);
    $("#method").innerHTML = `
      <p><b>Forecast.</b> Every 6 hours, hourly forecasts for the next week (and the past two weeks) come from Open-Meteo for each crag,
        at the elevation of the climate grid cell, and are rolled up into daily highs, lows, humidity, rain, wind and sun.</p>
      <p><b>Calibration.</b> On the recent days covered by both the forecast and my gridMET/ERA5 record, each crag's average difference
        is removed, so forecast and history agree. The same elevation correction and derived measures (dewpoint spread, days since rain,
        rain in the 3 days before) are then applied as in the analysis.</p>
      <p><b>Score.</b> Each day is scored with its region's model from <a href="../drivers/">What drives my sends</a>: the conditions that
        have best separated my hard sends from other days there. The score is a percentile against the same crag's days within
        ±15 days of the date over 25 years, so 50 is typical and 95 means better than 95% of them. Tested on held-out years, the regional
        models pick the send day ${cvs.join(", ")} of the time (50% is chance); the all-areas model ${D.overall_cv ? Math.round(D.overall_cv.c * 100) + "%" : ""}.</p>
      <p><b>In season.</b> A crag is in season if I've climbed there within ${Math.round(D.season_days / 7)} weeks of this date in any year
        (crags with only a couple of days borrow their region's history). Everything else is listed separately, so a desert crag in July
        can't top the board just by being unusually mild for July.</p>
      <p><b>Reasons</b> compare each condition with a typical day for that crag and season, and show which ones help (✓) or hurt (✗) the score most.</p>`;
    $("#foot").innerHTML = `Weather data by <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0); history from gridMET and ERA5-Land via Google Earth Engine.
      <a href="../">Ticklist map</a> · <a href="../drivers/">What drives my sends</a>`;
  }

  function renderAll() { renderStats(); renderPrime(); renderBoard(); renderMethod(); }

  document.querySelectorAll("[data-rank]").forEach((b) => (b.onclick = () => {
    S.rank = b.dataset.rank;
    document.querySelectorAll("[data-rank]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    renderBoard();
  }));
  document.querySelectorAll("[data-unit]").forEach((b) => (b.onclick = () => {
    S.unit = b.dataset.unit;
    document.querySelectorAll("[data-unit]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    renderAll();
  }));
  $("#region").onchange = (e) => { S.region = e.target.value; renderAll(); };

  fetch("../data/conditions.json", { cache: "no-cache" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => {
      D = d;
      (d.variables || []).forEach((v) => (VARS[v.key] = v.label.toLowerCase()));
      const regs = [...new Set(d.crags.map((c) => c.region_id).filter(Boolean))]
        .map((id) => d.regions.find((r) => r.id === id)).filter(Boolean);
      $("#region").innerHTML = `<option value="">All regions</option>` + regs.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
      renderAll();
    })
    .catch((e) => { $("main").innerHTML = `<p class="lead">Couldn't load the conditions (${esc(e.message)}). They're created by the conditions refresh on GitHub.</p>`; });
})();

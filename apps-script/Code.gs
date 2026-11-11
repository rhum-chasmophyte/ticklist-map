// ============ Ticklist – entry web app ============
// Paste over everything in Code.gs, save, then Deploy > Manage deployments > Edit (pencil)
// > Version: New version > Deploy. The web app URL stays the same.

const SHEET_ID = '1qXgdS0btxFkB3B4opZ1cbbQjtLiDkC7Qlyz8wEOX26c';
const MASTER_TAB = 'Ticklist';            // everything already logged (dropdown options)
const ENTRY_TAB = 'Entries';              // where new climbs land
const BOULDER_TAB = 'Boulder Locations';  // Nation, State, Crag, Zone, Boulder, x, y
const CRAG_TAB = 'Crag Locations';        // Crag, State, Country, Lat, Lon (for centring the map)

// Column order used only when a tab has no header row yet. Otherwise values are written
// under the matching header, wherever that column is.
const ENTRY_HEADERS = ['Date', 'Grade', 'Stars', 'Name', 'Boulder', 'Zone', 'Crag', 'State',
                       'Nation', 'NumberSessions', 'Notes', 'Beta', 'Style'];
const BOULDER_HEADERS = ['Nation', 'State', 'Crag', 'Zone', 'Boulder', 'x', 'y'];
const ALIASES = {
  Grade: ['Grade', 'Grade (v-scale)'], NumberSessions: ['NumberSessions', 'Sessions'],
  Nation: ['Nation', 'Country'], x: ['x', 'Lon', 'Longitude'], y: ['y', 'Lat', 'Latitude'],
};

function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('Ticklist')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

// ---------- helpers ----------
function txt_(v) { return String(v === null || v === undefined ? '' : v).trim(); }
function key_(v) { return txt_(v).toLowerCase(); }

function readTab_(ss, name) {
  const sh = ss.getSheetByName(name);
  if (!sh || sh.getLastRow() < 1) return { sh: sh, head: [], rows: [] };
  const values = sh.getDataRange().getValues();
  return { sh: sh, head: values[0].map(txt_), rows: values.slice(1) };
}

// Column index (0-based) for a field, trying its aliases; -1 if the tab doesn't have it
function col_(head, field) {
  const lower = head.map(function (h) { return h.toLowerCase(); });
  const names = ALIASES[field] || [field];
  for (let i = 0; i < names.length; i++) {
    const j = lower.indexOf(names[i].toLowerCase());
    if (j >= 0) return j;
  }
  return -1;
}

// Build a row for a tab from {field: value}, placing each value under its header
function rowFor_(head, fallbackHeaders, values) {
  const h = head.length ? head : fallbackHeaders;
  const row = new Array(h.length).fill('');
  Object.keys(values).forEach(function (field) {
    const i = col_(h, field);
    if (i >= 0) row[i] = values[field];
  });
  return row;
}

function num_(v) {
  const n = typeof v === 'number' ? v : parseFloat(String(v).trim());
  return isFinite(n) ? n : null;
}

// ---------- read: every place you've logged or georeferenced ----------
// Returns { places: [[nation, state, crag, zone, boulder, lat, lon], ...], crags: {"state|crag": [lat, lon]} }
function getFormData() {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const byKey = {};
  const places = [];
  function add(nation, state, crag, zone, boulder, lat, lon) {
    state = txt_(state); crag = txt_(crag); zone = txt_(zone); boulder = txt_(boulder);
    nation = txt_(nation).toUpperCase();
    if (!state || !crag) return;
    const k = [state, crag, zone, boulder].map(key_).join('|');
    let p = byKey[k];
    if (!p) { p = byKey[k] = [nation, state, crag, zone, boulder, null, null]; places.push(p); }
    if (!p[0] && nation) p[0] = nation;
    if (p[5] === null && lat !== null && lon !== null) { p[5] = lat; p[6] = lon; }
  }

  const b = readTab_(ss, BOULDER_TAB);
  const bi = ['Nation', 'State', 'Crag', 'Zone', 'Boulder', 'y', 'x'].map(function (f) { return col_(b.head, f); });
  b.rows.forEach(function (r) {
    const get = function (i) { return i >= 0 ? r[i] : ''; };
    const lat = num_(get(bi[5])), lon = num_(get(bi[6]));
    const ok = lat !== null && lon !== null && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
    add(get(bi[0]), get(bi[1]), get(bi[2]), get(bi[3]), get(bi[4]), ok ? lat : null, ok ? lon : null);
  });

  const t = readTab_(ss, MASTER_TAB);
  const ti = ['Nation', 'State', 'Crag', 'Zone', 'Boulder'].map(function (f) { return col_(t.head, f); });
  t.rows.forEach(function (r) {
    const get = function (i) { return i >= 0 ? r[i] : ''; };
    add(get(ti[0]), get(ti[1]), get(ti[2]), get(ti[3]), get(ti[4]), null, null);
  });

  // Nation for rows that didn't say: borrow it from another row of the same crag
  const nationOf = {};
  places.forEach(function (p) { if (p[0]) nationOf[key_(p[1]) + '|' + key_(p[2])] = p[0]; });
  places.forEach(function (p) { if (!p[0]) p[0] = nationOf[key_(p[1]) + '|' + key_(p[2])] || ''; });

  const crags = {};
  const c = readTab_(ss, CRAG_TAB);
  const ci = ['Crag', 'State', 'y', 'x'].map(function (f) { return col_(c.head, f); });
  c.rows.forEach(function (r) {
    const lat = num_(r[ci[2]]), lon = num_(r[ci[3]]);
    if (ci[0] >= 0 && lat !== null && lon !== null) crags[key_(r[ci[1]]) + '|' + key_(r[ci[0]])] = [lat, lon];
  });
  return { places: places, crags: crags };
}

// ---------- write: one climb, plus the boulder if it's new or newly placed ----------
function submitEntry(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const tz = ss.getSpreadsheetTimeZone();

    let sheet = ss.getSheetByName(ENTRY_TAB);
    if (!sheet) { sheet = ss.insertSheet(ENTRY_TAB); sheet.appendRow(ENTRY_HEADERS); }
    const head = readTab_(ss, ENTRY_TAB).head;
    // midnight in the Sheet's own timezone, so it never shows up as 23:00 the day before
    const date = Utilities.parseDate(e.date, tz, 'yyyy-MM-dd');
    sheet.appendRow(rowFor_(head, ENTRY_HEADERS, {
      Date: date,
      Grade: e.grade === '' ? '' : Number(e.grade),
      Stars: e.stars === '' ? '' : Number(e.stars),
      Name: e.name, Boulder: e.boulder, Zone: e.zone, Crag: e.crag, State: e.state,
      Nation: e.nation,
      NumberSessions: e.sessions === '' ? '' : Number(e.sessions),
      Notes: e.notes, Beta: e.beta, Style: (e.style || []).join(', '),
    }));
    const dateCol = col_(head.length ? head : ENTRY_HEADERS, 'Date');
    if (dateCol >= 0) sheet.getRange(sheet.getLastRow(), dateCol + 1).setNumberFormat('yyyy-mm-dd');

    return { ok: true, boulder: upsertBoulder_(ss, e) };
  } finally {
    lock.releaseLock();
  }
}

// Adds the boulder to Boulder Locations if it isn't there (with coordinates if given),
// or fills in coordinates for a listed boulder that has none. Returns 'added', 'placed' or ''.
function upsertBoulder_(ss, e) {
  if (!txt_(e.boulder)) return '';
  const lat = num_(e.lat), lon = num_(e.lon);
  const hasXY = lat !== null && lon !== null;
  let sheet = ss.getSheetByName(BOULDER_TAB);
  if (!sheet) { sheet = ss.insertSheet(BOULDER_TAB); sheet.appendRow(BOULDER_HEADERS); }
  const t = readTab_(ss, BOULDER_TAB);
  const head = t.head.length ? t.head : BOULDER_HEADERS;
  const ix = ['State', 'Crag', 'Zone', 'Boulder', 'x', 'y'].map(function (f) { return col_(head, f); });
  const want = [e.state, e.crag, e.zone, e.boulder].map(key_).join('|');
  const wantNoZone = [e.state, e.crag, e.boulder].map(key_).join('|');
  for (let r = 0; r < t.rows.length; r++) {
    const row = t.rows[r];
    const cell = function (i) { return key_(i >= 0 ? row[i] : ''); };
    const k = [ix[0], ix[1], ix[2], ix[3]].map(cell).join('|');
    // no zone given: the same boulder name at the same crag counts as a match
    const same = k === want || (!txt_(e.zone) && [ix[0], ix[1], ix[3]].map(cell).join('|') === wantNoZone);
    if (!same) continue;
    const empty = num_(row[ix[4]]) === null || num_(row[ix[5]]) === null;
    if (hasXY && empty && ix[4] >= 0 && ix[5] >= 0) {
      sheet.getRange(r + 2, ix[4] + 1).setValue(round7_(lon));
      sheet.getRange(r + 2, ix[5] + 1).setValue(round7_(lat));
      return 'placed';
    }
    return '';
  }
  sheet.appendRow(rowFor_(t.head, BOULDER_HEADERS, {
    Nation: txt_(e.nation).toUpperCase(), State: e.state, Crag: e.crag, Zone: e.zone, Boulder: e.boulder,
    x: hasXY ? round7_(lon) : '', y: hasXY ? round7_(lat) : '',
  }));
  return 'added';
}

function round7_(v) { return Math.round(v * 1e7) / 1e7; }

// ---------- one-time fix (run by hand from the editor) ----------
// The old form saved dates as midnight in the script's timezone, which the Sheet shows as
// 23:00 the day before. This snaps every date in Entries to midnight of the intended day.
// Select fixEntryDates in the toolbar's function list and click Run. Safe to run twice.
function fixEntryDates() {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const sheet = ss.getSheetByName(ENTRY_TAB);
  const tz = ss.getSpreadsheetTimeZone();
  const head = readTab_(ss, ENTRY_TAB).head;
  const c = col_(head, 'Date');
  const n = sheet.getLastRow() - 1;
  if (c < 0 || n < 1) return;
  const range = sheet.getRange(2, c + 1, n, 1);
  let changed = 0;
  const vals = range.getValues().map(function (r) {
    const d = r[0];
    if (!(d instanceof Date)) return [d];
    const hour = Number(Utilities.formatDate(d, tz, 'H'));
    const day = new Date(d.getTime() + (hour >= 12 ? 24 * 3600 * 1000 : 0));
    const fixed = Utilities.parseDate(Utilities.formatDate(day, tz, 'yyyy-MM-dd'), tz, 'yyyy-MM-dd');
    if (fixed.getTime() !== d.getTime()) changed++;
    return [fixed];
  });
  range.setValues(vals);
  range.setNumberFormat('yyyy-mm-dd');
  Logger.log('Dates adjusted: ' + changed + ' of ' + n);
}

"""Read the Ticklist, Crag Locations and Boulder Locations tabs (Sheets API or CSV exports)."""
import json
import os
import re

import numpy as np
import pandas as pd

from . import config as C

SERIAL_ORIGIN = pd.Timestamp("1899-12-30")   # Sheets date serial 0


def _client():
    import gspread
    key = os.environ.get("GCP_SA_KEY")
    if key:
        return gspread.service_account_from_dict(json.loads(key))
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    return gspread.service_account(filename=path) if path else gspread.oauth()


def _tab(sheet, name) -> pd.DataFrame:
    # Unformatted values: dates come back as serial numbers, not display text.
    rows = sheet.worksheet(name).get_values(
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="SERIAL_NUMBER",
    )
    if not rows:
        return pd.DataFrame()
    header = [str(h).strip() for h in rows[0]]
    body = [r + [""] * (len(header) - len(r)) for r in rows[1:]]
    return pd.DataFrame(body, columns=header)


def _optional_tab(sh, names):
    import gspread
    for name in names:
        try:
            return _tab(sh, name)
        except gspread.exceptions.WorksheetNotFound:
            continue
    return None


def load_from_sheet(sheet_id: str):
    """Returns (ticklist, crags, boulders). Crags and boulders are None if the tab is missing."""
    sh = _client().open_by_key(sheet_id)
    return (_tab(sh, C.TICKLIST_TAB), _optional_tab(sh, [C.CRAGS_TAB]),
            _optional_tab(sh, C.BOULDERS_TABS))


def load_from_csv(ticklist_csv, crags_csv=None, boulders_csv=None):
    read = lambda f: pd.read_csv(f, dtype=str, keep_default_na=False) if f else None
    return read(ticklist_csv), read(crags_csv), read(boulders_csv)


# --- Value helpers -------------------------------------------------------------
def parse_date(v):
    """Sheets serial or text -> date. Serials are rounded to the nearest day: a date written
    at midnight in another timezone shows up as 23:00 the day before."""
    if v is None or str(v).strip() == "":
        return pd.NaT
    try:
        return SERIAL_ORIGIN + pd.to_timedelta(round(float(v)), unit="D")
    except (TypeError, ValueError):
        return pd.to_datetime(v, errors="coerce")


def _num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.strip().replace("", None), errors="coerce")


def _txt(v):
    """Cell -> clean text. Whole numbers lose the '.0' a CSV or the API may add (crag '221')."""
    if v is None:
        return ""
    if isinstance(v, float):
        if np.isnan(v):
            return ""
        if v.is_integer():
            return str(int(v))
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def _col(df, *names):
    """First matching column (case-insensitive) as clean text, or blanks if none exists."""
    lower = {str(c).strip().casefold(): c for c in df.columns}
    for n in names:
        if n.casefold() in lower:
            return df[lower[n.casefold()]].map(_txt)
    return pd.Series([""] * len(df), index=df.index)


def _key(s):
    return pd.Series(s).astype(str).str.strip().str.casefold()


def parse_grade(v):
    m = re.search(r"-?\d+", str(v))
    return int(m.group()) if m else None


def points_for(grade, style):
    """8a.nu-style points, matching the sheet's formula: 450 + 50 per V-grade,
    Flash +53, First Ascent +33, Second Go +2, Repetition -1 (bonuses add up)."""
    if grade is None:
        return np.nan
    s = str(style).casefold()
    p = 450 + 50 * grade
    if "flash" in s:
        p += 53
    if "first ascent" in s or re.search(r"\bfa\b", s):
        p += 33
    if "second go" in s:
        p += 2
    if "repetition" in s or "repeat" in s:
        p -= 1
    return float(p)


# --- Tabs ----------------------------------------------------------------------
def clean_ticklist(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only the public columns. Notes and Beta never leave this function.
    Rows without a grade (day logs) are dropped."""
    out = pd.DataFrame({
        "date": df["Date"].map(parse_date).dt.normalize(),
        "grade": _col(df, "Grade", "Grade (v-scale)").map(parse_grade),
        "stars": _num(_col(df, "Stars")).values,
        "name": _col(df, "Name"),
        "boulder": _col(df, "Boulder"),
        "zone": _col(df, "Zone"),
        "crag": _col(df, "Crag"),
        "state": _col(df, "State"),
        "nation": _col(df, "Nation", "Country").str.upper(),
        "sessions": _num(_col(df, "NumberSessions", "Sessions")).values,
        "style": _col(df, "Style"),
    })
    sheet_pts = _num(_col(df, "8a points", "8a Points", "Points")).values
    calc = [points_for(g, s) for g, s in zip(out["grade"], out["style"])]
    out["points"] = np.where(np.isfinite(sheet_pts), sheet_pts, calc)

    out = out[out["name"] != ""]
    day_logs = out["grade"].isna()
    if day_logs.any():
        print(f"Skipping {int(day_logs.sum())} rows without a grade (day logs)")
    out = out[~day_logs].copy()
    out["grade"] = out["grade"].astype(int)

    bad = out["stars"].notna() & ((out["stars"] < 0) | (out["stars"] > 5))
    if bad.any():
        print(f"WARNING: star ratings outside 0-5 treated as unrated: "
              f"{list(zip(out.loc[bad, 'name'], out.loc[bad, 'stars']))}")
        out.loc[bad, "stars"] = np.nan
    # FA flag from the Style column ("FA", "fa", "First Ascent", "FA / Flash", ...)
    out["is_fa"] = out["style"].str.contains(r"\bFA\b|first\s*ascent", case=False, regex=True)
    out = out.reset_index(drop=True)
    out["join"] = _key(out["crag"]) + "|" + _key(out["state"])
    return out


def clean_crags(df) -> pd.DataFrame:
    cols = ["crag", "state", "country", "lat", "lon", "elev_m", "radius_m", "join"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame({
        "crag": _col(df, "Crag"),
        "state": _col(df, "State"),
        "country": _col(df, "Country", "Nation").str.upper(),
        "lat": _num(_col(df, "Lat", "y")).values,
        "lon": _num(_col(df, "Lon", "x")).values,
        "elev_m": _num(_col(df, "Elev_m")).values,       # optional
        "radius_m": _num(_col(df, "Radius_m")).values,   # optional
    })
    out = out[(out["crag"] != "") & out["lat"].notna() & out["lon"].notna()].reset_index(drop=True)
    out["join"] = _key(out["crag"]) + "|" + _key(out["state"])
    dup = out[out["join"].duplicated(keep=False)]
    if len(dup):
        print(f"WARNING: duplicate crag rows (using the first): {sorted(set(dup['crag']))}")
    return out.drop_duplicates("join").reset_index(drop=True)[cols]


def _coords(df):
    """Lat/Lon from x/y, Lon/Lat, or a single 'lat, lon' Coords column."""
    lat = _num(_col(df, "y", "Lat", "Latitude")).to_numpy(float, copy=True)
    lon = _num(_col(df, "x", "Lon", "Lng", "Longitude")).to_numpy(float, copy=True)
    coords = _col(df, "Coords", "Coordinates")
    for i, c in enumerate(coords):
        if c and (np.isnan(lat[i]) or np.isnan(lon[i])):
            m = re.findall(r"-?\d+(?:\.\d+)?", c)
            if len(m) >= 2:
                lat[i], lon[i] = float(m[0]), float(m[1])
    bad = ~((np.abs(lat) <= 90) & (np.abs(lon) <= 180))
    lat[bad], lon[bad] = np.nan, np.nan
    return lat, lon


def clean_boulders(df) -> pd.DataFrame:
    """Boulder Locations tab -> one row per boulder (or per zone, when Boulder is blank)."""
    cols = ["nation", "crag", "state", "zone", "subzone", "boulder", "lat", "lon",
            "join", "zkey", "bkey", "bkey2"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols)
    lat, lon = _coords(df)
    out = pd.DataFrame({
        "nation": _col(df, "Nation", "Country").str.upper(),
        "crag": _col(df, "Crag"), "state": _col(df, "State"), "zone": _col(df, "Zone"),
        "subzone": _col(df, "Subzone", "Sub-zone"), "boulder": _col(df, "Boulder"),
        "lat": lat, "lon": lon,
    })
    out = out[(out["crag"] != "") & ((out["boulder"] != "") | (out["zone"] != ""))].reset_index(drop=True)
    out["join"] = _key(out["crag"]) + "|" + _key(out["state"])
    out["zkey"] = out["join"] + "|" + _key(out["zone"])
    out["bkey"] = out["zkey"] + "|" + _key(out["boulder"])
    out["bkey2"] = out["join"] + "|" + _key(out["boulder"])
    dup = out[out["bkey"].duplicated(keep=False) & (out["boulder"] != "")]
    if len(dup):
        print(f"WARNING: boulder listed twice in the same zone (using the first): "
              f"{sorted(set(zip(dup['crag'], dup['boulder'])))[:10]}")
    return out.drop_duplicates("bkey").reset_index(drop=True)[cols]

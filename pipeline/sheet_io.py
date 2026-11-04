"""Read the Ticklist and Crag Locations tabs (Google Sheets API or exported CSVs)."""
import json
import os

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
    header = [str(h).strip() for h in rows[0]]
    body = [r + [""] * (len(header) - len(r)) for r in rows[1:]]
    return pd.DataFrame(body, columns=header)


def load_from_sheet(sheet_id: str):
    sh = _client().open_by_key(sheet_id)
    return _tab(sh, C.TICKLIST_TAB), _tab(sh, C.CRAGS_TAB)


def load_from_csv(ticklist_csv, crags_csv):
    return (pd.read_csv(ticklist_csv, dtype=str, keep_default_na=False),
            pd.read_csv(crags_csv, dtype=str, keep_default_na=False))


def parse_date(v):
    if v is None or str(v).strip() == "":
        return pd.NaT
    try:
        return SERIAL_ORIGIN + pd.to_timedelta(float(v), unit="D")
    except (TypeError, ValueError):
        return pd.to_datetime(v, errors="coerce")


def _num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.strip().replace("", None), errors="coerce")


def _key(s):
    return pd.Series(s).astype(str).str.strip().str.casefold()


def clean_ticklist(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only the public columns. Notes and Beta never leave this function."""
    out = pd.DataFrame({
        "date": df["Date"].map(parse_date).dt.normalize(),
        "grade": df["Grade"].astype(str).str.strip(),
        "stars": _num(df["Stars"]).values,
        "name": df["Name"].astype(str).str.strip(),
        "crag": df["Crag"].astype(str).str.strip(),
        "state": df["State"].astype(str).str.strip(),
        "sessions": _num(df["NumberSessions"]).values,
        "style": df.get("Style", pd.Series([""] * len(df))).astype(str).str.strip(),
        "points": _num(df["8a points"]).values,
    })
    # FA flag from the Style column ("FA", "fa", "First Ascent", "FA / Flash", ...)
    out["is_fa"] = out["style"].str.contains(r"\bFA\b|first\s*ascent", case=False, regex=True)
    out = out[out["name"] != ""].reset_index(drop=True)
    out["join"] = _key(out["crag"]) + "|" + _key(out["state"])
    return out


def clean_crags(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "crag": df["Crag"].astype(str).str.strip(),
        "state": df["State"].astype(str).str.strip(),
        "country": df["Country"].astype(str).str.strip().str.upper(),
        "lat": _num(df["Lat"]).values,
        "lon": _num(df["Lon"]).values,
        # optional columns
        "elev_m": _num(df["Elev_m"]).values if "Elev_m" in df else float("nan"),
        "radius_m": _num(df["Radius_m"]).values if "Radius_m" in df else float("nan"),
    })
    out = out[(out["crag"] != "") & out["lat"].notna() & out["lon"].notna()].reset_index(drop=True)
    out["join"] = _key(out["crag"]) + "|" + _key(out["state"])
    dup = out[out["join"].duplicated(keep=False)]
    if len(dup):
        print(f"WARNING: duplicate crag rows (using the first): {sorted(set(dup['crag']))}")
    return out.drop_duplicates("join").reset_index(drop=True)

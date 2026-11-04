"""Earth Engine access: daily point series from gridMET / ERA5-Land, and elevations."""
import json
import math
import os
import time

import numpy as np
import pandas as pd

from . import config as C

SOURCES = {
    "gridmet": dict(
        asset="IDAHO_EPSCOR/GRIDMET",
        scale=4638.3,
        # name used in this code -> band names to look for, in order (datasets rename bands)
        bands={"tmmx": ["tmmx"], "tmmn": ["tmmn"], "sph": ["sph"], "pr": ["pr"],
               "srad": ["srad"], "vs": ["vs"], "th": ["th"], "pet": ["eto", "pet"],
               "etr": ["etr"], "erc": ["erc"], "bi": ["bi"], "fm100": ["fm100"],
               "fm1000": ["fm1000"]},
        required=["tmmx", "tmmn", "sph", "pr"],
    ),
    "era5land": dict(
        asset="ECMWF/ERA5_LAND/DAILY_AGGR",
        scale=11132.0,
        bands={k: [k] for k in [
            "temperature_2m_max", "temperature_2m_min", "dewpoint_temperature_2m",
            "total_precipitation_sum", "surface_solar_radiation_downwards_sum",
            "u_component_of_wind_10m", "v_component_of_wind_10m",
            "potential_evaporation_sum"]},
        required=["temperature_2m_max", "temperature_2m_min",
                  "dewpoint_temperature_2m", "total_precipitation_sum"],
    ),
}

_ee = None
_resolved = {}   # source -> {actual band name: name used in this code}


def _bands(source):
    """Match our band list against what the dataset actually has (checked once per run)."""
    if source not in _resolved:
        s = SOURCES[source]
        have = set(_ee.ImageCollection(s["asset"]).limit(1, "system:time_start", False)
                   .first().bandNames().getInfo())
        found = {}
        for name, candidates in s["bands"].items():
            hit = next((c for c in candidates if c in have), None)
            if hit:
                found[hit] = name
            elif name in s["required"]:
                raise RuntimeError(f"{s['asset']} has no band for {name}; available: {sorted(have)}")
            else:
                print(f"  ! {s['asset']}: no '{name}' band (looked for {candidates}); it will be blank")
        _resolved[source] = found
    return _resolved[source]


def init():
    """Service account key from GCP_SA_KEY (GitHub) or the file named by
    GOOGLE_APPLICATION_CREDENTIALS (your PC); otherwise your personal GEE login."""
    global _ee
    import ee
    key = os.environ.get("GCP_SA_KEY")
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key and path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            key = f.read()
    if key:
        info = json.loads(key)
        creds = ee.ServiceAccountCredentials(info["client_email"], key_data=key)
        ee.Initialize(creds, project=C.GEE_PROJECT)
    else:
        ee.Initialize(project=C.GEE_PROJECT)
    _ee = ee


def pick_source(country: str, lon: float, lat: float) -> str:
    w, s, e, n = C.CONUS_BBOX
    if country.upper() in ("US", "USA") and w <= lon <= e and s <= lat <= n:
        return "gridmet"
    return "era5land"


# --- Daily series ------------------------------------------------------------
def _get_region(source, lon, lat, start, end):
    ee = _ee
    s = SOURCES[source]
    bands = _bands(source)
    col = ee.ImageCollection(s["asset"]).filterDate(start, end).select(list(bands))
    try:
        rows = col.getRegion(ee.Geometry.Point(lon, lat), s["scale"]).getInfo()
    except Exception as err:
        # Earth Engine raises this for a date range with no images yet (e.g. the last
        # day or two, before the dataset publishes them). That just means "no new data".
        if "no bands in collection" in str(err).lower():
            rows = []
        else:
            raise
    if len(rows) < 2:
        return pd.DataFrame(columns=list(s["bands"]))
    df = pd.DataFrame(rows[1:], columns=rows[0]).rename(columns=bands)
    for name in s["bands"]:                      # missing optional bands -> blank column
        if name not in df:
            df[name] = np.nan
    df["date"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.drop(columns=["id", "longitude", "latitude", "time"]).set_index("date").sort_index()
    df = df[~df.index.duplicated()]
    return df.apply(pd.to_numeric, errors="coerce")


def _standardize(source, raw: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    if source == "gridmet":
        out["tmax_c"] = raw["tmmx"] - 273.15
        out["tmin_c"] = raw["tmmn"] - 273.15
        out["sph"] = raw["sph"]
        out["pr_mm"] = raw["pr"]
        out["srad_wm2"] = raw["srad"]
        out["wind_ms"] = raw["vs"]
        out["wind_dir"] = raw["th"]
        out["pet_mm"] = raw["pet"]
        out["etr_mm"] = raw["etr"]
        for b in ("erc", "bi", "fm100", "fm1000"):
            out[b] = raw[b]
    else:
        u, v = raw["u_component_of_wind_10m"], raw["v_component_of_wind_10m"]
        out["tmax_c"] = raw["temperature_2m_max"] - 273.15
        out["tmin_c"] = raw["temperature_2m_min"] - 273.15
        out["td_c"] = raw["dewpoint_temperature_2m"] - 273.15
        out["pr_mm"] = raw["total_precipitation_sum"] * 1000.0
        out["srad_wm2"] = raw["surface_solar_radiation_downwards_sum"] / 86400.0
        out["wind_ms"] = np.hypot(u, v)
        out["wind_dir"] = (np.degrees(np.arctan2(-u, -v)) + 360) % 360   # direction wind blows FROM
        out["pet_mm"] = -raw["potential_evaporation_sum"] * 1000.0        # ECMWF: evaporation is negative
    return out.dropna(subset=["tmax_c", "tmin_c"], how="all")


_RETRYABLE = ("memory limit", "timed out", "too many concurrent", "rate limit",
              "quota", "internal error", "service unavailable", "deadline")
MIN_CHUNK_DAYS = 90


def _fetch_range(source, lon, lat, a: pd.Timestamp, b: pd.Timestamp, depth=0):
    """Fetch [a, b). If Earth Engine refuses (memory/timeout/busy), split in half and retry."""
    try:
        return [_get_region(source, lon, lat, a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"))]
    except Exception as err:            # ee.EEException, HttpError, socket errors
        msg = str(err).lower()
        if not any(k in msg for k in _RETRYABLE):
            raise
        if (b - a).days <= MIN_CHUNK_DAYS:
            if depth > 8:
                raise
            time.sleep(min(60, 5 * 2 ** min(depth, 4)))      # small chunk still refused: wait
            return _fetch_range(source, lon, lat, a, b, depth + 1)
        mid = a + (b - a) / 2
        mid = mid.normalize()
        print(f"    Earth Engine busy; splitting {a:%Y-%m-%d}..{b:%Y-%m-%d}")
        return (_fetch_range(source, lon, lat, a, mid, depth + 1)
                + _fetch_range(source, lon, lat, mid, b, depth + 1))


def fetch_daily(source, lon, lat, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Standardized daily series for [start, end), requested in 2-year chunks."""
    parts, t = [], start
    while t < end:
        t2 = min(t + pd.DateOffset(years=2), end)
        for raw in _fetch_range(source, lon, lat, t, t2):
            if len(raw):
                parts.append(_standardize(source, raw))
        t = t2
    return pd.concat(parts).sort_index() if parts else pd.DataFrame()


def valid_sample_point(source, lon, lat):
    """ERA5-Land is land-only, so a coastal crag (Norway) can land in a masked
    sea cell. Test the crag's cell, then its 8 neighbours, and use the nearest
    one that has data."""
    res = SOURCES[source]["scale"] / 111320.0
    probe = ("2020-07-01", "2020-07-03")
    cands = [(0, 0)] + [(i, j) for i in (-1, 0, 1) for j in (-1, 0, 1) if (i, j) != (0, 0)]
    cands.sort(key=lambda ij: math.hypot(*ij))
    for i, j in cands:
        plon, plat = lon + i * res / max(math.cos(math.radians(lat)), 0.2), lat + j * res
        df = _get_region(source, plon, plat, *probe)
        if len(df) and df.iloc[:, 0].notna().any():
            return plon, plat
    return None


# --- Elevations ----------------------------------------------------------------
_dem_cache = {}


def dem_asset():
    """First DEM in config.DEM_ASSETS that exists, with its elevation band name."""
    if "asset" not in _dem_cache:
        for asset in C.DEM_ASSETS:
            try:
                bands = _ee.ImageCollection(asset).first().bandNames().getInfo()
            except Exception:
                continue
            band = "DEM" if "DEM" in bands else bands[0]
            _dem_cache.update(asset=asset, band=band)
            break
        else:
            raise RuntimeError(f"None of these DEMs are available: {C.DEM_ASSETS}")
    return _dem_cache["asset"]


def _dem():
    dem_asset()
    return (_ee.ImageCollection(_dem_cache["asset"]).select([_dem_cache["band"]], ["DEM"])
            .mosaic())


def cell_bounds(source, lon, lat):
    """Bounds of the source grid cell containing (lon, lat)."""
    ee = _ee
    proj = ee.ImageCollection(SOURCES[source]["asset"]).first().projection().getInfo()
    if proj.get("crs") == "EPSG:4326" and "transform" in proj:
        xs, _, x0, _, ys, y0 = proj["transform"]
        col, row = math.floor((lon - x0) / xs), math.floor((lat - y0) / ys)
        xa, xb = x0 + col * xs, x0 + (col + 1) * xs
        ya, yb = y0 + row * ys, y0 + (row + 1) * ys
        return [min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)]
    h = SOURCES[source]["scale"] / 2 / 111320.0            # fallback: centred square
    return [lon - h, lat - h, lon + h, lat + h]


def elevations(source, sample_lon, sample_lat, crag_lon, crag_lat, elev_override=None, radius_m=None):
    """Returns (z_crag, z_grid, cell_bounds)."""
    ee = _ee
    dem = _dem()
    bounds = cell_bounds(source, sample_lon, sample_lat)
    z_grid = dem.reduceRegion(ee.Reducer.mean(), ee.Geometry.Rectangle(bounds),
                              C.DEM_SCALE_CELL, maxPixels=1e8).get("DEM").getInfo()
    if elev_override is not None:
        z_crag = float(elev_override)
    else:
        geom = ee.Geometry.Point(crag_lon, crag_lat)
        if radius_m:
            geom = geom.buffer(float(radius_m))
        z_crag = dem.reduceRegion(ee.Reducer.mean(), geom, C.DEM_SCALE_CRAG,
                                  maxPixels=1e8).get("DEM").getInfo()
    if z_grid is None:          # cell is mostly sea in the DEM
        z_grid = z_crag
    return float(z_crag), float(z_grid), bounds

"""Live send-conditions scoreboard.

    python -m pipeline.conditions        # writes docs/data/conditions.json

For every crag: fetch the Open-Meteo forecast (past 2 weeks + next 7 days, hourly, at the weather
grid cell's elevation), roll it up to daily values the same way as the historical record, remove
each crag's systematic offset from gridMET/ERA5 on the days both cover, then apply the same lapse-
rate correction and derived variables. Each forecast day is scored with the crag's regional model
from drivers.json and reported as a percentile against that crag's typical days for the season.
"""
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C
from . import physics
from .build import CragCache
from .drivers import transform

OUT = C.ROOT / "docs" / "data" / "conditions.json"
DRIVERS = C.ROOT / "docs" / "data" / "drivers.json"
API = "https://api.open-meteo.com/v1/forecast"
HOURLY = ["temperature_2m", "dew_point_2m", "precipitation", "wind_speed_10m",
          "wind_direction_10m", "shortwave_radiation"]
PAST_DAYS = 14             # overlap with gridMET/ERA5 for calibration, and recent rain
FORECAST_DAYS = 8          # today + 7
CHUNK = 20                 # locations per request
BIAS_VARS = ["tmax_c", "tmin_c", "td_c", "srad_wm2", "wind_ms"]
MIN_OVERLAP = 5            # days needed before applying a calibration offset
MAX_OFFSET = {"tmax_c": 6, "tmin_c": 6, "td_c": 8, "srad_wm2": 120, "wind_ms": 4}
SEASON_DAYS = 45           # "in season": climbed at the crag within +/-6 weeks of the date, any year
MIN_OWN_DAYS = 3           # below this, a crag uses its region's climbing days instead
PRIME = 95                 # percentile that counts as prime
SHOW = ["tmax", "tmin", "rmin", "dewpoint_spread", "pr", "rain3", "days_since_rain", "wind", "srad", "vpd"]


# ----------------------------------------------------------------------------- fetch
def fetch_open_meteo(points):
    """points: [(lat, lon, elevation)] -> list of Open-Meteo responses, same order."""
    out = []
    for i in range(0, len(points), CHUNK):
        part = points[i:i + CHUNK]
        q = dict(latitude=",".join(f"{p[0]:.5f}" for p in part),
                 longitude=",".join(f"{p[1]:.5f}" for p in part),
                 elevation=",".join(f"{p[2]:.0f}" for p in part),
                 hourly=",".join(HOURLY), timezone="auto", wind_speed_unit="ms",
                 past_days=PAST_DAYS, forecast_days=FORECAST_DAYS)
        url = API + "?" + urllib.parse.urlencode(q)
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ticklist-map conditions"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode())
                break
            except Exception as err:      # rate limit, network blip: back off and retry
                if attempt == 3:
                    raise
                print(f"  Open-Meteo request failed ({err}); retrying")
                time.sleep(10 * 2 ** attempt)
        out += data if isinstance(data, list) else [data]
        time.sleep(1)
    return out


def hourly_to_daily(resp):
    """Open-Meteo hourly (local time) -> daily values in the pipeline's standard columns."""
    h = resp["hourly"]
    df = pd.DataFrame({k: h.get(k) for k in HOURLY})
    df["day"] = pd.to_datetime(pd.Series(h["time"]).str[:10])
    g = df.groupby("day")
    rad = np.radians(df["wind_direction_10m"].astype(float))
    df["u"], df["v"] = np.sin(rad), np.cos(rad)
    out = pd.DataFrame({
        "tmax_c": g["temperature_2m"].max(),
        "tmin_c": g["temperature_2m"].min(),
        "td_c": g["dew_point_2m"].mean(),
        "pr_mm": g["precipitation"].sum(min_count=18),
        "srad_wm2": g["shortwave_radiation"].mean(),
        "wind_ms": g["wind_speed_10m"].mean(),
        "wind_dir": (np.degrees(np.arctan2(df.groupby("day")["u"].mean(), df.groupby("day")["v"].mean())) + 360) % 360,
    })
    complete = g["temperature_2m"].count() >= 18
    return out[complete]


# ----------------------------------------------------------------------------- per crag
def as_dewpoint(raw, z_grid):
    """Historical raw series -> same columns as the forecast (gridMET stores specific humidity)."""
    r = raw.copy()
    if "sph" in r and r["sph"].notna().any():
        e = physics.e_from_q(r["sph"], physics.pressure_kpa(z_grid))
        r["td_c"] = physics.td_from_e(e)
        r = r.drop(columns=["sph"])
    return r


def calibrate(fc, hist):
    """Shift each forecast variable by its mean difference from the historical record on the
    days both cover (capped, and only with enough overlap). Returns (fc, offsets)."""
    both = fc.index.intersection(hist.index)
    offsets = {}
    fc = fc.copy()
    for k in BIAS_VARS:
        if k not in hist or len(both) < MIN_OVERLAP:
            continue
        d = (hist.loc[both, k] - fc.loc[both, k]).dropna()
        if len(d) < MIN_OVERLAP:
            continue
        off = float(np.clip(d.mean(), -MAX_OFFSET[k], MAX_OFFSET[k]))
        fc[k] = fc[k] + off
        offsets[k] = round(off, 2)
    fc["wind_ms"] = fc["wind_ms"].clip(lower=0)
    fc["srad_wm2"] = fc["srad_wm2"].clip(lower=0)
    return fc, offsets


def crag_frame(raw, fc, meta):
    """History (last 120 days) + calibrated forecast for later dates -> corrected daily series."""
    hist = as_dewpoint(raw, meta["z_grid"])
    hist = hist[hist.index >= hist.index.max() - pd.Timedelta(days=120)]
    fc, offsets = calibrate(fc, hist)
    later = fc[fc.index > hist.index.max()]
    cols = sorted(set(hist.columns) | set(later.columns))
    combined = pd.concat([hist.reindex(columns=cols), later.reindex(columns=cols)]).sort_index()
    s = physics.correct_to_crag(combined, meta["z_grid"], meta["z_crag"])
    pr = s["pr"].asfreq("D")
    s["rain3"] = pr.shift(1).rolling(3, min_periods=1).sum().reindex(s.index)
    s["days_since_rain"] = s["days_since_rain"].clip(upper=60)
    return s, offsets, fc.index.min()


# ----------------------------------------------------------------------------- scoring
class Model:
    def __init__(self, m):
        self.vars, self.how = m["vars"], m["how"]
        self.mu, self.sd, self.beta = np.array(m["mu"]), np.array(m["sd"]), np.array(m["beta"])

    def eta(self, X):
        """X: rows x len(vars), raw units -> score (higher = better send conditions)."""
        z = np.column_stack([(transform(X[:, i], h) - self.mu[i]) / self.sd[i] for i, h in enumerate(self.how)])
        k = len(self.vars)
        return z @ self.beta[:k] + (z ** 2) @ self.beta[k:]


def score_day(model, row, base):
    """Percentile of a day's score among the crag's same-season days, plus each condition's
    contribution compared with a typical (median) day."""
    x = np.array([[row[v] for v in model.vars]], float)
    if np.isnan(x).any():
        return None
    Xb = base[model.vars].dropna().to_numpy(float)
    if len(Xb) < 50:
        return None
    eb, e = model.eta(Xb), model.eta(x)[0]
    pct = 100.0 * ((eb < e).sum() + 0.5 * (eb == e).sum()) / len(eb)
    med = np.median(Xb, axis=0)
    contrib = {}
    for i, v in enumerate(model.vars):
        xt = x.copy()
        xt[0, i] = med[i]
        contrib[v] = round(float(e - model.eta(xt)[0]), 3)
    typical = {v: round(float(med[i]), 2) for i, v in enumerate(model.vars)}
    return round(float(pct), 1), contrib, typical


def season_days(doys, when, window=SEASON_DAYS):
    d = np.abs(np.asarray(doys) - when.dayofyear)
    return int((np.minimum(d, 366 - d) <= window).sum())


# ----------------------------------------------------------------------------- main
def run(out_path=OUT, drivers_path=DRIVERS, fetch=fetch_open_meteo, cache_dir=None, send_path=None):
    from .drivers import load            # historical corrected series, same as the analysis
    drv = json.loads(drivers_path.read_text(encoding="utf-8"))
    d, crags, series, sends = load(send_path or C.OUT_PATH, cache_dir)
    overall = Model(drv["model"]) if drv.get("model") else None
    regions = {r["id"]: r for r in drv["regions"]}
    region_of = drv.get("crag_region", {})

    # climbing days (dates) by region, for "in season"
    s = sends[sends["crag_id"].notna() & ~sends["date_approx"].astype(bool) & sends["date"].notna()]
    s = s.assign(doy=pd.to_datetime(s["date"]).dt.dayofyear, region=s["crag_id"].map(region_of))
    day_doys = s.drop_duplicates(["crag_id", "date"])

    ids = [cid for cid in crags if cid in series]
    metas = {}
    for cid in ids:
        cache = CragCache(cid) if cache_dir is None else _cache_at(cache_dir, cid)
        metas[cid] = cache.load()
    pts = [(crags[c]["lat"], crags[c]["lon"], metas[c][0]["z_grid"]) for c in ids]
    print(f"Conditions: fetching forecasts for {len(ids)} crags")
    responses = fetch(pts)

    rows = []
    for cid, resp in zip(ids, responses):
        c, (meta, raw) = crags[cid], metas[cid]
        try:
            fc = hourly_to_daily(resp)
        except Exception as err:
            print(f"  ! {c['name']}: unreadable forecast ({err})")
            continue
        sser, offsets, first = crag_frame(raw, fc, meta)
        rid = region_of.get(cid)
        reg = regions.get(rid) if rid else None
        use_region = bool(reg and reg.get("enough") and reg.get("model"))
        model = Model(reg["model"]) if use_region else overall
        if model is None:
            continue
        today = fc.index.max() - pd.Timedelta(days=FORECAST_DAYS - 1)
        days = []
        for t in pd.date_range(today, fc.index.max()):
            if t not in sser.index:
                continue
            base = series[cid][physics.baseline_mask(series[cid].index, t)]
            sc = score_day(model, sser.loc[t], base)
            if sc is None:
                continue
            pct, contrib, typical = sc
            vals = {k: (None if pd.isna(sser.loc[t].get(k)) else round(float(sser.loc[t][k]), 2)) for k in SHOW}
            days.append(dict(date=t.strftime("%Y-%m-%d"), score=pct, prime=pct >= PRIME,
                             contrib=contrib, typical=typical, vals=vals))
        if not days:
            continue
        # in season: climbed at this crag within +/-6 weeks of the date in some year
        # (crags with only a couple of days borrow their region's history)
        own = day_doys[day_doys["crag_id"] == cid]["doy"]
        doys = own if len(own) >= MIN_OWN_DAYS or not rid else day_doys[day_doys["region"] == rid]["doy"]
        n_season = season_days(doys, today)
        rows.append(dict(
            id=cid, name=c["name"], state=c["state"], country=c["country"],
            region_id=rid, region=reg["name"] if reg else None,
            model="region" if use_region else "overall", model_vars=model.vars,
            in_season=n_season > 0, season_visits=n_season, season_basis="crag" if len(own) >= MIN_OWN_DAYS or not rid else "region",
            offsets=offsets, days=days,
        ))
    rows.sort(key=lambda r: (-r["in_season"], -r["days"][0]["score"]))
    out = dict(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source="Open-Meteo", prime=PRIME, season_days=SEASON_DAYS,
        variables=drv["variables"], crags=rows,
        regions=[dict(id=r["id"], name=r["name"], enough=r["enough"],
                      cv=(r.get("model") or {}).get("cv")) for r in drv["regions"]],
        overall_cv=(drv.get("model") or {}).get("cv"),
    )
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    prime = sum(dd["prime"] for r in rows if r["in_season"] for dd in r["days"])
    print(f"Wrote {out_path}: {len(rows)} crags, {sum(r['in_season'] for r in rows)} in season, "
          f"{prime} prime crag-days ahead")
    return out


def _cache_at(cache_dir, cid):
    c = CragCache(cid)
    c.dir = cache_dir / cid
    c.meta_path, c.daily_path = c.dir / "meta.json", c.dir / "daily.csv.gz"
    return c


if __name__ == "__main__":
    run()

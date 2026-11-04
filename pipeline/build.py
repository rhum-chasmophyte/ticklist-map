"""Build docs/data/sends.json from the Sheet + Earth Engine.

    python -m pipeline.build                          # live: Sheet + GEE (needs SHEET_ID)
    python -m pipeline.build --ticklist-csv t.csv --crags-csv c.csv   # from CSV exports
    python -m pipeline.build ... --fake-climate       # offline test, synthetic weather
"""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C
from . import physics, sheet_io
from .gee_fetch import pick_source

TODAY = pd.Timestamp.today().normalize()
SERIES_START = pd.Timestamp(f"{C.BASELINE_START}-01-01")


def slug(*parts):
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


# --- Per-crag cache ------------------------------------------------------------
class CragCache:
    """cache/<crag_id>/meta.json + daily.csv.gz holding the *uncorrected* source series."""

    def __init__(self, crag_id):
        self.dir = C.CACHE_DIR / crag_id
        self.meta_path = self.dir / "meta.json"
        self.daily_path = self.dir / "daily.csv.gz"

    def load(self):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}
        daily = (pd.read_csv(self.daily_path, index_col="date", parse_dates=["date"])
                 if self.daily_path.exists() else pd.DataFrame())
        return meta, daily

    def save(self, meta, daily):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        daily.index.name = "date"
        daily.round(4).to_csv(self.daily_path, compression="gzip")


def update_crag(crag, start, fetcher):
    """Return (meta, daily) for a crag, fetching only what the cache lacks."""
    cache = CragCache(crag["id"])
    meta, daily = cache.load()
    loc = dict(lat=round(crag["lat"], 5), lon=round(crag["lon"], 5), source=crag["source"])
    elev = dict(elev_m=None if pd.isna(crag["elev_m"]) else float(crag["elev_m"]),
                radius_m=None if pd.isna(crag["radius_m"]) else float(crag["radius_m"]))

    if any(meta.get(k) != v for k, v in loc.items()):            # moved pin: start over
        meta, daily = dict(loc), pd.DataFrame()
        pt = fetcher.sample_point(crag["source"], crag["lon"], crag["lat"])
        if pt is None:
            print(f"  ! {crag['crag']}: no {crag['source']} data near this point")
            return None, None
        meta["sample_lon"], meta["sample_lat"] = pt
    dem = fetcher.dem_asset()
    if (any(meta.get(k) != v for k, v in elev.items()) or "z_crag" not in meta
            or meta.get("dem") != dem):
        z_crag, z_grid, cell = fetcher.elevations(
            crag["source"], meta["sample_lon"], meta["sample_lat"],
            crag["lon"], crag["lat"], elev["elev_m"], elev["radius_m"])
        meta.update(elev, z_crag=round(z_crag, 1), z_grid=round(z_grid, 1), cell=cell, dem=dem)
        cache.save(meta, daily)

    gaps = []
    if daily.empty:
        gaps.append((start, TODAY + pd.Timedelta(days=1)))
    else:
        if start < daily.index.min():
            gaps.append((start, daily.index.min()))
        gaps.append((daily.index.max() + pd.Timedelta(days=1), TODAY + pd.Timedelta(days=1)))
    for a, b in gaps:
        while a < b:            # 2-year steps, saving after each, so a crash loses little
            step = min(a + pd.DateOffset(years=2), b)
            new = fetcher.daily(crag["source"], meta["sample_lon"], meta["sample_lat"], a, step)
            if len(new):
                daily = pd.concat([daily, new]).sort_index() if len(daily) else new
                daily = daily[~daily.index.duplicated(keep="last")]
                cache.save(meta, daily)
            a = step
    meta["last_date"] = daily.index.max().strftime("%Y-%m-%d") if len(daily) else None
    cache.save(meta, daily)
    return meta, daily


# --- Fetchers ------------------------------------------------------------------
class GEEFetcher:
    def __init__(self):
        from . import gee_fetch
        gee_fetch.init()
        self.g = gee_fetch

    def sample_point(self, source, lon, lat):
        return self.g.valid_sample_point(source, lon, lat)

    def elevations(self, *a):
        return self.g.elevations(*a)

    def dem_asset(self):
        return self.g.dem_asset()

    def daily(self, source, lon, lat, a, b):
        return self.g.fetch_daily(source, lon, lat, a, b)


class FakeFetcher:
    """Synthetic but plausible weather, for testing everything downstream of GEE."""

    def sample_point(self, source, lon, lat):
        return lon, lat

    def dem_asset(self):
        return "fake-dem"

    def elevations(self, source, slon, slat, lon, lat, elev, radius):
        z_crag = elev if elev is not None else 1500 + 400 * np.sin(lat + lon)
        return float(z_crag), float(z_crag + 250), [lon - .02, lat - .02, lon + .02, lat + .02]

    def daily(self, source, lon, lat, a, b):
        end = min(b, TODAY - pd.Timedelta(days=6 if source == "era5land" else 1))
        idx = pd.date_range(a, end - pd.Timedelta(days=1), freq="D")
        rng = np.random.default_rng(abs(hash((round(lon, 3), round(lat, 3), a.year))) % 2**32)
        season = np.cos(2 * np.pi * (idx.dayofyear - 200) / 365.25) * (1 if lat > 0 else -1)
        tmax = 15 + 13 * season + rng.normal(0, 4, len(idx))
        d = pd.DataFrame(index=idx)
        d["tmax_c"], d["tmin_c"] = tmax, tmax - 12 - rng.gamma(2, 1.5, len(idx))
        d["pr_mm"] = np.where(rng.random(len(idx)) < .25, rng.gamma(1.2, 6, len(idx)), 0)
        d["srad_wm2"] = 200 + 100 * season + rng.normal(0, 30, len(idx))
        d["wind_ms"], d["wind_dir"] = rng.gamma(3, 1.2, len(idx)), rng.uniform(0, 360, len(idx))
        d["pet_mm"] = np.clip(3 + 3 * season + rng.normal(0, 1, len(idx)), 0, None)
        if source == "gridmet":
            d["sph"] = physics.q_from_e(physics.es(d["tmin_c"] - rng.gamma(2, 2, len(idx))), 85)
            d["etr_mm"] = d["pet_mm"] * 1.3
            d["erc"], d["bi"] = rng.gamma(4, 10, len(idx)), rng.gamma(3, 12, len(idx))
            d["fm100"], d["fm1000"] = rng.uniform(4, 25, len(idx)), rng.uniform(6, 28, len(idx))
        else:
            d["td_c"] = d["tmin_c"] - rng.gamma(2, 2, len(idx))
        return d


# --- Main ---------------------------------------------------------------------------
def clean_value(v, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    return round(float(v), nd)


def build(ticklist_raw, crags_raw, fetcher, out_path=C.OUT_PATH):
    sends = sheet_io.clean_ticklist(ticklist_raw)
    crags = sheet_io.clean_crags(crags_raw)
    crags["id"] = [slug(r.country, r.state, r.crag) for r in crags.itertuples()]
    crags["source"] = [pick_source(r.country, r.lon, r.lat) for r in crags.itertuples()]

    sends = sends.merge(crags[["join", "id", "country"]], on="join", how="left")
    missing = sends[sends["id"].isna()]
    if len(missing):
        pairs = sorted(set(zip(missing["crag"], missing["state"])))
        print(f"WARNING: {len(missing)} sends have no Crag Locations row: {pairs}")

    var_meta = {v["key"]: v for v in C.VARIABLES}
    crag_out, climate = [], {}
    for crag in crags.to_dict("records"):
        cs = sends[sends["id"] == crag["id"]]
        if cs.empty:
            continue
        first = cs["date"].min()
        start = min(SERIES_START, first - pd.Timedelta(days=120)) if pd.notna(first) else SERIES_START
        print(f"- {crag['crag']} ({crag['state']}, {crag['country']}) via {crag['source']}")
        meta, daily = update_crag(crag, start, fetcher)
        if meta is None:
            continue
        series = physics.correct_to_crag(daily, meta["z_grid"], meta["z_crag"])
        crag_out.append(dict(
            id=crag["id"], name=crag["crag"], state=crag["state"], country=crag["country"],
            lat=crag["lat"], lon=crag["lon"], source=crag["source"],
            z_crag=meta["z_crag"], z_grid=meta["z_grid"],
            dz=round(meta["z_grid"] - meta["z_crag"], 1), climate_through=meta["last_date"]))

        for s in cs.itertuples():
            if pd.isna(s.date):
                climate[s.Index] = dict(status="no_date")
                continue
            if s.date not in series.index or series.loc[s.date, ["tmax"]].isna().all():
                climate[s.Index] = dict(status="pending", source=crag["source"])
                continue
            row = series.loc[s.date]
            base = series[physics.baseline_mask(series.index, s.date)]
            values, pct, typical = {}, {}, {}
            for k in C.VAR_KEYS:
                x = row.get(k, np.nan)
                values[k] = clean_value(x)
                if values[k] is None:
                    continue
                if var_meta[k].get("pct", True):
                    pct[k] = physics.percentile_of(base[k].to_numpy(float), float(x))
                    typical[k] = clean_value(base[k].median())
            climate[s.Index] = dict(
                status="ok", source=crag["source"], values=values, pct=pct, typical=typical,
                tmax_grid=clean_value(row["tmax_grid"]), tmin_grid=clean_value(row["tmin_grid"]))

    send_out = []
    for s in sends.itertuples():
        sid = hashlib.md5(f"{s.date}|{s.name}|{s.crag}".encode()).hexdigest()[:10]
        send_out.append(dict(
            id=sid, date=None if pd.isna(s.date) else s.date.strftime("%Y-%m-%d"),
            name=s.name, grade=s.grade, stars=clean_value(s.stars, 0), style=s.style,
            is_fa=bool(s.is_fa),
            sessions=clean_value(s.sessions, 0), points=clean_value(s.points, 1),
            crag_id=None if pd.isna(s.id) else s.id, crag=s.crag, state=s.state,
            country=None if pd.isna(s.country) else s.country,
            climate=climate.get(s.Index, dict(status="no_location"))))

    summary = {}
    for k in C.VAR_KEYS:
        if not var_meta[k].get("pct", True):
            continue
        p = [s["climate"]["pct"][k] for s in send_out
             if s["climate"].get("status") == "ok" and s["climate"]["pct"].get(k) is not None]
        if p:
            summary[k] = dict(n=len(p), median_pct=round(float(np.median(p)), 1),
                              ks_p=physics.ks_uniform_p(p))

    doc = dict(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        settings=dict(baseline=[C.BASELINE_START, C.BASELINE_END], window_days=C.WINDOW_DAYS,
                      rain_threshold_mm=C.RAIN_THRESHOLD_MM,
                      lapse_c_per_km=dict(tmax=C.LAPSE_TMAX, tmin=C.LAPSE_TMIN)),
        variables=C.VARIABLES, crags=crag_out, sends=send_out, summary=summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    st = pd.Series([s["climate"]["status"] for s in send_out]).value_counts().to_dict()
    print(f"Wrote {out_path} — {len(send_out)} sends, {len(crag_out)} crags, climate: {st}")
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticklist-csv")
    ap.add_argument("--crags-csv")
    ap.add_argument("--fake-climate", action="store_true")
    ap.add_argument("--out", default=str(C.OUT_PATH))
    a = ap.parse_args()
    if a.ticklist_csv:
        t, c = sheet_io.load_from_csv(a.ticklist_csv, a.crags_csv)
    else:
        t, c = sheet_io.load_from_sheet(os.environ["SHEET_ID"])
    from pathlib import Path
    out = Path(a.out)
    if a.fake_climate:      # keep made-up weather away from the real cache and website
        C.CACHE_DIR = C.ROOT / "test-output" / "cache"
        if a.out == str(C.OUT_PATH):
            out = C.ROOT / "test-output" / "sends.json"
        fetcher = FakeFetcher()
    else:
        fetcher = GEEFetcher()
    build(t, c, fetcher, out)


if __name__ == "__main__":
    main()

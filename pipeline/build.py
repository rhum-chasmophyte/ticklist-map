"""Build docs/data/sends.json from the Sheet + Earth Engine.

    python -m pipeline.build                          # live: Sheet + GEE (needs SHEET_ID)
    python -m pipeline.build --ticklist-csv t.csv --crags-csv c.csv --boulders-csv b.csv  # CSVs
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
    pts = [[round(a, 5), round(b, 5)] for a, b in (crag.get("points") or [])]
    elev = dict(elev_m=None if pd.isna(crag["elev_m"]) else float(crag["elev_m"]),
                radius_m=None if pd.isna(crag["radius_m"]) else float(crag["radius_m"]),
                pts_hash=hashlib.md5(json.dumps(pts).encode()).hexdigest()[:10])

    if any(meta.get(k) != v for k, v in loc.items()):
        if (meta.get("source") == crag["source"] and meta.get("cell") and not daily.empty
                and in_cell(meta["cell"], crag["lon"], crag["lat"])):
            meta.update(loc)        # moved within the same weather grid cell: keep the weather
            meta.pop("z_crag", None)
        else:                       # moved to another cell (or new crag): start over
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
            crag["lon"], crag["lat"], elev["elev_m"], elev["radius_m"], pts or None)
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


def in_cell(cell, lon, lat, tol=1e-6):
    w, s, e, n = cell
    return w - tol <= lon <= e + tol and s - tol <= lat <= n + tol


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

    def elevations(self, source, slon, slat, lon, lat, elev, radius, points=None):
        z_crag = elev if elev is not None else 1500 + 400 * np.sin(lat + lon)
        h = 0.02 if source == "gridmet" else 0.05
        return float(z_crag), float(z_crag + 250), [slon - h, slat - h, slon + h, slat + h]

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


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = (np.sin((p2 - p1) / 2) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def build_crags(sends, pins, bl):
    """One row per crag that has sends. Position: centre of its georeferenced boulders
    (CRAG_POINT_FROM_BOULDERS), else its Crag Locations pin. Also drops boulder points that
    sit far from the rest of their crag (swapped x/y, missing minus sign)."""
    pins = pins.set_index("join")
    bl = bl.copy()
    far = []
    for j, grp in bl[bl["lat"].notna()].groupby("join"):
        ref = ((pins.loc[j, "lat"], pins.loc[j, "lon"]) if j in pins.index
               else (grp["lat"].median(), grp["lon"].median()))
        d = _haversine_km(ref[0], ref[1], grp["lat"].values, grp["lon"].values)
        bad = grp.index[d > C.BOULDER_FAR_KM]
        far += [(bl.loc[i, "crag"], bl.loc[i, "boulder"] or bl.loc[i, "zone"]) for i in bad]
        bl.loc[bad, ["lat", "lon"]] = np.nan
    if far:
        print(f"WARNING: {len(far)} boulder points are more than {C.BOULDER_FAR_KM} km from the "
              f"rest of their crag and are ignored (swapped x/y or a missing minus sign?): {far[:10]}")

    rows, missing = [], []
    for j, cs in sends.groupby("join", sort=False):
        pin = pins.loc[j] if j in pins.index else None
        pts = bl[(bl["join"] == j) & bl["lat"].notna()]
        nation = cs["nation"][cs["nation"] != ""]
        bnation = bl.loc[(bl["join"] == j) & (bl["nation"] != ""), "nation"]
        country = (pin["country"] if pin is not None and pin["country"] else
                   nation.mode().iloc[0] if len(nation) else
                   bnation.mode().iloc[0] if len(bnation) else "")
        row = dict(crag=pin["crag"] if pin is not None else cs["crag"].iloc[0],
                   state=pin["state"] if pin is not None else cs["state"].iloc[0],
                   country=country, join=j, points=None,
                   elev_m=pin["elev_m"] if pin is not None else np.nan,
                   radius_m=pin["radius_m"] if pin is not None else np.nan)
        if C.CRAG_POINT_FROM_BOULDERS and len(pts):
            row.update(lat=float(pts["lat"].mean()), lon=float(pts["lon"].mean()), pos="boulders",
                       points=sorted({(float(x), float(y)) for x, y in zip(pts["lon"], pts["lat"])}))
        elif pin is not None:
            row.update(lat=float(pin["lat"]), lon=float(pin["lon"]), pos="pin")
        else:
            missing.append((cs["crag"].iloc[0], cs["state"].iloc[0], len(cs)))
            continue
        if not country:
            print(f"WARNING: no Nation for {row['crag']} ({row['state']}); using ERA5-Land weather")
        rows.append(row)
    if missing:
        print(f"WARNING: no location for these crags (add a Crag Locations row or boulder x/y), "
              f"their sends are left off the map: {missing}")
    return pd.DataFrame(rows), bl


def resolve_places(sends, crags, bl):
    """Zones and boulders for the sends, each with a position and how it was found.
    Returns (zones, boulders, {send index: (zone_id, boulder_id)})."""
    crag_of = crags.set_index("join")
    key = lambda v: str(v).strip().casefold()
    tab_b = bl[bl["boulder"] != ""]
    full = {k: i for i, k in tab_b["bkey"].items()}
    by_name = tab_b.groupby("bkey2").groups

    # zone points: an explicit zone row (Boulder blank) wins, else the mean of its boulders
    zone_pin = bl[(bl["boulder"] == "") & bl["lat"].notna()].groupby("zkey")[["lat", "lon"]].first()
    zone_avg = tab_b[tab_b["lat"].notna()].groupby("zkey")[["lat", "lon"]].mean()

    zones, boulders, place, not_in_tab = {}, {}, {}, set()
    for s in sends.itertuples():
        if s.join not in crag_of.index:
            continue
        c = crag_of.loc[s.join]
        crag_id = c["id"]
        hit = None
        if s.boulder:
            hit = full.get(f"{s.join}|{key(s.zone)}|{key(s.boulder)}")
            if hit is None and len(by_name.get(f"{s.join}|{key(s.boulder)}", [])) == 1:
                hit = by_name[f"{s.join}|{key(s.boulder)}"][0]
            if hit is None:
                not_in_tab.add((s.crag, s.zone, s.boulder))
        zone = s.zone or (bl.loc[hit, "zone"] if hit is not None else "")

        zid = None
        if zone:
            zk = f"{s.join}|{key(zone)}"
            zid = slug(crag_id, zone)
            if zid not in zones:
                src = zone_pin if zk in zone_pin.index else zone_avg if zk in zone_avg.index else None
                lat, lon, pos = ((src.loc[zk, "lat"], src.loc[zk, "lon"], "gps") if src is not None
                                 else (c["lat"], c["lon"], "crag"))
                zones[zid] = dict(id=zid, crag_id=crag_id, name=zone,
                                  lat=round(float(lat), 6), lon=round(float(lon), 6), pos=pos)
        bid = None
        if s.boulder:
            bid = slug(crag_id, zone, s.boulder)
            if bid not in boulders:
                r = bl.loc[hit] if hit is not None else None
                if r is not None and pd.notna(r["lat"]):
                    lat, lon, pos = r["lat"], r["lon"], "gps"
                elif zid and zones[zid]["pos"] == "gps":
                    lat, lon, pos = zones[zid]["lat"], zones[zid]["lon"], "zone"
                else:
                    lat, lon, pos = c["lat"], c["lon"], "crag"
                boulders[bid] = dict(id=bid, crag_id=crag_id, zone_id=zid, zone=zone or None,
                                     subzone=(r["subzone"] or None) if r is not None else None,
                                     name=s.boulder, lat=round(float(lat), 6),
                                     lon=round(float(lon), 6), pos=pos)
        place[s.Index] = (zid, bid)

    if not_in_tab:
        print(f"Note: {len(not_in_tab)} boulders aren't in Boulder Locations yet (shown at their "
              f"zone or crag): {sorted(not_in_tab)[:12]}")
    gps = sum(b["pos"] == "gps" for b in boulders.values())
    print(f"Places: {len(zones)} zones, {len(boulders)} boulders ({gps} with coordinates)")
    return list(zones.values()), list(boulders.values()), place


def build(ticklist_raw, crags_raw, fetcher, out_path=C.OUT_PATH, boulders_raw=None):
    sends = sheet_io.clean_ticklist(ticklist_raw)
    bl = sheet_io.clean_boulders(boulders_raw)
    crags, bl = build_crags(sends, sheet_io.clean_crags(crags_raw), bl)
    crags["id"] = [slug(r.country, r.state, r.crag) for r in crags.itertuples()]
    crags["source"] = [pick_source(r.country, r.lon, r.lat) for r in crags.itertuples()]
    sends = sends.merge(crags[["join", "id", "country"]], on="join", how="left")

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
            lat=round(crag["lat"], 6), lon=round(crag["lon"], 6), pos=crag["pos"], source=crag["source"],
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

    zone_out, boulder_out, place = resolve_places(sends, crags, bl)

    # Placeholder dates (a whole trip logged on one day, or year-only Jan 1 entries) would give
    # every send that day's weather, so they're flagged and kept out of the conditions analysis.
    per_date = sends["date"].value_counts()
    approx = set(per_date[per_date > C.APPROX_DATE_MIN_SENDS].index)
    if C.APPROX_DATE_JAN1:
        approx |= {d for d in per_date.index if d.month == 1 and d.day == 1}
    if approx:
        n = int(sends["date"].isin(approx).sum())
        print(f"Approximate dates (left out of conditions analysis): {n} sends on "
              f"{sorted(d.strftime('%Y-%m-%d') for d in approx)}")

    send_out = []
    for s in sends.itertuples():
        sid = hashlib.md5(f"{s.date}|{s.name}|{s.crag}".encode()).hexdigest()[:10]
        send_out.append(dict(
            id=sid, date=None if pd.isna(s.date) else s.date.strftime("%Y-%m-%d"),
            date_approx=bool(pd.notna(s.date) and s.date in approx),
            name=s.name, grade=int(s.grade), stars=clean_value(s.stars, 1), style=s.style,
            is_fa=bool(s.is_fa),
            sessions=clean_value(s.sessions, 0), points=clean_value(s.points, 1),
            crag_id=None if pd.isna(s.id) else s.id, crag=s.crag, state=s.state,
            zone=s.zone or None, boulder=s.boulder or None,
            zone_id=place.get(s.Index, (None, None))[0], boulder_id=place.get(s.Index, (None, None))[1],
            country=None if pd.isna(s.country) else s.country,
            climate=climate.get(s.Index, dict(status="no_location"))))

    # One observation per climbing day (crag + date): five sends on one day are one day's weather.
    days = {}
    for s in send_out:
        if s["climate"].get("status") == "ok" and not s["date_approx"]:
            days[(s["crag_id"], s["date"])] = s["climate"]["pct"]
    summary = {}
    for k in C.VAR_KEYS:
        if not var_meta[k].get("pct", True):
            continue
        p = [pct[k] for pct in days.values() if pct.get(k) is not None]
        if p:
            summary[k] = dict(n_days=len(p), median_pct=round(float(np.median(p)), 1),
                              ks_p=physics.ks_uniform_p(p))

    doc = dict(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        settings=dict(baseline=[C.BASELINE_START, C.BASELINE_END], window_days=C.WINDOW_DAYS,
                      rain_threshold_mm=C.RAIN_THRESHOLD_MM,
                      lapse_c_per_km=dict(tmax=C.LAPSE_TMAX, tmin=C.LAPSE_TMIN)),
        variables=C.VARIABLES, crags=crag_out, zones=zone_out, boulders=boulder_out, sends=send_out,
        summary=summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    st = pd.Series([s["climate"]["status"] for s in send_out]).value_counts().to_dict()
    print(f"Wrote {out_path} — {len(send_out)} sends, {len(crag_out)} crags, climate: {st}")
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticklist-csv")
    ap.add_argument("--crags-csv")
    ap.add_argument("--boulders-csv")
    ap.add_argument("--fake-climate", action="store_true")
    ap.add_argument("--out", default=str(C.OUT_PATH))
    a = ap.parse_args()
    if a.ticklist_csv:
        t, c, bl = sheet_io.load_from_csv(a.ticklist_csv, a.crags_csv, a.boulders_csv)
    else:
        t, c, bl = sheet_io.load_from_sheet(os.environ["SHEET_ID"])
    from pathlib import Path
    out = Path(a.out)
    if a.fake_climate:      # keep made-up weather away from the real cache and website
        C.CACHE_DIR = C.ROOT / "test-output" / "cache"
        if a.out == str(C.OUT_PATH):
            out = C.ROOT / "test-output" / "sends.json"
        fetcher = FakeFetcher()
    else:
        fetcher = GEEFetcher()
    build(t, c, fetcher, out, bl)


if __name__ == "__main__":
    main()

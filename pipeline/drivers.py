"""What drives my sends: a used-vs-available analysis of send-day conditions.

    python -m pipeline.drivers            # after pipeline.build; writes docs/data/drivers.json

Each climbing day (crag + date) is compared with every other day at the same crag within
+/-WINDOW_DAYS of the same date, 2001-2025 ("available" days). A weighted conditional logistic
regression, one stratum per climbing day, asks which conditions separate send days from the
days that were available. Matching on crag and season removes where and when I chose to go.

Hard sends count more: each climbing day is weighted by its hardest send relative to my level
at the time (2^(grade - level), capped at 1). Results are reported as leave-one-year-out
concordance: how often the conditions pick the send day over a comparable day (0.5 = chance).

Crags are grouped into climate regions (Ward clustering of their 25-year monthly climate), and
each region with enough days gets its own results, shrunk toward the overall model.
"""
import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C
from . import physics
from .build import CragCache

OUT = C.ROOT / "docs" / "data" / "drivers.json"
SEND_DATA = C.OUT_PATH

# Candidate conditions: (key, label, unit, transform for modelling)
CANDIDATES = [
    ("tmax", "Max temperature", "°C", None),
    ("tmin", "Min temperature", "°C", None),
    ("dewpoint", "Dewpoint", "°C", None),
    ("dewpoint_spread", "Dewpoint spread", "°C", None),
    ("rmin", "Afternoon humidity", "%", None),
    ("rmax", "Overnight humidity", "%", None),
    ("vpd", "Vapor pressure deficit", "kPa", None),
    ("sph", "Specific humidity", "g/kg", None),
    ("srad", "Solar radiation", "W/m²", None),
    ("wind", "Wind speed", "m/s", None),
    ("pr", "Rain on the day", "mm", "log1p"),
    ("rain3", "Rain in the 3 days before", "mm", "log1p"),
    ("days_since_rain", "Days since rain", "days", "log1p"),
]
LEVEL_WINDOW_DAYS = 730      # "my level" = 90th percentile grade over the previous two years
LEVEL_QUANTILE = 0.9
MIN_WEIGHT = 1 / 64          # six grades below my level
MIN_AREA_DAYS = 25           # a climate region needs this many climbing days for its own results
MIN_AREA_NEFF = 8
MAX_REGIONS = 8
AREA_SHRINK = 4.0            # ridge pull of a region's model toward the overall model
MODEL_MAX_VARS = 5
MODEL_MAX_CORR = 0.7
N_BOOT = 1000
RNG = np.random.default_rng(42)


# ----------------------------------------------------------------------------- data
def load(send_path=SEND_DATA, cache_dir=None):
    d = json.loads(send_path.read_text(encoding="utf-8"))
    crags = {c["id"]: c for c in d["crags"]}
    series = {}
    for cid, c in crags.items():
        cache = CragCache(cid) if cache_dir is None else _cache_at(cache_dir, cid)
        meta, daily = cache.load()
        if daily.empty or "z_grid" not in meta:
            continue
        s = physics.correct_to_crag(daily, meta["z_grid"], meta["z_crag"])
        pr = s["pr"].asfreq("D")
        s["rain3"] = pr.shift(1).rolling(3, min_periods=1).sum().reindex(s.index)
        s["days_since_rain"] = s["days_since_rain"].clip(upper=60)
        series[cid] = s
    sends = pd.DataFrame(d["sends"])
    return d, crags, series, sends


def _cache_at(cache_dir, cid):
    c = CragCache(cid)
    c.dir = cache_dir / cid
    c.meta_path, c.daily_path = c.dir / "meta.json", c.dir / "daily.csv.gz"
    return c


def climbing_days(sends):
    """One row per crag + date with weather, weighted by the hardest send that day."""
    s = sends[sends["climate"].map(lambda c: c.get("status") == "ok")].copy()
    s = s[~s["date_approx"].astype(bool)]
    s["t"] = pd.to_datetime(s["date"])
    s["g"] = pd.to_numeric(s["grade"], errors="coerce")
    s = s.dropna(subset=["g", "t"]).sort_values("t")

    # my level at each date: 90th percentile of grades sent in the previous two years
    all_sends = sends.copy()
    all_sends["t"] = pd.to_datetime(all_sends["date"], errors="coerce")
    all_sends["g"] = pd.to_numeric(all_sends["grade"], errors="coerce")
    all_sends = all_sends.dropna(subset=["t", "g"]).sort_values("t")
    level = []
    for t in s["t"]:
        prior = all_sends[(all_sends["t"] <= t) & (all_sends["t"] > t - pd.Timedelta(days=LEVEL_WINDOW_DAYS))]["g"]
        if len(prior) < 8:
            prior = all_sends[all_sends["t"] <= t]["g"]
        level.append(prior.quantile(LEVEL_QUANTILE) if len(prior) else s["g"].max())
    s["level"] = level
    s["w"] = np.clip(2.0 ** (s["g"] - s["level"]), MIN_WEIGHT, 1.0)
    days = (s.groupby(["crag_id", "t"])
              .agg(w=("w", "max"), grade=("g", "max"), level=("level", "first"), n=("name", "size"))
              .reset_index())
    days["year"] = days["t"].dt.year
    return days


# ----------------------------------------------------------------------------- strata
def build_strata(days, series, keys):
    """Case rows, padded control rows and a mask, all in raw units."""
    cases, ctrls, keep = [], [], []
    for i, r in days.iterrows():
        s = series.get(r["crag_id"])
        if s is None or r["t"] not in s.index:
            continue
        m = physics.baseline_mask(s.index, r["t"]) & (s.index != r["t"])
        cases.append(s.loc[r["t"], keys].to_numpy(float))
        ctrls.append(s.loc[m, keys].to_numpy(float))
        keep.append(i)
    M = max(len(c) for c in ctrls)
    X = np.full((len(ctrls), M, len(keys)), np.nan)
    for j, c in enumerate(ctrls):
        X[j, :len(c)] = c
    return np.array(cases), X, days.loc[keep].reset_index(drop=True)


def transform(v, how):
    return np.log1p(np.clip(v, 0, None)) if how == "log1p" else v


# ----------------------------------------------------------------------------- model
def design(z, quad=True):
    """Standardized values -> features (linear + quadratic)."""
    return np.concatenate([z, z ** 2], axis=-1) if quad else z


def fit_clogit(xc, Xs, mask, w, beta0=None, lam=0.0, prior=None, iters=50):
    """Weighted conditional logit by Newton-Raphson with an optional ridge toward `prior`."""
    S, M, F = Xs.shape
    beta = np.zeros(F) if beta0 is None else beta0.copy()
    prior = np.zeros(F) if prior is None else prior
    allx = np.concatenate([xc[:, None, :], Xs], axis=1)
    allm = np.concatenate([np.ones((S, 1), bool), mask], axis=1)
    allx = np.where(allm[..., None], allx, 0.0)
    for _ in range(iters):
        eta = allx @ beta
        eta = np.where(allm, eta, -np.inf)
        eta -= eta.max(axis=1, keepdims=True)
        p = np.exp(eta)
        p /= p.sum(axis=1, keepdims=True)
        xbar = np.einsum("sm,smf->sf", p, allx)
        grad = (w[:, None] * (xc - xbar)).sum(0) - lam * (beta - prior)
        cov = np.einsum("sm,smf,smg->sfg", p, allx, allx) - np.einsum("sf,sg->sfg", xbar, xbar)
        H = -(w[:, None, None] * cov).sum(0) - lam * np.eye(F)
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            break
        beta -= step
        if np.abs(step).max() < 1e-7:
            break
    return beta


def concordance(xc, Xs, mask, beta):
    """Per stratum: share of available days the send day out-scores (ties count half)."""
    sc, sx = xc @ beta, Xs @ beta
    gt = ((sc[:, None] > sx) & mask).sum(1) + 0.5 * ((np.isclose(sc[:, None], sx)) & mask).sum(1)
    return gt / mask.sum(1)


def cv_concordance(xc, Xs, mask, w, years, lam=0.0, prior=None):
    """Leave-one-year-out concordance per stratum (NaN if a year can't be held out)."""
    out = np.full(len(xc), np.nan)
    for y in np.unique(years):
        test, train = years == y, years != y
        if train.sum() < 5:
            continue
        b = fit_clogit(xc[train], Xs[train], mask[train], w[train], lam=lam, prior=prior)
        out[test] = concordance(xc[test], Xs[test], mask[test], b)
    return out


def summarize_c(c, w, years):
    """Weighted mean concordance with a year-clustered bootstrap 90% interval."""
    ok = ~np.isnan(c)
    c, w, years = c[ok], w[ok], years[ok]
    if len(c) < 5:
        return None
    est = float(np.average(c, weights=w))
    uy = np.unique(years)
    idx = {y: np.where(years == y)[0] for y in uy}
    boots = []
    for _ in range(N_BOOT):
        pick = np.concatenate([idx[y] for y in RNG.choice(uy, len(uy))])
        boots.append(np.average(c[pick], weights=w[pick]))
    lo, hi = np.percentile(boots, [5, 95])
    return dict(c=round(est, 3), lo=round(float(lo), 3), hi=round(float(hi), 3),
                sig=bool(lo > 0.5))


def wmedian(v, w):
    o = np.argsort(v)
    cw = np.cumsum(w[o])
    return float(v[o][np.searchsorted(cw, cw[-1] / 2)])


# ----------------------------------------------------------------------------- one variable
class Var:
    def __init__(self, key, label, unit, how):
        self.key, self.label, self.unit, self.how = key, label, unit, how


def analyse_var(v, cases, X, days, weighted=True, prior=None, lam=0.0, curve=True):
    """Concordance, direction and sweet-spot curve for one condition in one set of strata."""
    j = KEYS.index(v.key)
    xc = transform(cases[:, j], v.how)
    xs = transform(X[:, :, j], v.how)
    ok = ~np.isnan(xc)
    xc, xs = xc[ok], xs[ok]
    mask = ~np.isnan(xs)
    w = days["w"].to_numpy()[ok] if weighted else np.ones(ok.sum())
    years = days["year"].to_numpy()[ok]
    if ok.sum() < 5:
        return None
    mu, sd = np.nanmean(xs), np.nanstd(xs) or 1.0
    zc = design(((xc - mu) / sd)[:, None])
    zs = design(np.where(mask, (xs - mu) / sd, 0.0)[..., None])
    c = cv_concordance(zc, zs, mask, w, years, lam=lam, prior=prior)
    res = summarize_c(c, w, years)
    if res is None:
        return None
    # direction: where send days sit within their own crag + season (percentile)
    pct = ((xs < xc[:, None]) & mask).sum(1) / mask.sum(1) * 100
    res["median_pct"] = round(wmedian(pct, w), 1)
    res["direction"] = "lower" if res["median_pct"] < 50 else "higher"
    beta = fit_clogit(zc, zs, mask, w, lam=lam, prior=prior)
    res["beta"] = [round(float(b), 4) for b in beta]
    res["mu"], res["sd"] = round(float(mu), 4), round(float(sd), 4)
    if curve:
        res.update(sweet_curve(v, beta, mu, sd, xs[mask], xc, w))
    return res


def sweet_curve(v, beta, mu, sd, avail, xc, w):
    """Relative likelihood of a send across the typical range, plus both distributions."""
    lo, hi = np.percentile(avail, [2, 98])
    if hi <= lo:
        hi = lo + 1
    grid = np.linspace(lo, hi, 41)
    z = (grid - mu) / sd
    eta = beta[0] * z + beta[1] * z ** 2
    rel = np.exp(eta - eta.max())
    good = rel >= 0.8
    peak = int(np.argmax(rel))
    a = peak
    while a > 0 and good[a - 1]:
        a -= 1
    b = peak
    while b < len(grid) - 1 and good[b + 1]:
        b += 1
    back = (lambda x: np.expm1(x)) if v.how == "log1p" else (lambda x: x)
    bins = np.linspace(lo, hi, 21)
    ha, _ = np.histogram(np.clip(avail, lo, hi), bins)
    hs, _ = np.histogram(np.clip(xc, lo, hi), bins, weights=w)
    return dict(
        curve=[[round(float(back(x)), 3), round(float(r), 4)] for x, r in zip(grid, rel)],
        sweet=[round(float(back(grid[a])), 2), round(float(back(grid[b])), 2)],
        sweet_open=["low" if a == 0 else None, "high" if b == len(grid) - 1 else None],
        hist=dict(edges=[round(float(back(e)), 3) for e in bins],
                  avail=[round(float(x), 4) for x in ha / max(ha.sum(), 1)],
                  sends=[round(float(x), 4) for x in hs / max(hs.sum(), 1e-9)]),
    )


# ----------------------------------------------------------------------------- combined model
def combined_model(ranked, cases, X, days, prior=None, lam=1.0, label="all"):
    """Linear + quadratic conditional logit on a few distinct conditions (for the scoreboard)."""
    chosen = []
    zs_all = {}
    for v, r in ranked:
        if not r or not r["sig"]:
            continue
        j = KEYS.index(v.key)
        xs = transform(X[:, :, j], v.how)
        z = (xs - np.nanmean(xs)) / (np.nanstd(xs) or 1)
        zs_all[v.key] = z - np.nanmean(z, axis=1, keepdims=True)    # within-stratum variation
        redundant = any(abs(_corr(zs_all[v.key], zs_all[u.key])) > MODEL_MAX_CORR for u, _ in chosen)
        if not redundant:
            chosen.append((v, r))
        if len(chosen) == MODEL_MAX_VARS:
            break
    if not chosen:
        return None
    idx = [KEYS.index(v.key) for v, _ in chosen]
    xc = np.column_stack([transform(cases[:, j], v.how) for j, (v, _) in zip(idx, chosen)])
    xs = np.stack([transform(X[:, :, j], v.how) for j, (v, _) in zip(idx, chosen)], axis=-1)
    ok = ~np.isnan(xc).any(1)
    xc, xs = xc[ok], xs[ok]
    mask = ~np.isnan(xs).any(-1)
    mu, sd = np.nanmean(xs.reshape(-1, xs.shape[-1]), 0), np.nanstd(xs.reshape(-1, xs.shape[-1]), 0)
    sd[sd == 0] = 1
    zc = design((xc - mu) / sd)
    zs = design(np.where(mask[..., None], (xs - mu) / sd, 0.0))
    w, years = days["w"].to_numpy()[ok], days["year"].to_numpy()[ok]
    pr = None if prior is None else np.array(prior)
    c = cv_concordance(zc, zs, mask, w, years, lam=lam, prior=pr)
    beta = fit_clogit(zc, zs, mask, w, lam=lam, prior=pr)
    return dict(vars=[v.key for v, _ in chosen], how=[v.how for v, _ in chosen],
                mu=[round(float(x), 4) for x in mu], sd=[round(float(x), 4) for x in sd],
                beta=[round(float(b), 4) for b in beta], cv=summarize_c(c, w, years))


def _corr(a, b):
    m = ~np.isnan(a) & ~np.isnan(b)
    return np.corrcoef(a[m], b[m])[0, 1] if m.sum() > 10 else 0.0


# ----------------------------------------------------------------------------- regions
def climate_regions(crags, series, days):
    """Ward clustering of each crag's 25-year monthly climate; the most clusters that still
    leave every region with enough climbing days (small leftovers are reported, not analysed)."""
    from scipy.cluster.hierarchy import fcluster, linkage
    used = days.groupby("crag_id").size()
    ids = [c for c in used.index if c in series]
    feats = []
    for cid in ids:
        s = series[cid]
        s = s[(s.index.year >= C.BASELINE_START) & (s.index.year <= C.BASELINE_END)]
        m = s.groupby(s.index.month)
        feats.append(np.concatenate([m["tmax"].mean(), m["dewpoint"].mean(), np.log1p(m["pr"].mean() * 30)]))
    F = np.array(feats)
    F = (F - F.mean(0)) / (F.std(0) + 1e-9)
    if len(ids) < 3:
        return {cid: "r1" for cid in ids}, {}
    Z = linkage(F, "ward")
    best = None
    for k in range(min(MAX_REGIONS, len(ids)), 0, -1):
        lab = fcluster(Z, k, "maxclust")
        n = pd.Series(used[ids].values, index=lab).groupby(level=0).sum()
        big = n[n >= MIN_AREA_DAYS]
        if big.sum() >= 0.85 * used.sum() or k == 1:
            best = lab
            break
    region = {}
    info = {}
    for lab in np.unique(best):
        members = [cid for cid, l in zip(ids, best) if l == lab]
        members.sort(key=lambda c: -used[c])
        rid = "r" + str(len(info) + 1)
        for cid in members:
            region[cid] = rid
        info[rid] = members
    return region, info


def describe_region(members, crags, series, used):
    top = [crags[c]["name"] for c in members[:2]]
    name = top[0] + (f" & {top[1]}" if len(top) > 1 else "")
    allz = [crags[c]["z_crag"] for c in members]
    s = pd.concat([series[c] for c in members])
    s = s[(s.index.year >= C.BASELINE_START) & (s.index.year <= C.BASELINE_END)]
    jul = s[s.index.month == 7]["tmax"].mean()
    jan = s[s.index.month == 1]["tmax"].mean()
    return dict(name=name, crags=[crags[c]["name"] for c in members],
                states=sorted({crags[c]["state"] for c in members}),
                days=int(sum(used.get(c, 0) for c in members)),
                elev_m=[round(min(allz)), round(max(allz))],
                jan_tmax=round(float(jan), 1), jul_tmax=round(float(jul), 1))


# ----------------------------------------------------------------------------- main
KEYS = [k for k, *_ in CANDIDATES]
VARS = [Var(*c) for c in CANDIDATES]


def run(send_path=SEND_DATA, out_path=OUT, cache_dir=None):
    d, crags, series, sends = load(send_path, cache_dir)
    days = climbing_days(sends)
    days = days[days["crag_id"].isin(series)].reset_index(drop=True)
    cases, X, days = build_strata(days, series, KEYS)
    w = days["w"].to_numpy()
    print(f"Drivers: {len(days)} climbing days, effective n (hard-weighted) "
          f"{w.sum() ** 2 / (w ** 2).sum():.0f}")

    overall = []
    for v in VARS:
        r = analyse_var(v, cases, X, days, weighted=True)
        ru = analyse_var(v, cases, X, days, weighted=False, curve=False)
        if r:
            r["all_sends"] = {k: ru[k] for k in ("c", "lo", "hi", "sig", "median_pct")} if ru else None
        overall.append((v, r))
    overall.sort(key=lambda vr: -(vr[1]["c"] if vr[1] else 0))
    model = combined_model(overall, cases, X, days)

    region_of, members = climate_regions(crags, series, days)
    used = days.groupby("crag_id").size()
    regions = []
    for rid, mem in members.items():
        sel = days["crag_id"].isin(mem).to_numpy()
        desc = describe_region(mem, crags, series, used)
        wr = w[sel]
        neff = float(wr.sum() ** 2 / (wr ** 2).sum()) if sel.any() else 0
        entry = dict(id=rid, **desc, n_eff=round(neff, 1),
                     enough=bool(sel.sum() >= MIN_AREA_DAYS and neff >= MIN_AREA_NEFF), vars={})
        if entry["enough"]:
            dsub = days[sel].reset_index(drop=True)
            for v, g in overall:
                if not g:
                    continue
                # shrink the region's curve toward the overall one (ridge on the difference)
                r = analyse_var(v, cases[sel], X[sel], dsub, weighted=True,
                                prior=np.array(g["beta"]), lam=AREA_SHRINK)
                if r:
                    entry["vars"][v.key] = r
            ranked = sorted(((v, entry["vars"].get(v.key)) for v, _ in overall),
                            key=lambda vr: -(vr[1]["c"] if vr[1] else 0))
            entry["model"] = combined_model(ranked, cases[sel], X[sel], dsub)
        regions.append(entry)
    regions.sort(key=lambda r: -r["days"])

    out = dict(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        method=dict(window_days=C.WINDOW_DAYS, baseline=[C.BASELINE_START, C.BASELINE_END],
                    level_window_days=LEVEL_WINDOW_DAYS, level_quantile=LEVEL_QUANTILE,
                    min_area_days=MIN_AREA_DAYS, n_boot=N_BOOT),
        n_days=int(len(days)), n_eff=round(float(w.sum() ** 2 / (w ** 2).sum()), 1),
        n_sends=int(days["n"].sum()), years=[int(days["year"].min()), int(days["year"].max())],
        variables=[dict(key=v.key, label=v.label, unit=v.unit, log=v.how == "log1p") for v in VARS],
        overall=[dict(key=v.key, **r) for v, r in overall if r],
        model=model,
        regions=regions,
        crag_region={cid: region_of.get(cid) for cid in crags},
    )
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {out_path}")
    return out


if __name__ == "__main__":
    run()

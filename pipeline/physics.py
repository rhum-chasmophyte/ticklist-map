"""Lapse-rate correction, humidity recomputation, derived variables, percentiles."""
import math

import numpy as np
import pandas as pd

from . import config as C


# --- Moisture helpers (T in deg C, pressure and vapor pressure in kPa) ----
def es(t):
    """Saturation vapor pressure (Tetens)."""
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def pressure_kpa(z_m):
    """Standard-atmosphere surface pressure at elevation z."""
    return 101.325 * (1 - 2.25577e-5 * z_m) ** 5.25588


def e_from_q(q, p):
    return q * p / (0.622 + 0.378 * q)


def q_from_e(e, p):
    return 0.622 * e / (p - 0.378 * e)


def td_from_e(e):
    a = np.log(e / 0.6108)
    return 237.3 * a / (17.27 - a)


# --- Correction ------------------------------------------------------------
def correct_to_crag(daily: pd.DataFrame, z_grid: float, z_crag: float) -> pd.DataFrame:
    """Return the crag-level daily series.

    Input columns (standardized by gee_fetch): tmax_c, tmin_c, and either
    sph (gridMET) or td_c (ERA5-Land), plus the pass-through variables.
    Temperature is lapse-corrected; specific humidity is held fixed and RH,
    VPD and dewpoint are recomputed from it at the crag's pressure.
    """
    d = daily.copy()
    dz_km = (z_grid - z_crag) / 1000.0
    out = pd.DataFrame(index=d.index)
    out["tmax_grid"] = d["tmax_c"]
    out["tmin_grid"] = d["tmin_c"]
    tmax = d["tmax_c"] + C.LAPSE_TMAX * dz_km
    tmin = d["tmin_c"] + C.LAPSE_TMIN * dz_km
    tmin = np.minimum(tmin, tmax)
    out["tmax"], out["tmin"] = tmax, tmin

    p_grid, p_crag = pressure_kpa(z_grid), pressure_kpa(z_crag)
    if "sph" in d and d["sph"].notna().any():
        q = d["sph"]
    else:
        q = q_from_e(es(d["td_c"]), p_grid)
    e = e_from_q(q, p_crag)
    e = np.minimum(e, es(tmin))          # can't exceed saturation at the daily low
    out["sph"] = q_from_e(e, p_crag) * 1000.0          # g/kg
    out["rmax"] = 100 * e / es(tmin)
    out["rmin"] = 100 * e / es(tmax)
    out["vpd"] = np.maximum(0, (es(tmax) + es(tmin)) / 2 - e)
    out["dewpoint"] = td_from_e(e)
    out["dewpoint_spread"] = tmax - out["dewpoint"]

    for col, src in [("pr", "pr_mm"), ("srad", "srad_wm2"), ("wind", "wind_ms"),
                     ("wind_dir", "wind_dir"), ("pet", "pet_mm"), ("etr", "etr_mm"),
                     ("erc", "erc"), ("bi", "bi"), ("fm100", "fm100"), ("fm1000", "fm1000")]:
        out[col] = d[src] if src in d else np.nan

    out["days_since_rain"] = days_since_rain(out["pr"])
    return out


def days_since_rain(pr: pd.Series) -> pd.Series:
    """Days since the last day with pr > threshold (0 = it rained that day).
    Index must be a daily DatetimeIndex."""
    original = pr.index
    full = pr.asfreq("D")          # fills any missing days (ERA5 has the odd gap) with NaN
    wet_dates = pd.Series(full.index.where(full > C.RAIN_THRESHOLD_MM), index=full.index)
    last_wet = wet_dates.ffill()
    dsr = pd.Series((full.index - pd.DatetimeIndex(last_wet)).days.astype("float"),
                    index=full.index)
    return dsr.reindex(original)   # back to the same dates as the input


# --- Baseline percentiles ---------------------------------------------------
def baseline_mask(index: pd.DatetimeIndex, when: pd.Timestamp) -> np.ndarray:
    in_years = (index.year >= C.BASELINE_START) & (index.year <= C.BASELINE_END)
    diff = np.abs(index.dayofyear - when.dayofyear)
    diff = np.minimum(diff, 366 - diff)
    return in_years & (diff <= C.WINDOW_DAYS)


def percentile_of(values: np.ndarray, x: float):
    v = values[~np.isnan(values)]
    if len(v) == 0 or x is None or np.isnan(x):
        return None
    below = (v < x).sum()
    equal = (v == x).sum()
    return round(100.0 * (below + 0.5 * equal) / len(v), 1)


def ks_uniform_p(pcts):
    """Kolmogorov-Smirnov test of percentiles against Uniform(0, 100).
    Small p = send days are not a random draw from that season's days."""
    x = np.sort(np.asarray(pcts, float) / 100.0)
    n = len(x)
    if n < 5:
        return None
    i = np.arange(1, n + 1)
    d = max((i / n - x).max(), (x - (i - 1) / n).max())
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d
    p = 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam) for k in range(1, 101))
    return round(min(max(p, 0.0), 1.0), 4)

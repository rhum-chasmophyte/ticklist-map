"""Settings for the ticklist climate pipeline. Edit here, not in the code."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "cache"                     # per-crag daily climate series (committed)
OUT_PATH = ROOT / "docs" / "data" / "sends.json"  # what the web page loads

# --- Google ---------------------------------------------------------------
GEE_PROJECT = os.environ.get("GEE_PROJECT", "ee-richiehum")
TICKLIST_TAB = "Ticklist"
CRAGS_TAB = "Crag Locations"

# --- Baseline: same crag, same time of year -------------------------------
BASELINE_START = 2001
BASELINE_END = 2025
WINDOW_DAYS = 15            # +/- days around the send's day of year

# --- Derived variables ----------------------------------------------------
RAIN_THRESHOLD_MM = 2.5     # a "meaningful rain" day

# --- Lapse-rate correction (deg C per km) ---------------------------------
LAPSE_TMAX = 6.5
LAPSE_TMIN = 4.0            # weaker: overnight lows pool in valleys

# --- Elevation ------------------------------------------------------------
# First one that exists is used. GLO30 was superseded by GLO30_2024_1 in 2026.
DEM_ASSETS = ["COPERNICUS/DEM/GLO30_2024_1", "COPERNICUS/DEM/GLO30"]
DEM_SCALE_CELL = 90         # m, for averaging over a whole grid cell
DEM_SCALE_CRAG = 30         # m, for the crag itself

# gridMET covers the contiguous US only; anything else uses ERA5-Land
CONUS_BBOX = (-125.0, 24.4, -66.8, 49.5)   # W, S, E, N

# --- Variables exposed to the web page -----------------------------------
# pct=False: no percentile (circular quantity)
VARIABLES = [
    dict(key="tmax", label="Max temperature", unit="°C", group="Temperature"),
    dict(key="tmin", label="Min temperature", unit="°C", group="Temperature"),
    dict(key="rmax", label="Max relative humidity", unit="%", group="Moisture"),
    dict(key="rmin", label="Min relative humidity", unit="%", group="Moisture"),
    dict(key="sph", label="Specific humidity", unit="g/kg", group="Moisture"),
    dict(key="vpd", label="Vapor pressure deficit", unit="kPa", group="Moisture"),
    dict(key="dewpoint", label="Dewpoint", unit="°C", group="Moisture"),
    dict(key="dewpoint_spread", label="Dewpoint spread (Tmax − Td)", unit="°C", group="Moisture"),
    dict(key="pr", label="Precipitation", unit="mm", group="Precipitation"),
    dict(key="days_since_rain", label="Days since rain (>2.5 mm)", unit="days", group="Precipitation"),
    dict(key="srad", label="Solar radiation", unit="W/m²", group="Energy"),
    dict(key="pet", label="Reference ET", unit="mm", group="Energy",
         note="ERA5-Land value is an approximation (different method)"),
    dict(key="etr", label="Alfalfa reference ET", unit="mm", group="Energy", gridmet_only=True),
    dict(key="wind", label="Wind speed", unit="m/s", group="Wind",
         note="ERA5-Land derived from daily-mean components; understates gusty days"),
    dict(key="wind_dir", label="Wind direction", unit="°", group="Wind", pct=False),
    dict(key="erc", label="Energy release component", unit="", group="Fire danger", gridmet_only=True),
    dict(key="bi", label="Burning index", unit="", group="Fire danger", gridmet_only=True),
    dict(key="fm100", label="100-hr fuel moisture", unit="%", group="Fire danger", gridmet_only=True),
    dict(key="fm1000", label="1000-hr fuel moisture", unit="%", group="Fire danger", gridmet_only=True),
]
VAR_KEYS = [v["key"] for v in VARIABLES]

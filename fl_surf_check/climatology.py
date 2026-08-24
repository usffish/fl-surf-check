"""
The historical baseline: what does a normal surf day look like in FLORIDA?

WHY THIS EXISTS
---------------
The curves in scoring.py answer "is this rideable?" on an absolute scale. They
cannot answer the question you actually care about, which is "is today one of
the good ones?" A 2.5ft/11s day is unremarkable in Hawaii and a red-letter day
here. Only the historical record can tell you which you are looking at.

ONE STATEWIDE BASELINE, NOT ONE PER SPOT
----------------------------------------
Every spot is ranked against a single pooled Florida-wide distribution, not
against its own private history. This mirrors the movie-score engine, where
min and max are computed "across the entire batch after all fetches complete -
not per-movie".

It also matters for what the tool is *for*. Per-spot baselines grade each break
on its own curve, which flatters consistently weak spots: a mediocre day at
Pensacola would score the same "p90" as a genuinely excellent day at Sebastian
Inlet, because each is only compared to itself. Since the whole point is
deciding which spot to drive to, they have to be measured against one shared
yardstick. A spot that is simply better should read as better.

NOT SEASONAL - DELIBERATELY
----------------------------
An earlier version windowed the baseline to +/-14 days around the date being
scored, so a February day was judged against other Februaries and an August
day against other Augusts. That was removed on request: the baseline now
pools every day in the full record, regardless of time of year.

The tradeoff is real and worth stating plainly, because it changes what
"normal" means. Measured on the actual record: the seasonal (late-August)
window put the statewide median at 1.25ft and the 90th percentile at 2.95ft.
Pooling all five years instead moves those to 1.51ft and 3.54ft, because
winter nor'easters run bigger than summer trade-wind swell and now pull the
whole distribution up. The practical effect: a typical AUGUST day will read
as somewhat below normal against a baseline that also contains every winter
storm on record, where a seasonal baseline would have called it ordinary. What
you get in exchange is a single, simpler question - "how does this compare to
Florida surf overall" - rather than one that shifts meaning with the calendar.

DATA SOURCE AND ITS LIMITS
--------------------------
Open-Meteo's marine endpoint serves history through the same URL as the
forecast, via start_date/end_date. Three limits, all measured rather than
assumed:

  - History begins 2021-10-01. There is no marine data before that; the
    archive endpoint (archive-api.open-meteo.com) returns all-NaN for wave
    variables, carrying atmospheric reanalysis only. So the baseline rests on
    ~5 years of days, not decades. That thinness is why the rarity percentile
    is shrunk toward normal - see scoring.shrink_percentile - though pooling
    the full record rather than a 29-day window means there is now far more
    evidence behind it, and shrinkage has correspondingly less to do.

  - The full pull is now several million samples across 41 spots. It takes a
    few seconds, and running it repeatedly WILL trip Open-Meteo's per-minute
    rate limit - which then starves the live conditions request in the same
    run, silently dropping every spot to its no-data floor. It must be
    fetched once and cached. Hence the 30-day disk cache below.

  - Requesting 6 hourly variables returns them in the order requested
    (verified against the flatbuffers Variable enum), so positional reads are
    safe, but they are read by index and stay coupled to HISTORY_VARS.

WHY DAILY MAXIMA, NOT HOURLY SAMPLES
-------------------------------------
The full record holds tens of thousands of hourly samples per spot, but those
are nowhere near independent - a single 3-day swell contributes 72 highly
correlated hours. Each day is therefore reduced to its maximum before pooling,
which both matches the question ("was that a good surf *day*") and stops
autocorrelation from inflating the apparent evidence.

The same caution applies across spots: all 41 see broadly the same weather
systems, so 41 spot-days on one date are not 41 independent observations. The
pooled distribution uses every spot-day, but the evidence count that drives
shrinkage is the number of distinct DAYS - the conservative choice.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass

import numpy as np

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

# Measured, not guessed: the first hour of real marine data Open-Meteo serves.
HISTORY_START = "2021-10-01"

# Deliberately the SWELL partition, not the total sea state - the total mixes
# in local wind chop, which is not rideable. See conditions.Conditions.
HISTORY_VARS = ("swell_wave_height", "swell_wave_period")

DEFAULT_CACHE_PATH = ".fl_surf_climatology.json"
CACHE_MAX_AGE_DAYS = 30

# Sun elevation (degrees) above which an hour counts as surfable light.
# -6 is civil twilight: the sun is below the horizon but there is enough light
# to see a set coming, which is exactly when dawn patrol happens. Using 0
# (true sunrise) would throw away the best hour of many days - the calmest,
# most offshore wind in Florida is right at first light.
DAYLIGHT_ELEVATION_DEG = -6.0

_LEVELS = tuple(range(0, 101))


@dataclass(frozen=True)
class Baseline:
    """
    The pooled Florida-wide distribution of surf, as daily maxima across the
    full historical record - not windowed to any particular time of year.

    `n_days` counts distinct calendar days (the independent unit), while
    `n_observations` counts spot-days actually pooled. Shrinkage uses the
    former; the latter is reported so the sample size is visible.
    """
    height_p: tuple[float, ...]   # percentiles of daily-max swell height, ft
    period_p: tuple[float, ...]   # percentiles of daily-max swell period, s
    n_days: int
    n_years: int
    n_observations: int = 0
    n_spots: int = 0
    # Mean and SD of log(value), i.e. a GEOMETRIC standard deviation.
    #
    # Measured on the real pooled record, raw swell height has a skew of 1.95 -
    # strongly right-tailed, because a handful of tropical systems stretch the
    # upper end. A raw z-score on that is badly behaved: the mean sits at the
    # 64th percentile rather than the 50th, and the largest day on record lands
    # at z+5.9. Taking logs first drops the skew to 0.27 and makes z mean what
    # people expect it to mean - measured +1 SD = p84.8 against a textbook
    # p84.1, +2 SD = p96.4, +3 SD = p99.9.
    log_height_mean: float = 0.0
    log_height_sd: float = 0.0
    log_period_mean: float = 0.0
    log_period_sd: float = 0.0

    LEVELS = _LEVELS

    def height_sigma(self, value: float | None) -> float | None:
        """How many geometric SDs above (or below) normal a swell height is."""
        return _log_z(value, self.log_height_mean, self.log_height_sd)

    def period_sigma(self, value: float | None) -> float | None:
        """How many geometric SDs above (or below) normal a swell period is."""
        return _log_z(value, self.log_period_mean, self.log_period_sd)

    def height_percentile(self, value: float | None) -> float | None:
        """Where a swell height sits in the statewide historical record, 0-100."""
        return _percentile_of(value, self.height_p)

    def period_percentile(self, value: float | None) -> float | None:
        """Where a swell period sits in the statewide historical record, 0-100."""
        return _percentile_of(value, self.period_p)

    def summary(self) -> str:
        return (
            f"FL normal {self.height_p[50]:.1f}ft/{self.period_p[50]:.0f}s, "
            f"good {self.height_p[90]:.1f}ft/{self.period_p[90]:.0f}s "
            f"({self.n_days}d over {self.n_years}yr, {self.n_spots} spots)"
        )


def _log_z(value: float | None, log_mean: float, log_sd: float) -> float | None:
    """
    Standard deviations above normal, measured in log space.

    Returns None when the value or the baseline cannot support the calculation.
    Non-positive values have no logarithm; they are also flatter than anything
    the record contains, so they floor rather than error.
    """
    if value is None or log_sd <= 0.0:
        return None
    if value <= 0.0:
        return -3.0
    return (float(np.log(value)) - log_mean) / log_sd


def _percentile_of(value: float | None, curve: tuple[float, ...]) -> float | None:
    """
    Invert a percentile curve: given a value, return its percentile rank.

    `curve` holds the value at each whole percentile 0..100, so this is a
    search for where `value` falls, interpolating between levels.
    """
    if value is None or not curve:
        return None
    arr = np.asarray(curve, dtype=float)
    if value <= arr[0]:
        return 0.0
    if value >= arr[-1]:
        return 100.0
    idx = int(np.searchsorted(arr, value, side="right")) - 1
    idx = max(0, min(len(arr) - 2, idx))
    lo, hi = arr[idx], arr[idx + 1]
    frac = 0.0 if hi == lo else (value - lo) / (hi - lo)
    return float(idx + frac)


def _solar_elevation(unix_times: np.ndarray, lat: float, lon: float) -> np.ndarray:
    """
    Sun elevation in degrees for each UTC timestamp, via the standard NOAA
    approximation. Accurate to a fraction of a degree, which is far more than
    this needs, and avoids taking on an astronomy dependency.
    """
    days = unix_times / 86400.0
    doy = (days % 365.2422)
    hours = (unix_times % 86400) / 3600.0

    gamma = 2.0 * np.pi / 365.0 * (doy + (hours - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma) + 0.001480 * np.sin(3 * gamma)
    )

    true_solar = hours * 60.0 + eqtime + 4.0 * lon
    hour_angle = np.radians(true_solar / 4.0 - 180.0)
    latr = np.radians(lat)
    cos_zenith = (
        np.sin(latr) * np.sin(decl)
        + np.cos(latr) * np.cos(decl) * np.cos(hour_angle)
    )
    return 90.0 - np.degrees(np.arccos(np.clip(cos_zenith, -1.0, 1.0)))


def is_daylight(unix_times: np.ndarray, lat: float, lon: float,
                elevation: float = DAYLIGHT_ELEVATION_DEG) -> np.ndarray:
    """True for hours with enough light to surf at this location."""
    return _solar_elevation(unix_times, lat, lon) >= elevation


def _daily_maxima(values: np.ndarray, times: np.ndarray):
    """Collapse an hourly series to one value per calendar day (UTC)."""
    days = times // 86400
    edges = np.flatnonzero(np.diff(days)) + 1
    groups = np.split(values, edges)
    day_groups = np.split(days, edges)
    return (
        np.array([g.max() for g in groups if len(g)]),
        np.array([g[0] for g in day_groups if len(g)]),
    )


def build_baseline(
    spots,
    end_date: dt.date | None = None,
    client=None,
) -> Baseline | None:
    """
    Fetch the full history for every spot and pool it into ONE statewide
    baseline, covering the entire record from HISTORY_START to `end_date`
    (default: yesterday). Not windowed to any time of year - see the module
    docstring for why.

    One batched request covers all spots. Callers should normally go through
    `load_baseline`, which puts a disk cache in front of this. Returns None if
    too little data comes back to say anything useful.
    """
    if client is None:
        import openmeteo_requests
        client = openmeteo_requests.Client()

    end = (end_date or dt.date.today()) - dt.timedelta(days=1)
    end = min(end, dt.date.today() - dt.timedelta(days=1))

    responses = client.weather_api(
        MARINE_URL,
        params={
            "latitude": ",".join(str(s.lat) for s in spots),
            "longitude": ",".join(str(s.lon) for s in spots),
            "hourly": list(HISTORY_VARS),
            "length_unit": "imperial",
            "timezone": "UTC",
            "start_date": HISTORY_START,
            "end_date": end.isoformat(),
        },
    )

    heights: list[np.ndarray] = []
    periods: list[np.ndarray] = []
    all_days: list[np.ndarray] = []
    n_spots = 0

    for spot, response in zip(spots, responses):
        hourly = response.Hourly()
        h = hourly.Variables(0).ValuesAsNumpy()
        p = hourly.Variables(1).ValuesAsNumpy()
        times = np.arange(len(h)) * hourly.Interval() + hourly.Time()

        # Night hours are dropped before anything else. A baseline that counts
        # 3am swell describes the ocean, not the surf: those hours can never be
        # ridden, so letting them set "normal" - or supply a day's maximum -
        # measures something nobody can act on.
        finite = ~np.isnan(h) & ~np.isnan(p) & is_daylight(times, spot.lat, spot.lon)
        if not finite.any():
            continue

        h_daily, days = _daily_maxima(h[finite], times[finite])
        p_daily, _ = _daily_maxima(p[finite], times[finite])

        heights.append(h_daily)
        periods.append(p_daily)
        all_days.append(days)
        n_spots += 1

    if not heights:
        return None

    pooled_h = np.concatenate(heights)
    pooled_p = np.concatenate(periods)
    pooled_days = np.concatenate(all_days)

    if len(pooled_h) < 10:
        return None

    # Distinct days, not spot-days: all 41 spots see broadly the same weather
    # systems, so a single date supplies roughly one independent observation.
    distinct_days = np.unique(pooled_days)
    years = {
        dt.datetime.fromtimestamp(int(d) * 86400, dt.timezone.utc).year
        for d in distinct_days
    }

    # Geometric statistics: see the note on Baseline.log_height_mean for why
    # these are taken in log space rather than on the raw values.
    pos_h = pooled_h[pooled_h > 0]
    pos_p = pooled_p[pooled_p > 0]
    log_h = np.log(pos_h) if len(pos_h) else np.array([0.0])
    log_p = np.log(pos_p) if len(pos_p) else np.array([0.0])

    return Baseline(
        height_p=tuple(float(v) for v in np.percentile(pooled_h, _LEVELS)),
        period_p=tuple(float(v) for v in np.percentile(pooled_p, _LEVELS)),
        n_days=int(len(distinct_days)),
        n_years=len(years),
        n_observations=int(len(pooled_h)),
        n_spots=n_spots,
        log_height_mean=float(log_h.mean()),
        log_height_sd=float(log_h.std()),
        log_period_mean=float(log_p.mean()),
        log_period_sd=float(log_p.std()),
    )


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _cache_is_fresh(path: str, max_age_days: int) -> bool:
    try:
        with open(path) as fh:
            meta = json.load(fh).get("meta", {})
    except (OSError, ValueError):
        return False
    try:
        built = dt.date.fromisoformat(meta["built"])
    except (KeyError, ValueError):
        return False
    return 0 <= (dt.date.today() - built).days <= max_age_days


def load_baseline(
    spots,
    path: str = DEFAULT_CACHE_PATH,
    max_age_days: int = CACHE_MAX_AGE_DAYS,
    end_date: dt.date | None = None,
    client=None,
    force_refresh: bool = False,
) -> Baseline | None:
    """
    Return the statewide baseline, hitting the network only if needed.

    The history request is heavy enough that running it every invocation trips
    Open-Meteo's per-minute limit and starves the live conditions request in
    the same run. This cache is what keeps that from happening. Because the
    baseline is no longer seasonal, freshness is purely a matter of age - it
    does not need rebuilding just because the calendar moved.

    Any failure returns None rather than raising: the tool must still rank
    spots when the baseline is unavailable, just without rarity.
    """
    if not force_refresh and _cache_is_fresh(path, max_age_days):
        try:
            with open(path) as fh:
                raw = json.load(fh)["baseline"]
            return Baseline(
                height_p=tuple(raw["height_p"]),
                period_p=tuple(raw["period_p"]),
                n_days=raw["n_days"],
                n_years=raw["n_years"],
                n_observations=raw.get("n_observations", 0),
                n_spots=raw.get("n_spots", 0),
                log_height_mean=raw["log_height_mean"],
                log_height_sd=raw["log_height_sd"],
                log_period_mean=raw["log_period_mean"],
                log_period_sd=raw["log_period_sd"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            pass  # fall through and rebuild

    try:
        baseline = build_baseline(spots, end_date=end_date, client=client)
    except Exception:
        return None

    if baseline is None:
        return None

    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(
                {
                    "meta": {
                        "built": dt.date.today().isoformat(),
                        "history_start": HISTORY_START,
                        "vars": list(HISTORY_VARS),
                        "scope": "florida-wide pooled, full record, daylight hours only",
                        "daylight_elevation_deg": DAYLIGHT_ELEVATION_DEG,
                    },
                    "baseline": {
                        "height_p": list(baseline.height_p),
                        "period_p": list(baseline.period_p),
                        "n_days": baseline.n_days,
                        "n_years": baseline.n_years,
                        "n_observations": baseline.n_observations,
                        "n_spots": baseline.n_spots,
                        "log_height_mean": baseline.log_height_mean,
                        "log_height_sd": baseline.log_height_sd,
                        "log_period_mean": baseline.log_period_mean,
                        "log_period_sd": baseline.log_period_sd,
                    },
                },
                fh,
            )
        os.replace(tmp, path)  # atomic; never leaves a half-written cache
    except OSError:
        pass  # the cache is an optimisation, not a requirement

    return baseline

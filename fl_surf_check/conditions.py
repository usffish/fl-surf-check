"""
Fetch current surf conditions for every spot.

Sources (both free, documented, no API key, no signup):

  1. Open-Meteo Marine API  - wave height, period, direction
     Open-Meteo Forecast API - wind speed & direction
     Fetched through `openmeteo-requests`, the OFFICIAL Open-Meteo Python
     client (https://github.com/open-meteo/python-requests). Two things make
     it worth using over hand-rolled requests:
       - it accepts a comma-separated list of coordinates, so all ~26 spots
         come back in ONE request instead of 26 (much kinder to a free API,
         and much faster)
       - combined with requests-cache, repeated runs within the hour are
         served from a local cache instead of re-hitting the API

  2. NOAA CO-OPS Tides & Currents - tide predictions
     https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
     Plain `requests` against the documented public endpoint. (The
     `noaa-coops` PyPI package wraps this, but it drags in pandas AND zeep,
     a full SOAP stack, for what is a single documented GET - a much larger
     dependency and audit surface than this needs.)

The "nearest tide station" idea and the caching/retry client structure are
adapted from ryansurf/cli-surf (MIT). See ATTRIBUTION.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_TIDE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

CACHE_SECONDS = 1800  # 30 min - surf data doesn't change faster than this


@dataclass
class Conditions:
    """Current conditions at one spot. Any field may be None if unavailable."""
    wave_height_ft: float | None = None      # TOTAL sea state (swell + wind chop)
    wave_period_s: float | None = None
    wave_direction_deg: float | None = None
    # The SWELL partition on its own, with local wind chop removed. This is the
    # rideable groundswell, and it is what the historical baselines in
    # climatology.py are built from - so rarity must be judged on these, not on
    # the total. Validated against the NWS coastal waters forecast: Open-Meteo
    # reported swell 1.12ft @ 7.9s from 90deg where NWS said "east 1 foot at
    # 9 seconds", while the total sea state read 1.31ft @ 7.25s because it had
    # 0.66ft of 1.7s sea-breeze chop mixed in.
    swell_height_ft: float | None = None
    swell_period_s: float | None = None
    swell_direction_deg: float | None = None
    wind_speed_mph: float | None = None
    wind_direction_deg: float | None = None
    # Thunderstorm risk. Rain is fine to surf in; lightning is not, so this is
    # tracked separately from wave quality rather than folded into the score.
    #
    # NOTE this is forecast-only. The ERA5 archive that backs the historical
    # baseline never emits thunderstorm weather codes at all (measured: zero
    # occurrences of 95/96/99 across 1.1M hours in the most thunderstorm-prone
    # state in the US), because convection is sub-grid at ~25km and gets
    # parameterised into ordinary rain. CAPE is likewise all-NaN in the archive
    # while being available in the forecast. See climatology for why filtering
    # history for storms is neither possible nor necessary.
    weather_code: float | None = None
    cape_j_kg: float | None = None          # convective available potential energy
    precip_probability: float | None = None  # percent
    tide_state: str | None = None          # "rising", "falling", or None
    next_tide: str | None = None           # e.g. "H 14:32"
    errors: tuple[str, ...] = ()

    @property
    def has_wave_data(self) -> bool:
        return self.wave_height_ft is not None and self.wave_period_s is not None


def _build_client():
    """
    Build an Open-Meteo client with on-disk caching.

    Note: we deliberately do NOT use the `retry-requests` helper that
    Open-Meteo's own examples suggest. It is licensed GPLv3+, which is
    copyleft and would be worth thinking about if this code were ever
    redistributed. A short retry loop below costs us ~10 lines and keeps the
    whole project permissively licensed. See SECURITY-REVIEW.md.
    """
    import openmeteo_requests

    try:
        import requests_cache
        session = requests_cache.CachedSession(
            ".fl_surf_cache", backend="sqlite", expire_after=CACHE_SECONDS
        )
        return openmeteo_requests.Client(session=session)
    except Exception:
        # Caching is a nicety, not a requirement.
        return openmeteo_requests.Client()


def _series(hourly, var_index: int, count: int) -> list[float | None]:
    """Pull one hourly variable out of an Open-Meteo block as a plain list."""
    try:
        values = hourly.Variables(var_index).ValuesAsNumpy()
    except Exception:
        return [None] * count
    out: list[float | None] = []
    for i in range(count):
        if i >= len(values):
            out.append(None)
            continue
        v = float(values[i])
        out.append(None if v != v else v)  # filter NaN
    return out


def _window(hourly, hours_ahead: int) -> tuple[int, int, int]:
    """
    Index range covering now -> now + hours_ahead, plus the start index.

    Returns (start_index, count, first_timestamp).
    """
    start = hourly.Time()
    interval = hourly.Interval() or 3600
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    i0 = max(0, (now - start) // interval)
    n = max(1, hours_ahead * 3600 // interval)
    return int(i0), int(n), int(start + i0 * interval)


FORECAST_HOURS = 24

#: Longest window the marine model actually supports. Measured, not guessed:
#: at forecast_days=7 it returns 168 hours with zero NaN; at 10 the last 24
#: hours come back empty, and at 16 more than a third of the window is missing.
#: The atmospheric model runs further (16 days) but the swell is the binding
#: constraint, so requests are capped here.
MAX_FORECAST_DAYS = 7


def _forecast_days_for(hours_ahead: int) -> int:
    """Whole forecast days needed to cover a window, capped at what the model has."""
    return max(2, min(MAX_FORECAST_DAYS, (hours_ahead + 47) // 24))


def fetch_marine_and_wind(spots, hours_ahead: int = FORECAST_HOURS):
    """
    Fetch wave + wind data for all spots in two batched API calls.

    Returns {spot name: [(unix_time, Conditions), ...]} covering the next
    `hours_ahead` hours, NOT a single reading.

    Why a series: the historical baseline is built from each day's BEST hour,
    so comparing it against whatever hour you happened to run the tool is an
    apples-to-oranges test. Measured on the record, a randomly chosen hour
    scores a median of p34 against that baseline when it should average p50 -
    a systematic 16-point understatement of every rarity score.

    Florida makes this worse than a rounding error. The sea breeze swings the
    wind from offshore at dawn to onshore by mid-afternoon (measured at Cocoa
    Beach, July-August: 232 degrees at 05:00 to 125 degrees at 15:00), so the
    same swell scores very differently depending on when you ask. Returning
    the window lets the caller score every hour and pick the best one you can
    still get to.
    """
    results: dict[str, list] = {s.name: [] for s in spots}
    lats = ",".join(str(s.lat) for s in spots)
    lons = ",".join(str(s.lon) for s in spots)

    client = _build_client()

    # --- Waves (one request, all spots) ---
    try:
        responses = _with_retry(
            lambda: client.weather_api(
                MARINE_URL,
                params={
                    "latitude": lats,
                    "longitude": lons,
                    "hourly": [
                        "wave_height", "wave_period", "wave_direction",
                        "swell_wave_height", "swell_wave_period", "swell_wave_direction",
                    ],
                    "length_unit": "imperial",
                    "timezone": "UTC",
                    "forecast_days": _forecast_days_for(hours_ahead),
                },
            )
        )
        for spot, response in zip(spots, responses):
            hourly = response.Hourly()
            i0, n, t0 = _window(hourly, hours_ahead)
            interval = hourly.Interval() or 3600
            cols = [_series(hourly, v, i0 + n)[i0:] for v in range(6)]
            results[spot.name] = [
                (t0 + k * interval, Conditions(
                    wave_height_ft=cols[0][k], wave_period_s=cols[1][k],
                    wave_direction_deg=cols[2][k], swell_height_ft=cols[3][k],
                    swell_period_s=cols[4][k], swell_direction_deg=cols[5][k],
                ))
                for k in range(min(n, len(cols[0])))
            ]
    except Exception as exc:
        for spot in spots:
            if not results[spot.name]:
                results[spot.name] = [(0, Conditions())]
            for _, c in results[spot.name]:
                c.errors = c.errors + (f"wave data unavailable: {_describe(exc)}",)

    # --- Wind (one request, all spots) ---
    try:
        responses = _with_retry(
            lambda: client.weather_api(
                WEATHER_URL,
                params={
                    "latitude": lats,
                    "longitude": lons,
                    "hourly": [
                        "wind_speed_10m", "wind_direction_10m",
                        "weather_code", "cape", "precipitation_probability",
                    ],
                    "wind_speed_unit": "mph",
                    "timezone": "UTC",
                    "forecast_days": _forecast_days_for(hours_ahead),
                },
            )
        )
        for spot, response in zip(spots, responses):
            hourly = response.Hourly()
            i0, n, _ = _window(hourly, hours_ahead)
            cols = [_series(hourly, v, i0 + n)[i0:] for v in range(5)]
            for k, (_, c) in enumerate(results[spot.name]):
                if k >= len(cols[0]):
                    break
                c.wind_speed_mph = cols[0][k]
                c.wind_direction_deg = cols[1][k]
                c.weather_code = cols[2][k]
                c.cape_j_kg = cols[3][k]
                c.precip_probability = cols[4][k]
    except Exception as exc:
        for readings in results.values():
            for _, c in readings:
                c.errors = c.errors + (f"wind data unavailable: {_describe(exc)}",)

    return results


def _is_rate_limit(exc: Exception) -> bool:
    """True if this exception is Open-Meteo telling us to slow down."""
    return "limit exceeded" in str(exc).lower() or "rate limit" in str(exc).lower()


def _with_retry(fn, attempts: int = 3, backoff: float = 0.5):
    """
    Tiny retry helper (see the licensing note in _build_client).

    Rate-limit errors are deliberately NOT retried. Open-Meteo's limit is
    per-MINUTE, so the sub-second backoff here would never outlast it, and each
    extra attempt spends more of the quota we are already out of. Failing fast
    lets the caller surface an accurate message instead of stalling and then
    reporting a generic error. This is reachable in normal use: a
    --refresh-history run pulls ~1.1M samples and can leave the very next
    invocation rate-limited.
    """
    import time

    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if _is_rate_limit(exc):
                raise
            if i < attempts - 1:
                time.sleep(backoff * (2 ** i))
    raise last


def _describe(exc: Exception) -> str:
    """Short, actionable reason for a failed fetch."""
    if _is_rate_limit(exc):
        return "Open-Meteo rate limit hit - wait a minute and retry"
    return type(exc).__name__


def fetch_tide(
    station_id: str,
    tz: str = "America/New_York",
    session: requests.Session | None = None,
    hours: int = 48,
) -> tuple[str | None, str | None]:
    """
    Get (tide_state, next_tide_label) for a NOAA station.

    tide_state is "rising" or "falling" based on whether the next predicted
    extreme is a high or a low. Returns (None, None) on any failure - tide is
    a minor scoring input, so it degrades gracefully.

    `tz` must be the IANA timezone of the station (Spot.tz). We request
    time_zone=lst_ldt, so NOAA returns timestamps in the STATION's local time.
    Comparing those against a bare datetime.now() - the machine's local time -
    is only correct when the machine happens to share the station's timezone.
    The three Panhandle stations are US/Central while the machine (and every
    other station) is typically US/Eastern, so that comparison ran one hour
    fast there and could skip a tide that was imminent. Observed live: a Low
    18:02 Central was skipped in favour of the following High at 07:07 the
    next morning, which also inverted tide_state from falling to rising.
    """
    getter = session.get if session is not None else requests.get
    now = dt.datetime.now(dt.timezone.utc)
    params = {
        "station": station_id,
        "product": "predictions",
        "datum": "MLLW",
        "interval": "hilo",
        "units": "english",
        "time_zone": "lst_ldt",
        "format": "json",
        "begin_date": now.strftime("%Y%m%d"),
        "range": max(24, min(240, hours)),
        "application": "fl-surf-check",
    }

    try:
        resp = getter(NOAA_TIDE_URL, params=params, timeout=10)
        resp.raise_for_status()
        predictions = resp.json().get("predictions", [])
    except (requests.RequestException, ValueError, AttributeError):
        return None, None

    local_now = dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    for p in predictions:
        try:
            when = dt.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if when > local_now:
            kind = p.get("type", "")
            state = "rising" if kind == "H" else "falling"
            label = f"{'High' if kind == 'H' else 'Low'} {when.strftime('%H:%M')}"
            return state, label

    return None, None

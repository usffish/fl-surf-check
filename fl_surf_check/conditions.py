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

import requests

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_TIDE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

CACHE_SECONDS = 1800  # 30 min - surf data doesn't change faster than this


@dataclass
class Conditions:
    """Current conditions at one spot. Any field may be None if unavailable."""
    wave_height_ft: float | None = None
    wave_period_s: float | None = None
    wave_direction_deg: float | None = None
    wind_speed_mph: float | None = None
    wind_direction_deg: float | None = None
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


def _values_at_current_hour(hourly, var_index: int) -> float | None:
    """Pull the value for the current hour out of an Open-Meteo hourly block."""
    try:
        values = hourly.Variables(var_index).ValuesAsNumpy()
        start = hourly.Time()
        interval = hourly.Interval()
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        idx = max(0, min(len(values) - 1, (now - start) // interval))
        value = float(values[idx])
        return None if value != value else value  # filter NaN
    except Exception:
        return None


def fetch_marine_and_wind(spots) -> dict[str, Conditions]:
    """
    Fetch wave + wind data for all spots in two batched API calls.

    Returns a dict keyed by spot name.
    """
    results = {s.name: Conditions() for s in spots}
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
                    "hourly": ["wave_height", "wave_period", "wave_direction"],
                    "length_unit": "imperial",
                    "timezone": "UTC",
                    "forecast_days": 1,
                },
            )
        )
        for spot, response in zip(spots, responses):
            hourly = response.Hourly()
            c = results[spot.name]
            c.wave_height_ft = _values_at_current_hour(hourly, 0)
            c.wave_period_s = _values_at_current_hour(hourly, 1)
            c.wave_direction_deg = _values_at_current_hour(hourly, 2)
    except Exception as exc:
        for c in results.values():
            c.errors = c.errors + (f"wave data unavailable: {type(exc).__name__}",)

    # --- Wind (one request, all spots) ---
    try:
        responses = _with_retry(
            lambda: client.weather_api(
                WEATHER_URL,
                params={
                    "latitude": lats,
                    "longitude": lons,
                    "hourly": ["wind_speed_10m", "wind_direction_10m"],
                    "wind_speed_unit": "mph",
                    "timezone": "UTC",
                    "forecast_days": 1,
                },
            )
        )
        for spot, response in zip(spots, responses):
            hourly = response.Hourly()
            c = results[spot.name]
            c.wind_speed_mph = _values_at_current_hour(hourly, 0)
            c.wind_direction_deg = _values_at_current_hour(hourly, 1)
    except Exception as exc:
        for c in results.values():
            c.errors = c.errors + (f"wind data unavailable: {type(exc).__name__}",)

    return results


def _with_retry(fn, attempts: int = 3, backoff: float = 0.5):
    """Tiny retry helper (see the licensing note in _build_client)."""
    import time

    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(backoff * (2 ** i))
    raise last


def fetch_tide(station_id: str, session: requests.Session | None = None) -> tuple[str | None, str | None]:
    """
    Get (tide_state, next_tide_label) for a NOAA station.

    tide_state is "rising" or "falling" based on whether the next predicted
    extreme is a high or a low. Returns (None, None) on any failure - tide is
    a minor scoring input, so it degrades gracefully.
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
        "range": 48,
        "application": "fl-surf-check",
    }

    try:
        resp = getter(NOAA_TIDE_URL, params=params, timeout=10)
        resp.raise_for_status()
        predictions = resp.json().get("predictions", [])
    except (requests.RequestException, ValueError, AttributeError):
        return None, None

    local_now = dt.datetime.now()
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

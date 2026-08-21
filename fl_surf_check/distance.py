"""
Distance / drive-time helpers.

Straight-line distance uses `geopy.distance.great_circle` (MIT) rather than a
hand-rolled haversine - it's a well-tested implementation and geopy is already
a dependency for the geocoding fallback.

Driving distance/time uses the public OSRM demo server
(https://project-osrm.org/) - free, no API key, documented public endpoint.
It is a shared demo instance not intended for heavy traffic, so we make at
most one request per spot per run and fall back gracefully.

Fallback: if OSRM is unreachable, approximate road distance as straight-line
distance x a windiness factor, and drive time from an assumed average speed.
The result is labeled so the CLI can show you it's an estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import requests
from geopy.distance import great_circle

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"

# Used only when OSRM can't be reached.
ROAD_WINDINESS_FACTOR = 1.25   # real roads are never straight lines
FALLBACK_AVG_SPEED_MPH = 50.0  # rough FL highway-ish average


@dataclass
class DriveEstimate:
    distance_miles: float
    duration_minutes: float
    source: str  # "osrm" (real routing) or "estimate" (straight-line fallback)

    @property
    def is_estimate(self) -> bool:
        return self.source != "osrm"


def straight_line_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles, via geopy."""
    return great_circle((lat1, lon1), (lat2, lon2)).miles


def get_drive_estimate(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    timeout: float = 8.0,
    session: requests.Session | None = None,
) -> DriveEstimate:
    """Best-effort driving distance and time from origin to destination."""
    getter = session.get if session is not None else requests.get
    url = OSRM_URL.format(lon1=origin_lon, lat1=origin_lat, lon2=dest_lon, lat2=dest_lat)

    try:
        resp = getter(url, params={"overview": "false"}, timeout=timeout)
        resp.raise_for_status()
        route = resp.json()["routes"][0]
        return DriveEstimate(
            distance_miles=route["distance"] / 1609.344,
            duration_minutes=route["duration"] / 60.0,
            source="osrm",
        )
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        straight = straight_line_miles(origin_lat, origin_lon, dest_lat, dest_lon)
        approx_miles = straight * ROAD_WINDINESS_FACTOR
        return DriveEstimate(
            distance_miles=approx_miles,
            duration_minutes=(approx_miles / FALLBACK_AVG_SPEED_MPH) * 60.0,
            source="estimate",
        )


def closeness_factor(distance_miles: float, decay_miles: float) -> float:
    """
    Map a distance to a 0-1 "closeness" score using exponential decay.

    Returns 1.0 at zero distance and falls off smoothly. `decay_miles` is the
    distance at which the factor drops to about 37% (1/e) - think of it as
    "how far you're comfortable driving for a good session."

    Exponential decay (rather than a hard cutoff) is deliberate: it means a
    genuinely great, far-away day can still out-rank a mediocre close one,
    instead of far spots being excluded outright.
    """
    if decay_miles <= 0:
        return 1.0 if distance_miles <= 0 else 0.0
    return math.exp(-max(0.0, distance_miles) / decay_miles)

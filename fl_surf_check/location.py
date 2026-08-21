"""
Zip code -> (lat, lon).

Primary: `pgeocode` (BSD-3-Clause, https://github.com/symerio/pgeocode).
It uses the GeoNames postal-code dataset. On first use it downloads a small
country data file and caches it locally, so every subsequent run is fully
OFFLINE - no per-request rate limits, no API key, no phoning home with your
location on every run. That privacy/reliability property is why it's the
primary path.

Fallback: OpenStreetMap Nominatim via `geopy` (MIT), used only if pgeocode
can't resolve the zip. Nominatim's usage policy requires a descriptive
User-Agent and light traffic; we make at most one call per run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NOMINATIM_USER_AGENT = "fl-surf-check/1.0 (personal surf conditions script)"


class GeocodeError(Exception):
    """Raised when a zip code cannot be turned into coordinates."""


@dataclass
class Origin:
    lat: float
    lon: float
    label: str
    source: str  # "pgeocode" or "nominatim"


def _try_pgeocode(zip_code: str) -> Origin | None:
    try:
        import pgeocode
    except ImportError:
        return None

    try:
        nomi = pgeocode.Nominatim("us")
        rec = nomi.query_postal_code(zip_code)
    except Exception:
        # pgeocode raises a variety of things if the data file can't be
        # fetched on first run (no network, etc.). Fall through to Nominatim.
        return None

    lat = rec.get("latitude")
    lon = rec.get("longitude")
    if lat is None or lon is None or (isinstance(lat, float) and math.isnan(lat)):
        return None

    place = rec.get("place_name") or ""
    state = rec.get("state_code") or ""
    label = f"{place}, {state} {zip_code}".strip().strip(",")
    return Origin(float(lat), float(lon), label, source="pgeocode")


def _try_nominatim(zip_code: str) -> Origin | None:
    try:
        from geopy.geocoders import Nominatim
    except ImportError:
        return None

    try:
        geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=10)
        loc = geolocator.geocode({"postalcode": zip_code, "country": "us"})
    except Exception:
        return None

    if loc is None:
        return None
    return Origin(float(loc.latitude), float(loc.longitude), loc.address, source="nominatim")


def geocode_zip(zip_code: str) -> Origin:
    """Resolve a US zip code to an Origin. Raises GeocodeError on failure."""
    zip_code = str(zip_code).strip()
    if not (len(zip_code) == 5 and zip_code.isdigit()):
        raise GeocodeError(
            f"'{zip_code}' doesn't look like a US zip code. Expected 5 digits, e.g. 32118."
        )

    for attempt in (_try_pgeocode, _try_nominatim):
        origin = attempt(zip_code)
        if origin is not None:
            return origin

    raise GeocodeError(
        f"Could not resolve zip code {zip_code}. Check that it's valid, and that you "
        "have internet access for the first run (the zip database downloads once, "
        "then works offline)."
    )

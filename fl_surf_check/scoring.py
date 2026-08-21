"""
Surf quality scoring, and the "is it worth the drive" composite.

ALGORITHM PROVENANCE
--------------------
The overall shape of the surf score - break the forecast into independent
sub-ratings for swell height, swell period and wind direction, then combine
them - is adapted from the `Rating` service in hugosrc/surf-forecast-api
(Apache-2.0). See ATTRIBUTION.md.

Three deliberate changes were made to that original:

1. CONTINUOUS, NOT BUCKETED. The original snaps wind and swell directions
   into four compass quadrants (N/E/S/W) and returns integer sub-scores.
   Florida's coastline curves through roughly 80 degrees from Fernandina to
   Miami, so quadrant buckets put spots only ~50 miles apart into different
   categories and produce lots of tied scores. Here, direction is scored on
   the continuous angle between the wind and the spot's own offshore bearing,
   which both fits a curving coastline and gives a strict ranking with no ties.

2. WIND SPEED MATTERS, NOT JUST DIRECTION. The original scores wind purely on
   direction. But 3mph onshore is glassy and fine, while 25mph offshore is
   unrideable chop. Speed scales how much direction is allowed to matter.

3. FLORIDA-TUNED HEIGHT CURVE. The original treats "head high and above" as a
   flat maximum. Florida is a small-wave coast; this curve peaks in the
   3-5ft range and eases back above ~7ft, where FL beach breaks tend to close
   out rather than improve.

All scores are on a 0-10 scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Relative importance of each sub-score in the overall surf rating.
# These are opinionated but easy to tune - they must sum to 1.0.
WEIGHTS = {
    "height": 0.35,
    "period": 0.30,
    "wind": 0.25,
    "swell_direction": 0.10,
}


def _interpolate(x: float, points: list[tuple[float, float]]) -> float:
    """
    Piecewise-linear interpolation over (input, score) control points.

    Using interpolation instead of hard if/elif buckets means a 2.49ft wave
    and a 2.51ft wave get nearly identical scores, rather than falling into
    different tiers - which matters a lot when you're ranking 26 spots.
    """
    if not points:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return points[-1][1]


def score_wave_height(height_ft: float | None) -> float:
    """
    Score wave height, tuned for Florida beach breaks.

    Peaks around 3-5ft. Eases off above ~7ft where FL sandbars tend to
    close out rather than hold shape.
    """
    if height_ft is None:
        return 0.0
    return _interpolate(height_ft, [
        (0.0, 0.0),
        (0.7, 1.0),    # ankle slappers / flat
        (1.5, 3.5),    # knee-waist, longboard-able
        (2.5, 6.5),    # waist-chest, fun
        (3.5, 9.0),    # chest-head, very good for FL
        (5.0, 10.0),   # head+ - about as good as FL gets
        (7.0, 8.5),    # overhead, starting to get thick
        (10.0, 6.0),   # big and likely closing out
        (15.0, 3.5),   # storm surf, mostly unrideable beach break
    ])


def score_wave_period(period_s: float | None) -> float:
    """
    Score swell period. This is the single best proxy for wave QUALITY.

    Short-period (<6s) windswell is the disorganized slop Florida gets most
    of the time. Long-period groundswell (12s+) means organized, powerful,
    well-spaced waves - the stuff worth driving for.
    """
    if period_s is None:
        return 0.0
    return _interpolate(period_s, [
        (0.0, 0.0),
        (4.0, 1.0),    # pure wind chop
        (6.0, 2.5),    # short-period windswell
        (8.0, 5.0),    # mixed, workable
        (10.0, 7.0),   # decent organization
        (12.0, 8.75),  # proper groundswell
        (14.0, 10.0),  # excellent
        (20.0, 10.0),  # capped
    ])


def angular_difference(a: float, b: float) -> float:
    """Smallest absolute angle between two compass bearings, 0-180 degrees."""
    return abs((a - b + 180) % 360 - 180)


def score_wind(
    wind_speed_mph: float | None,
    wind_direction_deg: float | None,
    spot_offshore_deg: float,
) -> float:
    """
    Score wind from its speed and its angle relative to this spot's offshore
    direction.

    Both Open-Meteo's wind_direction_10m and our spot bearings use the
    meteorological convention: the direction the wind is coming FROM. So wind
    coming from `spot_offshore_deg` is blowing off the land and into the face
    of the wave - which grooms it and holds it open. Wind from the opposite
    direction is onshore, and turns the surf to mush.

    The key refinement over a pure direction score: wind SPEED scales how much
    direction matters. Near-calm wind is great no matter where it's from
    (glassy), while a howling wind is bad even when nominally offshore.
    """
    if wind_speed_mph is None or wind_direction_deg is None:
        return 5.0  # neutral - don't punish a spot for missing data

    # +1.0 = perfectly offshore, 0 = cross-shore, -1.0 = perfectly onshore
    offset = angular_difference(wind_direction_deg, spot_offshore_deg)
    alignment = math.cos(math.radians(offset))

    # Glassy conditions: below ~4mph, direction is nearly irrelevant.
    if wind_speed_mph <= 4.0:
        return 8.5 + 1.5 * max(0.0, alignment)

    # How strongly direction influences the score, ramping in with speed.
    influence = min(1.0, (wind_speed_mph - 4.0) / 14.0)

    # Baseline that decays as wind gets strong - even good offshore wind
    # becomes a problem when it's really honking.
    strength_penalty = _interpolate(wind_speed_mph, [
        (4.0, 0.0),
        (12.0, 0.4),
        (20.0, 1.6),
        (30.0, 3.5),
        (45.0, 5.5),
    ])

    base = 6.0
    score = base + (alignment * 4.0 * influence) - strength_penalty
    return max(0.0, min(10.0, score))


def score_swell_direction(
    wave_direction_deg: float | None,
    spot_facing_deg: float,
) -> float:
    """
    Score how directly the swell is hitting the beach.

    Swell arriving straight on has travelled unobstructed; swell arriving at a
    steep angle is partly shadowed by the coastline and loses size. This is a
    deliberately gentle input (only 10% of the total) because our per-spot
    beach bearings are approximations, not surveyed data.
    """
    if wave_direction_deg is None:
        return 5.0

    offset = angular_difference(wave_direction_deg, spot_facing_deg)
    if offset >= 90:
        return 0.5  # swell is coming from behind the beach - effectively blocked
    # cos falloff: straight-on = 10, 60 degrees off = ~5, 90 = blocked
    return max(0.5, 10.0 * math.cos(math.radians(offset)) ** 1.5)


def tide_modifier(tide_state: str | None) -> float:
    """
    Small bonus for a moving tide.

    Moving water generally means better-shaped, more consistent waves than a
    slack tide. This is intentionally tiny (+/- 0.15) - tide preference is
    extremely spot-specific and we don't have per-spot tide data good enough
    to justify more.
    """
    if tide_state in ("rising", "falling"):
        return 0.15
    return 0.0


@dataclass
class SurfScore:
    total: float                 # 0-10 overall surf quality
    height: float
    period: float
    wind: float
    swell_direction: float
    confidence: str              # "full", "partial", or "none"

    def breakdown(self) -> str:
        return (
            f"height {self.height:.1f} | period {self.period:.1f} | "
            f"wind {self.wind:.1f} | dir {self.swell_direction:.1f}"
        )


def score_conditions(conditions, spot) -> SurfScore:
    """Combine all sub-scores into one 0-10 surf quality rating for a spot."""
    h = score_wave_height(conditions.wave_height_ft)
    p = score_wave_period(conditions.wave_period_s)
    w = score_wind(conditions.wind_speed_mph, conditions.wind_direction_deg, spot.offshore_deg)
    d = score_swell_direction(conditions.wave_direction_deg, spot.facing_deg)

    total = (
        WEIGHTS["height"] * h
        + WEIGHTS["period"] * p
        + WEIGHTS["wind"] * w
        + WEIGHTS["swell_direction"] * d
    )
    total = max(0.0, min(10.0, total + tide_modifier(conditions.tide_state)))

    if not conditions.has_wave_data:
        confidence = "none"
    elif conditions.wind_speed_mph is None:
        confidence = "partial"
    else:
        confidence = "full"

    return SurfScore(total, h, p, w, d, confidence)


# ---------------------------------------------------------------------------
# The "worth the drive" normalization
# ---------------------------------------------------------------------------

@dataclass
class WorthScore:
    surf_score: float        # 0-10, pure conditions
    closeness: float         # 0-1, distance decay factor
    total: float             # 0-10, blended "worth driving to" score
    verdict: str


def worth_the_drive(
    surf_score: float,
    distance_miles: float,
    closeness: float,
    surf_weight: float = 0.7,
) -> WorthScore:
    """
    Blend surf quality with how far away the spot is.

    total = 10 * (surf_weight * (surf/10) + (1 - surf_weight) * closeness)

    Both terms are normalized to 0-1 before weighting, so the two very
    different units (a 0-10 rating and a distance in miles) can be combined
    meaningfully. `closeness` comes from an exponential decay on distance -
    see distance.closeness_factor.

    Why a weighted blend rather than surf_score * closeness? Multiplying makes
    distance able to zero out an otherwise perfect day, which doesn't match
    how people actually decide. A blend keeps a genuinely epic far-away spot
    competitive with a mediocre close one, which is exactly the trade-off
    you're trying to see.
    """
    surf_weight = max(0.0, min(1.0, surf_weight))
    normalized = surf_weight * (surf_score / 10.0) + (1.0 - surf_weight) * closeness
    total = max(0.0, min(10.0, normalized * 10.0))
    return WorthScore(surf_score, closeness, total, _verdict(surf_score, distance_miles))


def _verdict(surf_score: float, distance_miles: float) -> str:
    """A plain-English call on whether to actually get in the car."""
    if surf_score < 2.0:
        return "Flat - stay home"
    if surf_score < 3.5:
        return "Poor" if distance_miles < 30 else "Not worth the drive"
    if surf_score < 5.0:
        if distance_miles < 25:
            return "Marginal, but it's close"
        return "Probably not worth it"
    if surf_score < 6.5:
        if distance_miles < 60:
            return "Worth a look"
        return "Decent, but that's a haul"
    if surf_score < 8.0:
        if distance_miles < 120:
            return "Go surf"
        return "Good enough to make the trip"
    if distance_miles > 200:
        return "Epic - but that's a road trip"
    return "GO. Drop everything"

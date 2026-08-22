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


# ---------------------------------------------------------------------------
# Rarity: is this unusually good FOR THIS SPOT, right now?
# ---------------------------------------------------------------------------
#
# Everything above scores conditions on an absolute scale. That answers "is
# this rideable?" but not "is this a day worth clearing my calendar for?" -
# which depends entirely on what the spot normally does. Flagler in August is
# a different question from Sebastian Inlet in February.
#
# The approach is lifted from the movie-score engine's composite, with one
# deliberate substitution:
#
#   min-max normalisation  ->  PERCENTILE RANK
#
# The batch-wide framing carries over unchanged. The movie engine computes min
# and max "across the entire batch ... not per-movie"; here the distribution is
# pooled across ALL Florida spots rather than built per-spot. Grading each break
# on its own curve would flatter the weak ones - a mediocre Pensacola day would
# read the same "p90" as a genuinely excellent Sebastian Inlet day. One shared
# yardstick keeps better spots reading as better, which is the entire point when
# you are choosing where to drive.
#
# Min-max is the right call when the population is bounded and roughly even
# (Metascores land across 0-100). Surf is neither: it is heavily right-skewed
# by rare storm events. Measured over the 5-year record, min-max maps a median
# day at Fernandina to 0.17 and even a strong p90 day to only 0.38, because a
# single 7.9ft hurricane swell owns the top of the range. Every ordinary day
# bunches into the bottom fifth of the scale and the signal is lost.
#
# Percentile rank asks the same question - "where does this sit in the observed
# population?" - but spreads the answer evenly across 0-1 by construction, and
# is immune to how extreme the extremes are.
#
# The evidence-weighting carries over directly. The movie engine distrusts a
# 100% score from 10 reviews via Laplace's rule of succession, (p+1)/(n+2).
# The same reasoning applies here with even more force: the marine record only
# starts in 2021-10, so a "top 2% day!" claim may rest on ~5 seasons. Rather
# than let a thin sample shout, the percentile is shrunk toward the median in
# proportion to how little evidence stands behind it.

# Rarity labels are expressed as a fraction of the HIGHEST percentile the
# shrinkage can actually produce, not as absolute percentiles.
#
# This matters because shrinkage imposes a ceiling. With the current baseline
# (n=130 days) even a once-in-5-years 7.4ft swell shrinks to p91, so fixed
# cutoffs at p93 or p97 would be unreachable dead code - the same trap as a
# verdict threshold no real input can cross. Scaling to the attainable range
# keeps every label reachable at any n, and keeps their meaning stable as the
# record grows: "as close to the top as this much evidence can support".
RARITY_STANDOUT = 0.75   # >= 75% of the way from normal to the attainable max
RARITY_RARE = 0.85
RARITY_TOP = 0.95

# Laplace-style prior strength, in "virtual days". A baseline built from this
# many real days carries half the weight of its raw percentile; far fewer and
# it is pulled hard toward the median. ~30 is a season's worth of observations.
RARITY_PRIOR_DAYS = 30.0


def shrink_percentile(percentile: float, n_days: int, prior_days: float = RARITY_PRIOR_DAYS) -> float:
    """
    Pull a percentile toward the median (50) according to how much data backs it.

    This is Laplace's rule of succession generalised from a proportion to a
    percentile: the prior is "this was a perfectly ordinary day" (50th) and it
    carries the weight of `prior_days` virtual observations.

        shrunk = (percentile * n + 50 * prior) / (n + prior)

    With a full seasonal baseline (n ~ 145) a 98th-percentile reading barely
    moves, to ~90. With only 8 days of history behind it, that same reading
    lands near 60 - visible, but not shouted about. That is the intended
    behaviour: the record only reaches back to late 2021, and a rarity claim
    should never outrun its evidence.
    """
    if n_days <= 0:
        return 50.0
    return (percentile * n_days + 50.0 * prior_days) / (n_days + prior_days)


@dataclass
class RarityScore:
    """How unusual today's swell is against the statewide seasonal record."""
    percentile: float | None       # 0-100 after shrinkage, None without a baseline
    height_percentile: float | None
    period_percentile: float | None
    n_days: int
    n_years: int
    baseline_summary: str = ""
    #: Geometric standard deviations above normal. Drives the extra drive-time
    #: allowance; see drive_allowance_minutes.
    sigma: float | None = None

    @property
    def relative(self) -> float | None:
        """
        How far this day reaches toward the best score the evidence can support.

        0.0 is a perfectly normal day (p50), 1.0 is the ceiling that shrinkage
        allows given `n_days`. Comparing against the ceiling rather than a raw
        100 is what keeps the labels reachable - see the note on RARITY_*.
        """
        if self.percentile is None or self.n_days <= 0:
            return None
        ceiling = shrink_percentile(100.0, self.n_days)
        if ceiling <= 50.0:
            return None
        return (self.percentile - 50.0) / (ceiling - 50.0)

    @property
    def is_standout(self) -> bool:
        rel = self.relative
        return rel is not None and rel >= RARITY_STANDOUT

    def label(self) -> str:
        """Short human phrase for how rare this is, or '' when unremarkable."""
        rel = self.relative
        if rel is None:
            return ""
        if rel >= RARITY_TOP:
            return f"BEST IN {self.n_years}YR"
        if rel >= RARITY_RARE:
            return "RARE"
        if rel >= RARITY_STANDOUT:
            return "STANDOUT"
        if rel >= 0.45:
            return "above normal"
        if rel <= -0.60:
            return "below normal"
        return ""


def _combine_weighted(*pairs) -> float | None:
    """
    Weighted mean over (value, weight) pairs, skipping missing values.

    Same dynamic-denominator rule used throughout: a None drops out of both
    numerator and denominator instead of being counted as zero.
    """
    num = 0.0
    den = 0.0
    for value, weight in pairs:
        if value is not None:
            num += value * weight
            den += weight
    return None if den == 0 else num / den


def rarity_score(conditions, baseline) -> RarityScore:
    """
    Score how unusual today's swell is against the Florida-wide baseline.

    Height and period are ranked separately against the pooled statewide
    distribution, then combined, weighted the same way they are in the absolute
    score (0.35 : 0.30, renormalised) so the two views of the day stay
    consistent with each other.

    Follows the movie engine's dynamic-denominator rule: a missing signal is
    dropped from both numerator and denominator rather than zero-filled, so a
    spot is never penalised for data it simply does not have.
    """
    if baseline is None:
        return RarityScore(None, None, None, 0, 0)

    sigma = _combine_weighted(
        (baseline.height_sigma(conditions.swell_height_ft), WEIGHTS["height"]),
        (baseline.period_sigma(conditions.swell_period_s), WEIGHTS["period"]),
    )

    ph = baseline.height_percentile(conditions.swell_height_ft)
    pp = baseline.period_percentile(conditions.swell_period_s)

    numerator = 0.0
    denominator = 0.0
    if ph is not None:
        numerator += ph * WEIGHTS["height"]
        denominator += WEIGHTS["height"]
    if pp is not None:
        numerator += pp * WEIGHTS["period"]
        denominator += WEIGHTS["period"]

    if denominator == 0:
        return RarityScore(None, ph, pp, baseline.n_days, baseline.n_years,
                           baseline.summary(), sigma)

    combined = shrink_percentile(numerator / denominator, baseline.n_days)
    return RarityScore(
        percentile=combined,
        height_percentile=ph,
        period_percentile=pp,
        n_days=baseline.n_days,
        n_years=baseline.n_years,
        baseline_summary=baseline.summary(),
        sigma=sigma,
    )


# ---------------------------------------------------------------------------
# Earning drive time with exceptional conditions
# ---------------------------------------------------------------------------
#
# `closeness_factor` decays willingness to travel against a fixed `decay_miles`,
# which is the same on a flat Tuesday as on the best swell of the year. But a
# surfer's tolerance for driving is not fixed - it stretches with how good the
# day is. The rule here makes that explicit and tunable:
#
#     every 1 standard deviation above normal buys 60 more minutes of driving
#
# "Standard deviation" is GEOMETRIC (computed on log values). Raw swell height
# is strongly right-skewed - measured skew 1.95 on the pooled record - so a raw
# z-score misbehaves: its mean sits at the 64th percentile, and the largest day
# in five years lands at z+5.9, which would buy an absurd six extra hours. In
# log space the skew falls to 0.27 and z recovers its textbook meaning:
# +1 SD = p84.8, +2 SD = p96.4, +3 SD = p99.9. The best day on record is a
# well-behaved +3.1 SD, worth about three extra hours.
#
# The sigma is shrunk by the same evidence weighting the rarity percentile uses.
# A thin baseline cannot be allowed to send anyone on a four-hour drive.

#: Extra driving time earned per geometric standard deviation above normal.
MINUTES_PER_SIGMA = 60.0

#: Ceiling on earned time, so a freak reading can never justify an absurd trip.
MAX_ALLOWANCE_MINUTES = 240.0


def drive_allowance_minutes(
    sigma: float | None,
    n_days: int = 0,
    minutes_per_sigma: float = MINUTES_PER_SIGMA,
    prior_days: float = RARITY_PRIOR_DAYS,
    cap: float = MAX_ALLOWANCE_MINUTES,
) -> float:
    """
    Extra minutes of driving justified by conditions this far above normal.

    Only days *better* than normal earn anything; a below-average day does not
    incur a penalty, because the plain drive time is already the honest cost.

    The sigma is damped by n/(n + prior) - the same Laplace-style weighting
    applied to the rarity percentile - so a baseline built from very few days
    cannot talk anyone into a long drive on weak evidence.
    """
    if sigma is None or sigma <= 0.0:
        return 0.0
    confidence = 1.0 if n_days <= 0 else n_days / (n_days + prior_days)
    return min(cap, sigma * minutes_per_sigma * confidence)


def effective_drive(
    distance_miles: float,
    duration_minutes: float,
    allowance_minutes: float,
) -> tuple[float, float]:
    """
    Discount a real drive by the time exceptional conditions have earned.

    Returns (effective_miles, effective_minutes). A 2-hour drive on a day that
    has earned 60 minutes is scored as though it were a 1-hour drive, so it
    competes with genuinely local options.

    Miles are scaled by the same fraction as minutes rather than being
    discounted independently, which preserves each route's own average speed -
    an hour of interstate and an hour of surface streets cover very different
    ground, and the mileage-based decay downstream should see that difference.
    """
    if allowance_minutes <= 0.0 or duration_minutes <= 0.0:
        return distance_miles, duration_minutes
    remaining = max(0.0, duration_minutes - allowance_minutes)
    fraction = remaining / duration_minutes
    return distance_miles * fraction, remaining


# ---------------------------------------------------------------------------
# Thunderstorms
# ---------------------------------------------------------------------------
#
# Rain does not stop anyone surfing. Lightning does - and a surfer is the tallest
# conductive object on a flat wet plain, which is why Florida leads the country
# in lightning fatalities. So storm risk is deliberately NOT folded into the
# 0-10 quality score: a perfect 8ft swell with a squall line overhead is not
# "slightly worse surf", it is surf you must not paddle out into. It is reported
# as a separate gate, the same way `confidence` reports data quality.
#
# This is forecast-only, and that asymmetry is measured rather than assumed:
# the ERA5 archive behind the historical baseline emits ZERO thunderstorm codes
# across 1.1M hours of Florida record, because ~25km reanalysis parameterises
# convection away into ordinary rain. Filtering history for storms is therefore
# impossible with that source - and, separately, unnecessary: the baseline is
# built from daily maxima, and excluding storm hours moves its percentiles by
# 0.00%, because a 1-3 hour afternoon storm almost never coincides with the
# day's peak swell.

#: WMO codes that mean a thunderstorm is happening right now.
THUNDERSTORM_CODES = frozenset({95, 96, 99})

#: CAPE (J/kg) above which the atmosphere can support thunderstorms at all.
CAPE_MARGINAL = 1000.0
#: CAPE indicating strong instability - storms likely, and likely severe.
CAPE_STRONG = 2500.0


def storm_risk(conditions) -> str:
    """
    Classify thunderstorm risk as "active", "likely", "possible" or "none".

    "active" comes straight from the observed weather code. The other two blend
    instability (CAPE) with how likely precipitation is: high CAPE on a dry day
    is a loaded gun with no trigger, so neither alone is enough.

    Returns "unknown" when the forecast fields are missing, so callers can tell
    "no storm" apart from "no data".
    """
    code = conditions.weather_code
    cape = conditions.cape_j_kg
    pop = conditions.precip_probability

    if code is not None and int(code) in THUNDERSTORM_CODES:
        return "active"

    if cape is None:
        return "unknown" if code is None else "none"

    chance = pop if pop is not None else 0.0
    if cape >= CAPE_STRONG and chance >= 40.0:
        return "likely"
    if cape >= CAPE_MARGINAL and chance >= 50.0:
        return "possible"
    return "none"


def storm_warning(risk: str) -> str:
    """Short human phrase for a storm risk level, or '' when there is nothing to say."""
    return {
        "active": "THUNDERSTORM - do not paddle out",
        "likely": "thunderstorms likely",
        "possible": "thunderstorms possible",
    }.get(risk, "")


def storm_blocks_travel(risk: str) -> bool:
    """
    True when conditions should not earn extra drive time.

    Exceptional surf normally buys extra driving (see drive_allowance_minutes),
    but that logic must not talk anyone into a three-hour drive toward a
    lightning storm. An active or likely storm forfeits the allowance entirely.
    """
    return risk in ("active", "likely")

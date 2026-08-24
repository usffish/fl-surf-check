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
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Region tuning
# ---------------------------------------------------------------------------
#
# These control points are the ONLY part of the scoring that is specific to
# Florida. Everything else - the data sources, the baseline machinery, the
# value calculation - is region-agnostic, and the marine API is global.
#
# They are named constants rather than literals buried in the functions so
# they can be tuned in one place, and so a second region can be added later by
# supplying a different set. What that would take is measured, not guessed:
# Westport WA has a median day of 4.72ft and a p90 of 9.32ft, against Florida's
# 1.31ft and 2.82ft. Fed to the curve below, a typical Westport day scores 9.8
# and a genuinely big one scores 6.6 - the ordering inverts, because the curve
# encodes "Florida sandbar" rather than "wave". See the README.

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


#: Swell height (ft) -> 0-10, tuned for Florida beach breaks. REGION-SPECIFIC.
HEIGHT_CURVE = [
    (0.0, 0.0),
    (0.7, 1.0),    # ankle slappers / flat
    (1.5, 3.5),    # knee-waist, longboard-able
    (2.5, 6.5),    # waist-chest, fun
    (3.5, 9.0),    # chest-head, very good for FL
    (5.0, 10.0),   # head+ - about as good as FL gets
    (7.0, 8.5),    # overhead, starting to get thick
    (10.0, 6.0),   # big and likely closing out
    (15.0, 3.5),   # storm surf, mostly unrideable beach break
]

#: Swell period (s) -> 0-10. Less region-specific than height - long period
#: means organised everywhere - but the useful range still varies by coast.
PERIOD_CURVE = [
    (0.0, 0.0),
    (4.0, 1.0),    # pure wind chop
    (6.0, 2.5),    # short-period windswell
    (8.0, 5.0),    # mixed, workable
    (10.0, 7.0),   # decent organization
    (12.0, 8.75),  # proper groundswell
    (14.0, 10.0),  # excellent
    (20.0, 10.0),  # capped
]

#: Wind speed (mph) -> penalty subtracted from the wind sub-score.
WIND_PENALTY_CURVE = [
    (4.0, 0.0),
    (12.0, 0.4),
    (20.0, 1.6),
    (30.0, 3.5),
    (45.0, 5.5),
]


def score_wave_height(height_ft: float | None) -> float:
    """
    Score wave height, tuned for Florida beach breaks via HEIGHT_CURVE.

    Peaks around 3-5ft. Eases off above ~7ft where FL sandbars tend to
    close out rather than hold shape.
    """
    if height_ft is None:
        return 0.0
    return _interpolate(height_ft, HEIGHT_CURVE)


def score_wave_period(period_s: float | None) -> float:
    """
    Score swell period. This is the single best proxy for wave QUALITY.

    Short-period (<6s) windswell is the disorganized slop Florida gets most
    of the time. Long-period groundswell (12s+) means organized, powerful,
    well-spaced waves - the stuff worth driving for.
    """
    if period_s is None:
        return 0.0
    return _interpolate(period_s, PERIOD_CURVE)


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
    strength_penalty = _interpolate(wind_speed_mph, WIND_PENALTY_CURVE)

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
# starts in 2021-10, so even pooling the full history, a "top 2% day!" claim
# rests on a record that only spans a handful of years. Rather than let a thin
# sample shout, the percentile is shrunk toward the median in proportion to
# how little evidence stands behind it.

# Rarity labels are expressed as a fraction of the HIGHEST percentile the
# shrinkage can actually produce, not as absolute percentiles.
#
# This matters because shrinkage imposes a ceiling, and that ceiling moves
# with the evidence behind the baseline. Pooling the full record now gives it
# far more days than the old seasonal window did (n=1,787 vs n=130), so the
# ceiling has moved out to p99.2 - comfortably past fixed cutoffs like p93 or
# p97. But the mechanism has to keep working when evidence is thinner than
# that: with a --no-history fallback rebuilt from a short run, or if the
# record simply started more recently, a fixed p97 cutoff could again become
# unreachable dead code - the same trap as a verdict threshold no real input
# can cross. Scaling to the attainable range keeps every label reachable at
# any n, and keeps their meaning stable regardless: "as close to the top as
# this much evidence can support".
RARITY_STANDOUT = 0.75   # >= 75% of the way from normal to the attainable max
RARITY_RARE = 0.85
RARITY_TOP = 0.95

# Laplace-style prior strength, in "virtual days". A baseline built from this
# many real days carries half the weight of its raw percentile; far fewer and
# it is pulled hard toward the median. ~30 is roughly a month's worth of days.
RARITY_PRIOR_DAYS = 30.0


def shrink_percentile(percentile: float, n_days: int, prior_days: float = RARITY_PRIOR_DAYS) -> float:
    """
    Pull a percentile toward the median (50) according to how much data backs it.

    This is Laplace's rule of succession generalised from a proportion to a
    percentile: the prior is "this was a perfectly ordinary day" (50th) and it
    carries the weight of `prior_days` virtual observations.

        shrunk = (percentile * n + 50 * prior) / (n + prior)

    With a well-populated baseline (n in the hundreds or more) a 98th-percentile reading barely
    moves, to ~90. With only 8 days of history behind it, that same reading
    lands near 60 - visible, but not shouted about. That is the intended
    behaviour: the record only reaches back to late 2021, and a rarity claim
    should never outrun its evidence.
    """
    if n_days <= 0:
        return 50.0
    return (percentile * n_days + 50.0 * prior_days) / (n_days + prior_days)


#: Swell height at which Florida beach breaks stop improving on the ABSOLUTE
#: curve in score_wave_height, which eases off above this because sandbars
#: close out rather than hold shape.
CLOSEOUT_PEAK_FT = 5.0

#: Whether rarity should inherit that rolloff. OFF by design: sigma is
#: deliberately monotonic in height, so bigger always scores better.
#:
#: The absolute surf score still applies the rolloff, so SURF and VALUE will
#: disagree on very large days - SURF reads a 12ft swell as 5.0/10 while VALUE
#: keeps rewarding it. That is intended, not a bug: the size at which a break
#: stops being fun is a matter of who is paddling out, and this setting says
#: to leave that judgement to the surfer rather than the scoring curve.
#:
#: Set True to make rarity peak near CLOSEOUT_PEAK_FT and fall away above it.
APPLY_CLOSEOUT_ROLLOFF = False


def _closeout_adjusted(height_sigma: float | None, height_ft: float | None) -> float | None:
    """
    Stop rarity rewarding swell that is too big to surf here.

    A percentile is monotonic in height by construction: the bigger the swell,
    the rarer it is, forever. That is the right answer to "how unusual is
    this?" and the wrong answer to "should I go?" - measured on the record, a
    12ft closeout scored +3.13 sigma and printed "GO. Drop everything", while
    the absolute height curve had already fallen from 10.0 to 5.0 because
    Florida sandbars cannot hold a swell that size.

    Above the peak the sigma is scaled by how far the absolute curve has
    dropped, so rarity and rideability stop disagreeing. Below it nothing
    changes - a rare 4ft day is still simply rare.
    """
    if not APPLY_CLOSEOUT_ROLLOFF:
        return height_sigma
    if height_sigma is None or height_ft is None or height_ft <= CLOSEOUT_PEAK_FT:
        return height_sigma
    quality = score_wave_height(height_ft) / 10.0
    return height_sigma * max(0.0, quality)


@dataclass
class RarityScore:
    """How unusual today's swell is against the statewide historical record."""
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
        (_closeout_adjusted(baseline.height_sigma(conditions.swell_height_ft),
                            conditions.swell_height_ft), WEIGHTS["height"]),
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

#: Minutes of driving that one standard deviation of surf is worth - the
#: exchange rate at the heart of the value score.
#:
#: Calibrated against the record rather than guessed. At 60, the single best
#: surfable day in five years (New Smyrna, 14 Sep 2023: 5.1ft at 11s, 3mph
#: offshore, effective sigma +2.63) came out at -0.29 from Tampa - it justified
#: 2.6 hours against a 2h55m drive, so the tool would have said stay home on
#: the best day it has ever seen. 120 says a one-sigma day is worth two hours,
#: which puts that day comfortably positive and matches what someone would
#: actually do.
MINUTES_PER_SIGMA = 120.0

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
    has earned an hour is scored as though it were a 1-hour drive, so it
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


# ---------------------------------------------------------------------------
# The value calculation
# ---------------------------------------------------------------------------
#
#     value = sigma(surf) - drive_minutes / MINUTES_PER_SIGMA
#
# Both terms are in the same currency: standard deviations of surf quality.
# One sigma above normal buys MINUTES_PER_SIGMA of driving, two buys twice
# that, and so on.
# Positive means the surf is worth the trip; zero is break-even; the magnitude
# is how much margin you have either way. A value of +1 means you would still
# be ahead after another hour in the car.
#
# WHY DISTANCE IS NOT ITSELF Z-SCORED
# -----------------------------------
# Standardising distance across the spot list would make it relative to
# whatever spots happen to be in the list, which destroys the absolute
# calibration this score depends on. Measured against the shipped 26 spots, a
# distance z of -1.0 is 30 miles from Daytona but 109 miles from Tampa - so
# "one sigma closer" would mean two completely different drives, and could not
# be worth a fixed number of minutes in both. Dividing raw minutes by a fixed
# minutes-per-sigma keeps the units absolute and comparable between users.
#
# This also replaces the earlier closeness/blend approach, which mixed a 0-1
# decay factor with a 0-10 rating through a tunable weight. That produced a
# number with no natural zero: you could not say what a given score meant
# without also knowing --surf-weight and --decay-miles.


@dataclass
class ValueScore:
    """The worth-the-drive calculation, in standard deviations of surf."""
    total: float | None          # sigma plus itch minus novelty minus drive cost
    sigma: float | None          # surf quality, in SDs above normal
    drive_cost: float            # drive expressed in the same units
    drive_minutes: float
    verdict: str
    #: Personal adjustments from the surf log, both in sigma. Zero when no log
    #: exists, which is the honest default - a fresh install knows nothing
    #: about the user and should not pretend otherwise.
    itch: float = 0.0            # + : longer since surfing, more willing to drive
    novelty: float = 0.0         # + : surfed here often, handicap it

    @property
    def worth_it(self) -> bool:
        return self.total is not None and self.total > 0.0

    def margin_minutes(self) -> float | None:
        """How many minutes of extra driving this day would still justify."""
        if self.total is None:
            return None
        return self.total * MINUTES_PER_SIGMA

    @property
    def has_personal(self) -> bool:
        """True when the surf log actually moved this score."""
        return abs(self.itch) > 1e-9 or abs(self.novelty) > 1e-9


def value_score(
    sigma: float | None,
    drive_minutes: float,
    minutes_per_sigma: float = MINUTES_PER_SIGMA,
    itch: float = 0.0,
    novelty: float = 0.0,
) -> ValueScore:
    """
    Score a spot as surf quality, adjusted for you, minus the cost of getting
    there:

        value = sigma + itch - novelty - drive_minutes / minutes_per_sigma

    `sigma` is how many standard deviations above normal the surf is, from
    rarity_score. It is deliberately climatological rather than relative to the
    other spots today: on a flat day every spot should score badly, which a
    cross-sectional z-score could never express because it always has a winner.

    `itch` and `novelty` come from the surf log and are zero without one. Note
    they behave differently: itch is the SAME for every spot, so it slides the
    whole board and moves the go/no-go line without reordering anything.
    Novelty is per-spot, so it genuinely reorders.
    """
    if minutes_per_sigma <= 0:
        cost = 0.0
    else:
        cost = max(0.0, drive_minutes) / minutes_per_sigma

    if sigma is None:
        return ValueScore(None, None, cost, drive_minutes,
                          "no baseline - rarity unavailable", itch, novelty)

    total = sigma + itch - novelty - cost
    return ValueScore(total, sigma, cost, drive_minutes,
                      _value_verdict(total, sigma), itch, novelty)


def _value_verdict(total: float, sigma: float) -> str:
    """Plain-English reading of a value score."""
    if total >= 2.0:
        return "GO. Drop everything"
    if total >= 1.0:
        return "Go surf - worth another hour if you had to"
    if total >= 0.25:
        return "Worth the drive"
    if total >= -0.25:
        return "Break-even - your call"
    if sigma <= -0.5:
        return "Surf isn't there today"
    return "Not worth the drive"


# How much the wind can move the value score, in sigma. The swell sigma comes
# from the historical record, which is swell-only - Open-Meteo's marine archive
# carries no wind. Without this term the value score cannot tell a glassy day
# from a blown-out one: measured, a 3ft/10s day scored identically at +1.42
# sigma whether the wind was 4mph offshore or 25mph onshore, even though the
# absolute surf score separated them by 2.5 points.
#
# Wind is directional, not a magnitude, so it has no natural z-score. Instead
# the existing 0-10 wind sub-score is re-expressed on the sigma scale: a
# perfectly groomed day is worth about an hour of extra driving, a blown-out
# one costs about the same.
WIND_SIGMA_RANGE = 1.0


def wind_sigma(wind_score: float, sigma_range: float = WIND_SIGMA_RANGE) -> float:
    """
    Convert the 0-10 wind sub-score into a sigma-scale adjustment.

    5.0 (neutral, or missing data) maps to 0, so a spot is never pushed either
    way by wind we do not know about.
    """
    return ((wind_score - 5.0) / 5.0) * sigma_range


def effective_sigma(rarity, surf) -> float | None:
    """
    The surf quality that goes into the value score.

    Swell rarity against the historical record, adjusted for today's wind.
    Returns None when there is no baseline to measure rarity against.
    """
    if rarity is None or rarity.sigma is None:
        return None
    return rarity.sigma + wind_sigma(surf.wind)


# ---------------------------------------------------------------------------
# Choosing which hour to report
# ---------------------------------------------------------------------------

@dataclass
class BestHour:
    """The pick of the forecast window for one spot."""
    time: int                    # unix UTC
    conditions: object           # the Conditions at that hour
    surf: SurfScore
    rarity: RarityScore
    sigma: float | None          # rarity + wind, the value score's input
    n_considered: int            # daylight hours actually evaluated
    #: Best hour for each local date in the window, keyed by date. Needed for a
    #: multi-day view: a spot has one overall best hour, so grouping spots by
    #: that alone would silently drop every day nothing happened to peak on.
    by_day: dict = field(default_factory=dict)


def pick_best_hour(readings, spot, baseline, is_daylight_fn, local_date_fn=None) -> BestHour | None:
    """
    Score every surfable hour in the window and return the best.

    The historical baseline is built from each day's best hour, so a single
    arbitrary hour is the wrong thing to compare against it. Scoring the window
    and taking its maximum puts both sides of the comparison in the same units,
    and it answers the more useful question: not "how is it right now" but
    "is it worth going, and when".

    `by_day` on the result carries the best hour for each local date, so a
    multi-day run can report every day rather than only the days some spot
    happened to peak on.

    Night hours are skipped for the same reason they are excluded from the
    baseline - they cannot be surfed. If the whole window is dark, the first
    reading is returned so the spot still appears rather than vanishing.
    """
    if not readings:
        return None

    best = None
    per_day: dict = {}
    considered = 0
    for ts, cond in readings:
        if not is_daylight_fn(ts, spot.lat, spot.lon):
            continue
        considered += 1
        surf = score_conditions(cond, spot)
        rare = rarity_score(cond, baseline)
        sigma = effective_sigma(rare, surf)
        key = sigma if sigma is not None else surf.total
        entry = BestHour(ts, cond, surf, rare, sigma, 0)
        if best is None or key > best[0]:
            best = (key, entry)
        if local_date_fn is not None:
            d = local_date_fn(ts, spot)
            if d not in per_day or key > per_day[d][0]:
                per_day[d] = (key, entry)

    if best is None:  # window is entirely dark
        ts, cond = readings[0]
        surf = score_conditions(cond, spot)
        rare = rarity_score(cond, baseline)
        return BestHour(ts, cond, surf, rare, effective_sigma(rare, surf), 0)

    chosen = best[1]
    return BestHour(chosen.time, chosen.conditions, chosen.surf, chosen.rarity,
                    chosen.sigma, considered,
                    {d: e for d, (_, e) in per_day.items()})


# ---------------------------------------------------------------------------
# Personal factors, from the surf log
# ---------------------------------------------------------------------------
#
# Two adjustments that no forecast can supply, both expressed in sigma so they
# trade directly against drive time like everything else:
#
#     value = sigma + itch - novelty - drive_minutes / minutes_per_sigma
#
# Neither is capped, by choice. That makes the weight the only control, so the
# constants below carry more responsibility than a bounded version would - see
# the notes on each.

#: Sigma gained per day since the last logged session. Unbounded and linear:
#: a week out of the water is worth ~42 minutes of extra driving at the default
#: 120 min/sigma, a month ~3 hours.
#:
#: Because there is no ceiling, this grows without limit if the log goes stale.
#: That is the intended behaviour for a real dry spell, but it is also the
#: failure mode of forgetting to log: after a year of unlogged sessions the
#: tool would insist on an 18-hour drive. `days_since_last` returning None for
#: an empty log is deliberately treated as zero itch rather than infinite.
ITCH_RATE_PER_DAY = 0.05

#: Multiplier on the visit z-score. Unbounded, so a heavily-surfed spot can end
#: up several sigma down: measured on a realistic 60-session log this puts the
#: most-visited spot ~1.4 sigma (about 2h45m of driving) behind an unvisited
#: one at the default weight.
NOVELTY_WEIGHT = 0.5

#: Laplace-style prior, in "virtual sessions", damping the novelty z-score when
#: the log is young. Same mechanism as RARITY_PRIOR_DAYS: with three sessions
#: recorded your history cannot support a strong claim about your preferences,
#: so it barely moves anything. This is a confidence weight, not a cap - it
#: scales the z toward zero on thin evidence but never bounds it once the log
#: is mature.
NOVELTY_PRIOR_SESSIONS = 20.0


def itch_bonus(days_since_last: int | None,
               rate_per_day: float = ITCH_RATE_PER_DAY) -> float:
    """
    Sigma earned by not having surfed lately. Linear and unbounded.

    None (an empty log) means "unknown", not "forever", and returns zero: a
    fresh install should not open by insisting the user drive across the state.
    """
    if days_since_last is None or days_since_last <= 0:
        return 0.0
    return days_since_last * rate_per_day


def novelty_penalties(
    visit_counts: dict,
    weight: float = NOVELTY_WEIGHT,
    prior_sessions: float = NOVELTY_PRIOR_SESSIONS,
) -> dict:
    """
    Per-spot handicap from how often each spot has been surfed, as a z-score
    across the whole spot list. Positive means "surfed more than typical, make
    it work harder"; negative is a small bonus for the neglected ones.

    Two corrections to a plain z-score of the raw counts, both for measured
    reasons rather than taste:

    1. LOG FIRST. Visit counts across 41 spots are extremely zero-inflated -
       most spots are zero and a few are not - so a raw z-score treats any
       nonzero entry as an extreme outlier. Measured on a one-session log, the
       raw z of that single visit is +6.3, which at 120 min/sigma is a
       twelve-hour handicap from one surf. This is the same right-skew problem
       that made raw z-scores wrong for swell height, and it has the same fix.

    2. SHRINK BY EVIDENCE. Even in log space, a young log produces large z
       values simply because almost everything is zero. Scaling by
       n/(n + prior) means a three-session log barely registers while a
       two-hundred-session one applies at nearly full strength.

    The result is deliberately NOT bounded: once the log is mature the z is
    passed through at `weight`, however large it gets.
    """
    names = list(visit_counts)
    if not names:
        return {}

    counts = [float(visit_counts[n]) for n in names]
    total = sum(counts)
    if total <= 0:
        return {n: 0.0 for n in names}

    # Plain-Python stats: scoring.py has no numpy dependency and stays that way.
    logged = [math.log1p(c) for c in counts]
    mean = sum(logged) / len(logged)
    variance = sum((x - mean) ** 2 for x in logged) / len(logged)
    sd = math.sqrt(variance)

    # Tolerance, not `sd <= 0`. When every spot has the same count the spread
    # is mathematically zero, but summing N identical floats and dividing
    # leaves the mean off by ~1e-16, so sd lands near 1e-16 rather than on it -
    # on some platforms and not others. Dividing by that produces z = +-1.0
    # from pure rounding noise. Measured: 13 of the first 199 possible flat
    # counts trip this locally, and CI caught a case that this machine did not.
    if sd <= 1e-9 * max(1.0, abs(mean)):
        return {n: 0.0 for n in names}

    confidence = total / (total + prior_sessions)
    return {
        n: weight * ((logged[i] - mean) / sd) * confidence
        for i, n in enumerate(names)
    }

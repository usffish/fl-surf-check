"""
Tests for the scoring math. All pure functions - no network required.
"""

import math

import pytest

from fl_surf_check.distance import closeness_factor, straight_line_miles
from fl_surf_check.scoring import (
    angular_difference,
    score_swell_direction,
    score_wave_height,
    score_wave_period,
    score_wind,
    worth_the_drive,
)
from fl_surf_check.spots import SPOTS


# --------------------------------------------------------------------------
# Wave height
# --------------------------------------------------------------------------

def test_flat_scores_near_zero():
    assert score_wave_height(0.0) < 0.5


def test_height_score_peaks_in_florida_sweet_spot():
    """4ft should beat both 1ft and 12ft on a FL beach break."""
    assert score_wave_height(4.0) > score_wave_height(1.0)
    assert score_wave_height(4.0) > score_wave_height(12.0)


def test_height_score_is_monotonic_up_to_peak():
    prev = -1.0
    for h in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]:
        cur = score_wave_height(h)
        assert cur >= prev, f"score dropped at {h}ft"
        prev = cur


def test_height_score_bounded():
    for h in [0, 0.1, 3, 5, 8, 15, 40]:
        assert 0.0 <= score_wave_height(h) <= 10.0


def test_missing_height_is_zero():
    assert score_wave_height(None) == 0.0


def test_interpolation_is_continuous():
    """Nearly-equal inputs must produce nearly-equal scores (no bucket cliffs)."""
    a, b = score_wave_height(2.49), score_wave_height(2.51)
    assert abs(a - b) < 0.2


# --------------------------------------------------------------------------
# Period
# --------------------------------------------------------------------------

def test_groundswell_beats_windswell():
    assert score_wave_period(14.0) > score_wave_period(5.0)


def test_period_score_monotonic():
    prev = -1.0
    for p in [2, 4, 6, 8, 10, 12, 14, 18]:
        cur = score_wave_period(p)
        assert cur >= prev, f"period score dropped at {p}s"
        prev = cur


def test_period_bounded_and_capped():
    assert score_wave_period(30.0) == 10.0
    assert 0.0 <= score_wave_period(0.0) <= 10.0


# --------------------------------------------------------------------------
# Wind
# --------------------------------------------------------------------------

def test_angular_difference_wraps_around_north():
    assert angular_difference(350, 10) == pytest.approx(20)
    assert angular_difference(10, 350) == pytest.approx(20)
    assert angular_difference(0, 180) == pytest.approx(180)
    assert angular_difference(90, 90) == pytest.approx(0)


def test_offshore_beats_onshore_at_same_speed():
    offshore_deg = 270.0
    offshore = score_wind(12.0, 270.0, offshore_deg)
    onshore = score_wind(12.0, 90.0, offshore_deg)
    assert offshore > onshore


def test_glassy_light_wind_scores_well_regardless_of_direction():
    """This is the key refinement over the original algorithm."""
    light_onshore = score_wind(2.0, 90.0, 270.0)
    assert light_onshore >= 8.0


def test_howling_offshore_is_worse_than_light_offshore():
    """Direction alone shouldn't win - 35mph offshore is unrideable."""
    assert score_wind(35.0, 270.0, 270.0) < score_wind(8.0, 270.0, 270.0)


def test_wind_score_bounded():
    for speed in [0, 5, 12, 25, 40, 70]:
        for direction in [0, 45, 90, 180, 270, 359]:
            s = score_wind(speed, direction, 270.0)
            assert 0.0 <= s <= 10.0


def test_missing_wind_is_neutral_not_punitive():
    assert score_wind(None, None, 270.0) == 5.0


# --------------------------------------------------------------------------
# Swell direction
# --------------------------------------------------------------------------

def test_straight_on_swell_beats_glancing():
    assert score_swell_direction(90.0, 90.0) > score_swell_direction(150.0, 90.0)


def test_swell_from_behind_the_beach_is_blocked():
    assert score_swell_direction(270.0, 90.0) < 1.0


def test_swell_direction_bounded():
    for d in range(0, 360, 15):
        assert 0.0 <= score_swell_direction(d, 90.0) <= 10.0


# --------------------------------------------------------------------------
# Distance & the worth-the-drive blend
# --------------------------------------------------------------------------

def test_closeness_is_one_at_zero_distance():
    assert closeness_factor(0, 75) == pytest.approx(1.0)


def test_closeness_decays_to_1_over_e_at_decay_distance():
    assert closeness_factor(75, 75) == pytest.approx(1 / math.e, rel=1e-6)


def test_closeness_is_monotonically_decreasing():
    prev = 2.0
    for miles in [0, 10, 25, 50, 100, 200, 400]:
        cur = closeness_factor(miles, 75)
        assert cur < prev
        prev = cur


def test_closer_spot_wins_when_surf_is_equal():
    near = worth_the_drive(7.0, 10, closeness_factor(10, 75))
    far = worth_the_drive(7.0, 200, closeness_factor(200, 75))
    assert near.total > far.total


def test_epic_far_beats_mediocre_close():
    """The whole point of a blend rather than a hard cutoff."""
    epic_far = worth_the_drive(9.5, 120, closeness_factor(120, 75))
    meh_close = worth_the_drive(3.0, 5, closeness_factor(5, 75))
    assert epic_far.total > meh_close.total


def test_surf_weight_one_ignores_distance_entirely():
    near = worth_the_drive(6.0, 5, closeness_factor(5, 75), surf_weight=1.0)
    far = worth_the_drive(6.0, 300, closeness_factor(300, 75), surf_weight=1.0)
    assert near.total == pytest.approx(far.total)


def test_surf_weight_zero_ranks_purely_by_distance():
    good_far = worth_the_drive(10.0, 300, closeness_factor(300, 75), surf_weight=0.0)
    bad_near = worth_the_drive(0.0, 5, closeness_factor(5, 75), surf_weight=0.0)
    assert bad_near.total > good_far.total


def test_worth_score_always_bounded():
    for surf in [0, 2.5, 5, 7.5, 10]:
        for miles in [0, 30, 100, 500]:
            w = worth_the_drive(surf, miles, closeness_factor(miles, 75))
            assert 0.0 <= w.total <= 10.0


def test_flat_surf_always_says_stay_home():
    for miles in [1, 50, 300]:
        assert "stay home" in worth_the_drive(1.0, miles, closeness_factor(miles, 75)).verdict.lower()


def test_epic_but_distant_verdict_acknowledges_the_drive():
    """A 9/10 day 300 miles away shouldn't read the same as one 10 miles away."""
    near = worth_the_drive(9.0, 10, closeness_factor(10, 75)).verdict
    far = worth_the_drive(9.0, 300, closeness_factor(300, 75)).verdict
    assert near != far
    assert "road trip" in far.lower()


# --------------------------------------------------------------------------
# Spot data integrity
# --------------------------------------------------------------------------

def test_all_spots_have_sane_florida_coordinates():
    for s in SPOTS:
        assert 24.0 < s.lat < 31.5, f"{s.name} latitude outside Florida"
        assert -88.0 < s.lon < -79.5, f"{s.name} longitude outside Florida"


def test_all_bearings_and_stations_valid():
    for s in SPOTS:
        assert 0 <= s.facing_deg < 360, f"{s.name} has an invalid facing bearing"
        assert s.offshore_deg == (s.facing_deg + 180) % 360
        assert s.tide_station.isdigit() and len(s.tide_station) == 7, \
            f"{s.name} has a malformed NOAA station id"


def test_spot_names_are_unique():
    names = [s.name for s in SPOTS]
    assert len(names) == len(set(names))


def test_distance_between_known_spots_is_plausible():
    """Cocoa Beach to Sebastian Inlet is roughly 35 miles as the crow flies."""
    cocoa = next(s for s in SPOTS if "Cocoa" in s.name)
    sebastian = next(s for s in SPOTS if "Sebastian" in s.name)
    miles = straight_line_miles(cocoa.lat, cocoa.lon, sebastian.lat, sebastian.lon)
    assert 25 < miles < 45

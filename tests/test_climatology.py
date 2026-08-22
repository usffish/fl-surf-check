"""
Tests for the historical baseline and the rarity score built on it.

Everything here is offline. `build_baseline` takes an injectable client, so the
one test that exercises the fetch path feeds it a fake rather than reaching
Open-Meteo - which matters doubly here, since the real history request is large
enough to trip the API's per-minute rate limit.
"""

import datetime as dt
import json
import math

import numpy as np
import pytest

from fl_surf_check import climatology
from fl_surf_check.climatology import Baseline, build_baseline, load_baseline
from fl_surf_check.conditions import Conditions
from fl_surf_check.scoring import (
    MAX_ALLOWANCE_MINUTES,
    RARITY_RARE,
    RARITY_STANDOUT,
    RARITY_TOP,
    THUNDERSTORM_CODES,
    drive_allowance_minutes,
    effective_drive,
    rarity_score,
    shrink_percentile,
    storm_blocks_travel,
    storm_risk,
    storm_warning,
)
from fl_surf_check.spots import SPOTS


def _linear_baseline(n_days=130, n_years=5):
    """A baseline whose percentile curves are exactly linear, so ranks are predictable."""
    return Baseline(
        height_p=tuple(float(q) / 10.0 for q in range(101)),   # 0.0 -> 10.0 ft
        period_p=tuple(4.0 + float(q) / 10.0 for q in range(101)),  # 4 -> 14 s
        n_days=n_days,
        n_years=n_years,
        n_observations=n_days * 26,
        n_spots=26,
        # Geometric stats consistent with the linear curves above: a ~5ft
        # geometric mean with a log SD that puts 10ft near +1.2 SD.
        log_height_mean=math.log(5.0),
        log_height_sd=0.6,
        log_period_mean=math.log(9.0),
        log_period_sd=0.25,
    )


# --- percentile inversion ---------------------------------------------------

def test_percentile_of_is_monotonic_and_bounded():
    b = _linear_baseline()
    ranks = [b.height_percentile(v) for v in np.arange(0.0, 10.5, 0.25)]
    assert all(0.0 <= r <= 100.0 for r in ranks)
    assert ranks == sorted(ranks), "percentile rank must never decrease as the value rises"


def test_percentile_of_recovers_known_points():
    b = _linear_baseline()
    assert b.height_percentile(5.0) == pytest.approx(50.0, abs=1.0)
    assert b.height_percentile(9.0) == pytest.approx(90.0, abs=1.0)


def test_percentile_of_clamps_outside_the_record():
    """A swell bigger than anything on record is p100, not an error or overflow."""
    b = _linear_baseline()
    assert b.height_percentile(-5.0) == 0.0
    assert b.height_percentile(999.0) == 100.0
    assert b.height_percentile(None) is None


# --- Laplace-style shrinkage ------------------------------------------------

def test_shrinkage_pulls_thin_evidence_toward_normal():
    """
    The movie engine's core intuition: a spectacular reading backed by almost
    no data should not be trusted. p98 from 3 days must land far below p98
    from 1000 days.
    """
    thin = shrink_percentile(98.0, n_days=3)
    thick = shrink_percentile(98.0, n_days=1000)
    assert thin < 60, "3 days of evidence should not support a p98 claim"
    assert thick > 95, "1000 days of evidence should nearly preserve it"
    assert thin < thick


def test_shrinkage_is_monotonic_in_evidence():
    vals = [shrink_percentile(95.0, n) for n in (1, 5, 20, 100, 500, 5000)]
    assert vals == sorted(vals)


def test_shrinkage_returns_median_with_no_evidence():
    assert shrink_percentile(99.0, n_days=0) == 50.0


def test_shrinkage_never_escapes_the_input_range():
    for p in (0.0, 25.0, 50.0, 75.0, 100.0):
        for n in (1, 10, 130, 10_000):
            out = shrink_percentile(p, n)
            assert min(p, 50.0) - 1e-9 <= out <= max(p, 50.0) + 1e-9


# --- rarity labels ----------------------------------------------------------

def test_every_rarity_label_is_reachable_at_a_realistic_sample_size():
    """
    Regression: labels were once fixed percentiles (>=93, >=97) while shrinkage
    capped the achievable value at ~p91 for n=130, making the top two labels
    dead code no input could ever produce. Thresholds are now relative to the
    attainable ceiling, so each must be reachable.
    """
    b = _linear_baseline(n_days=130)
    labels = set()
    for h in np.arange(0.0, 10.1, 0.05):
        for p_ in (4.0, 9.0, 14.0):
            r = rarity_score(Conditions(swell_height_ft=float(h), swell_period_s=p_), b)
            labels.add(r.label())
    for expected in ("STANDOUT", "RARE", "BEST IN 5YR", "above normal", "below normal"):
        assert expected in labels, f"{expected!r} is unreachable - dead threshold"


def test_rarity_thresholds_are_ordered():
    assert RARITY_STANDOUT < RARITY_RARE < RARITY_TOP


def test_an_average_day_is_not_a_standout():
    b = _linear_baseline()
    r = rarity_score(Conditions(swell_height_ft=5.0, swell_period_s=9.0), b)
    assert not r.is_standout
    assert r.label() == ""


def test_a_record_day_is_a_standout():
    b = _linear_baseline()
    r = rarity_score(Conditions(swell_height_ft=10.0, swell_period_s=14.0), b)
    assert r.is_standout and r.label() == "BEST IN 5YR"


# --- dynamic denominator (mirrors the movie engine's rule) ------------------

def test_missing_period_drops_out_rather_than_scoring_zero():
    """
    A missing signal must be dropped from numerator AND denominator, not
    zero-filled - otherwise absent data reads as terrible data.
    """
    b = _linear_baseline()
    both = rarity_score(Conditions(swell_height_ft=9.0, swell_period_s=13.0), b)
    height_only = rarity_score(Conditions(swell_height_ft=9.0, swell_period_s=None), b)
    assert height_only.percentile is not None
    assert height_only.percentile == pytest.approx(shrink_percentile(90.0, b.n_days), abs=1.0)
    assert both.percentile is not None


def test_no_data_at_all_yields_no_rarity():
    b = _linear_baseline()
    r = rarity_score(Conditions(), b)
    assert r.percentile is None and r.label() == ""


def test_missing_baseline_is_survivable():
    r = rarity_score(Conditions(swell_height_ft=3.0, swell_period_s=10.0), None)
    assert r.percentile is None
    assert not r.is_standout


# --- build_baseline against a fake client -----------------------------------

class _FakeHourly:
    def __init__(self, series, t0, interval):
        self._series, self._t0, self._iv = series, t0, interval

    def Variables(self, i):
        class _V:
            def __init__(self, a): self._a = a
            def ValuesAsNumpy(self): return self._a
        return _V(self._series[i])

    def Time(self): return self._t0
    def Interval(self): return self._iv


class _FakeResponse:
    def __init__(self, hourly): self._h = hourly
    def Hourly(self): return self._h


class _FakeClient:
    """Serves a deterministic year-round hourly series for every spot."""
    def __init__(self, n_spots, days=800):
        self.n_spots, self.days = n_spots, days
        self.calls = 0

    def weather_api(self, url, params=None):
        self.calls += 1
        hours = self.days * 24
        t0 = int(dt.datetime(2021, 10, 1, tzinfo=dt.timezone.utc).timestamp())
        idx = np.arange(hours)
        height = 2.0 + np.sin(idx / 97.0)          # oscillates 1-3 ft
        period = 8.0 + np.cos(idx / 131.0)         # oscillates 7-9 s
        return [
            _FakeResponse(_FakeHourly([height, period], t0, 3600))
            for _ in range(self.n_spots)
        ]


def test_build_baseline_pools_all_spots_into_one_distribution():
    spots = list(SPOTS)
    client = _FakeClient(len(spots))
    b = build_baseline(spots, target_date=dt.date(2022, 6, 15), client=client)

    assert client.calls == 1, "history must be ONE batched request, not one per spot"
    assert b is not None
    assert b.n_spots == len(spots)
    # Pooled spot-days should far exceed distinct days - that is what pooling means.
    assert b.n_observations > b.n_days
    assert b.n_observations == pytest.approx(b.n_days * len(spots), rel=0.05)


def test_build_baseline_counts_distinct_days_not_spot_days_as_evidence():
    """
    All 26 spots see the same Atlantic swell, so one date is ~one independent
    observation, not 26. Shrinkage must use the conservative count.
    """
    spots = list(SPOTS)
    b = build_baseline(spots, target_date=dt.date(2022, 6, 15), client=_FakeClient(len(spots)))
    # +/-14 days over ~2 seasons of fake data: tens of days, not thousands.
    assert b.n_days < 100
    assert b.n_observations > 1000


def test_percentile_curves_are_sorted():
    b = build_baseline(list(SPOTS), target_date=dt.date(2022, 6, 15),
                       client=_FakeClient(len(SPOTS)))
    assert list(b.height_p) == sorted(b.height_p)
    assert list(b.period_p) == sorted(b.period_p)


# --- disk cache -------------------------------------------------------------

def test_cache_round_trips_and_avoids_a_second_fetch(tmp_path):
    spots = list(SPOTS)
    path = str(tmp_path / "clim.json")
    client = _FakeClient(len(spots))
    target = dt.date.today()

    first = load_baseline(spots, path=path, target_date=target, client=client)
    assert first is not None and client.calls == 1

    second = load_baseline(spots, path=path, target_date=target, client=client)
    assert client.calls == 1, "a fresh cache must not re-fetch"
    assert second.height_p == first.height_p
    assert second.n_days == first.n_days


def test_stale_cache_triggers_a_rebuild(tmp_path):
    spots = list(SPOTS)
    path = str(tmp_path / "clim.json")
    client = _FakeClient(len(spots))
    target = dt.date.today()

    load_baseline(spots, path=path, target_date=target, client=client)
    with open(path) as fh:
        raw = json.load(fh)
    raw["meta"]["built"] = (dt.date.today() - dt.timedelta(days=999)).isoformat()
    with open(path, "w") as fh:
        json.dump(raw, fh)

    load_baseline(spots, path=path, target_date=target, client=client)
    assert client.calls == 2


def test_cache_is_rebuilt_when_the_season_moves(tmp_path):
    """The baseline is seasonal, so a different target date must not reuse it."""
    spots = list(SPOTS)
    path = str(tmp_path / "clim.json")
    client = _FakeClient(len(spots))

    load_baseline(spots, path=path, target_date=dt.date(2022, 6, 15), client=client)
    load_baseline(spots, path=path, target_date=dt.date(2022, 12, 15), client=client)
    assert client.calls == 2


def test_corrupt_cache_does_not_crash(tmp_path):
    path = str(tmp_path / "clim.json")
    with open(path, "w") as fh:
        fh.write("{not json at all")
    client = _FakeClient(len(SPOTS))
    assert load_baseline(list(SPOTS), path=path, client=client) is not None


def test_network_failure_degrades_to_no_baseline(tmp_path):
    class _Boom:
        def weather_api(self, *a, **k):
            raise RuntimeError("open-meteo is down")

    out = load_baseline(list(SPOTS), path=str(tmp_path / "c.json"), client=_Boom())
    assert out is None, "the tool must still rank spots without a baseline"


def test_history_start_matches_measured_coverage():
    """Open-Meteo serves no marine data before this date; guard the constant."""
    assert climatology.HISTORY_START == "2021-10-01"
    assert climatology.HISTORY_VARS == ("swell_wave_height", "swell_wave_period")


# --- earning drive time with exceptional conditions -------------------------

def test_one_sigma_buys_roughly_the_configured_minutes():
    """The headline rule: +1 SD above normal is worth ~60 more minutes."""
    # n_days large enough that the evidence damping is negligible.
    got = drive_allowance_minutes(1.0, n_days=100_000, minutes_per_sigma=60.0)
    assert got == pytest.approx(60.0, rel=0.01)
    assert drive_allowance_minutes(2.0, n_days=100_000) == pytest.approx(120.0, rel=0.01)


def test_allowance_scales_linearly_with_sigma():
    vals = [drive_allowance_minutes(z, n_days=100_000) for z in (0.5, 1.0, 1.5, 2.0)]
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    assert all(d == pytest.approx(diffs[0], rel=0.01) for d in diffs)


def test_below_normal_days_earn_nothing_but_are_not_penalised():
    """A poor day should cost its real drive time - no more, no less."""
    for z in (-0.1, -1.0, -3.0):
        assert drive_allowance_minutes(z, n_days=130) == 0.0


def test_allowance_is_damped_by_thin_evidence():
    """
    A big sigma from a thin baseline must not send anyone on a long drive.
    Same Laplace-style weighting as the rarity percentile.
    """
    thin = drive_allowance_minutes(3.0, n_days=2)
    thick = drive_allowance_minutes(3.0, n_days=100_000)
    assert thin < thick / 3
    assert thick > 170


def test_allowance_is_capped():
    """Even an absurd reading cannot justify an unbounded drive."""
    assert drive_allowance_minutes(50.0, n_days=100_000) == MAX_ALLOWANCE_MINUTES


def test_allowance_is_zero_without_a_sigma():
    assert drive_allowance_minutes(None, n_days=130) == 0.0


def test_minutes_per_sigma_zero_disables_the_feature():
    assert drive_allowance_minutes(3.0, n_days=130, minutes_per_sigma=0.0) == 0.0


def test_effective_drive_discounts_time_and_distance_together():
    """A 2h/100mi drive with a 1h allowance is scored as 1h/50mi."""
    miles, minutes = effective_drive(100.0, 120.0, 60.0)
    assert minutes == pytest.approx(60.0)
    assert miles == pytest.approx(50.0)


def test_effective_drive_preserves_each_route_average_speed():
    """
    Highway and surface routes cover different ground per minute; discounting
    miles by the same fraction as minutes keeps that difference intact.
    """
    fast_mi, fast_min = effective_drive(120.0, 120.0, 60.0)   # 60 mph route
    slow_mi, slow_min = effective_drive(60.0, 120.0, 60.0)    # 30 mph route
    assert fast_min == slow_min == pytest.approx(60.0)
    assert fast_mi / fast_min == pytest.approx(120.0 / 120.0)
    assert slow_mi / slow_min == pytest.approx(60.0 / 120.0)


def test_effective_drive_never_goes_negative():
    miles, minutes = effective_drive(10.0, 15.0, 600.0)
    assert minutes == 0.0 and miles == 0.0


def test_effective_drive_is_a_no_op_without_an_allowance():
    assert effective_drive(80.0, 95.0, 0.0) == (80.0, 95.0)
    assert effective_drive(80.0, 0.0, 60.0) == (80.0, 0.0)


def test_sigma_uses_log_space_not_raw_values():
    """
    Swell height is strongly right-skewed (measured skew 1.95 on the real
    record), so sigma is computed geometrically. The check: a value at the
    geometric mean must be sigma 0, which a raw-value z-score would not give.
    """
    b = _linear_baseline()
    assert b.height_sigma(5.0) == pytest.approx(0.0, abs=1e-9)
    assert b.height_sigma(5.0 * math.e ** 0.6) == pytest.approx(1.0, abs=1e-9)
    assert b.height_sigma(5.0 / math.e ** 0.6) == pytest.approx(-1.0, abs=1e-9)


def test_sigma_is_none_without_a_usable_baseline():
    flat = Baseline(height_p=(1.0,) * 101, period_p=(8.0,) * 101,
                    n_days=10, n_years=1)  # no log stats -> sd 0
    assert flat.height_sigma(3.0) is None
    r = rarity_score(Conditions(swell_height_ft=3.0, swell_period_s=10.0), flat)
    assert drive_allowance_minutes(r.sigma, r.n_days) == 0.0


def test_a_big_day_earns_more_drive_time_than_an_average_one():
    b = _linear_baseline()
    avg = rarity_score(Conditions(swell_height_ft=5.0, swell_period_s=9.0), b)
    big = rarity_score(Conditions(swell_height_ft=10.0, swell_period_s=13.0), b)
    assert drive_allowance_minutes(big.sigma, big.n_days) > \
           drive_allowance_minutes(avg.sigma, avg.n_days)


# --- thunderstorms ----------------------------------------------------------
#
# Rain is surfable; lightning is not. These pin the gate that keeps great surf
# from talking anyone into a drive toward a storm.

def _c(**kw):
    return Conditions(**kw)


def test_observed_thunderstorm_code_is_active():
    for code in (95, 96, 99):
        assert storm_risk(_c(weather_code=code, cape_j_kg=0.0)) == "active"


def test_rain_alone_is_not_a_storm():
    """Explicitly: you can surf in the rain. Drizzle and showers must not flag."""
    for code in (51, 53, 61, 63, 65, 80, 81, 82):
        assert storm_risk(_c(weather_code=code, cape_j_kg=200.0,
                             precip_probability=90.0)) == "none"


def test_instability_without_precipitation_is_not_a_storm():
    """High CAPE on a dry day is a loaded gun with no trigger."""
    assert storm_risk(_c(weather_code=0, cape_j_kg=3500.0, precip_probability=5.0)) == "none"


def test_precipitation_without_instability_is_not_a_storm():
    assert storm_risk(_c(weather_code=61, cape_j_kg=100.0, precip_probability=95.0)) == "none"


def test_strong_instability_with_rain_chance_is_likely():
    assert storm_risk(_c(weather_code=3, cape_j_kg=3000.0, precip_probability=60.0)) == "likely"


def test_moderate_instability_with_rain_chance_is_possible():
    assert storm_risk(_c(weather_code=3, cape_j_kg=1500.0, precip_probability=60.0)) == "possible"


def test_missing_forecast_fields_report_unknown_not_none():
    """'no data' must be distinguishable from 'no storm'."""
    assert storm_risk(_c()) == "unknown"


def test_storm_forfeits_the_drive_allowance():
    assert storm_blocks_travel("active")
    assert storm_blocks_travel("likely")
    assert not storm_blocks_travel("possible")
    assert not storm_blocks_travel("none")
    assert not storm_blocks_travel("unknown")


def test_only_real_risks_produce_a_warning():
    assert storm_warning("active")
    assert storm_warning("likely")
    assert storm_warning("possible")
    assert storm_warning("none") == ""
    assert storm_warning("unknown") == ""


def test_lightning_cancels_the_bonus_for_an_otherwise_epic_day(capsys):
    """
    End to end: a huge swell that would normally earn hours of extra driving
    must earn nothing when there is lightning at the spot.
    """
    b = _linear_baseline()
    epic = _c(swell_height_ft=10.0, swell_period_s=13.0)
    r = rarity_score(epic, b)
    earned = drive_allowance_minutes(r.sigma, r.n_days)
    assert earned > 30, "an epic day should normally earn real drive time"

    stormy = _c(swell_height_ft=10.0, swell_period_s=13.0,
                weather_code=95, cape_j_kg=3000.0, precip_probability=80.0)
    assert storm_blocks_travel(storm_risk(stormy))


def test_historical_archive_cannot_supply_storm_data():
    """
    Documents why the baseline is not storm-filtered: ERA5 emits no
    thunderstorm codes, so any historical filter would be a no-op built on
    absent data. Guards the constants the reasoning depends on.
    """
    assert THUNDERSTORM_CODES == {95, 96, 99}
    assert "cape" not in climatology.HISTORY_VARS
    assert "weather_code" not in climatology.HISTORY_VARS

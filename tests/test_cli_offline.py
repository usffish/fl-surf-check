"""
End-to-end test of the CLI with every network call mocked out.

This exercises the real ranking, filtering and rendering code paths without
touching Open-Meteo, NOAA or OSRM - so it runs in CI, offline, and instantly.
"""

import datetime as dt
import math
import random
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from fl_surf_check import cli, conditions
from fl_surf_check.conditions import Conditions
from fl_surf_check.distance import DriveEstimate, straight_line_miles
from fl_surf_check.climatology import Baseline
from fl_surf_check.location import Origin
from fl_surf_check.spots import SPOTS

ORIGIN = Origin(29.2108, -81.0228, "Daytona Beach, FL 32118", "pgeocode")


def _fake_marine(spots, hours_ahead=24):
    """
    A full 24h window per spot, so best-hour selection is actually exercised
    and the fixture does not depend on what time the suite happens to run.
    """
    rng = random.Random(7)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    out = {}
    for s in spots:
        h = round(rng.uniform(0.4, 6.5), 1)
        p_ = round(rng.uniform(4, 14), 1)
        d = round(rng.uniform(40, 140))
        readings = []
        for k in range(24):
            readings.append((now + k * 3600, Conditions(
                wave_height_ft=h, wave_period_s=p_, wave_direction_deg=d,
                swell_height_ft=h, swell_period_s=p_, swell_direction_deg=d,
                wind_speed_mph=round(rng.uniform(1, 22)),
                wind_direction_deg=round(rng.uniform(0, 359)),
            )))
        out[s.name] = readings
    return out


def _fake_tide(station, tz="America/New_York", session=None, hours=48):
    return "rising", "High 14:32"


def _fake_baseline(spots, **kwargs):
    """
    One deterministic statewide baseline; never touches the network.

    The log-space stats are NOT optional decoration: without them
    Baseline.height_sigma returns None, effective_sigma returns None, and the
    whole value score collapses to None - silently falling back to the legacy
    blend and leaving the real ranking path untested. They are set to values
    consistent with the percentile curves above.
    """
    return Baseline(
        height_p=tuple(0.2 + 0.06 * q for q in Baseline.LEVELS),   # 0.2ft -> 6.2ft
        period_p=tuple(3.0 + 0.09 * q for q in Baseline.LEVELS),   # 3s -> 12s
        n_days=145,
        n_years=5,
        n_observations=145 * 41,
        n_spots=41,
        log_height_mean=math.log(1.5),
        log_height_sd=0.62,
        log_period_mean=math.log(7.2),
        log_period_sd=0.32,
    )


def _fake_drive(olat, olon, dlat, dlon, timeout=8.0, session=None):
    miles = straight_line_miles(olat, olon, dlat, dlon) * 1.25
    return DriveEstimate(miles, miles / 50 * 60, "osrm")


@pytest.fixture
def offline():
    with mock.patch.object(cli, "fetch_marine_and_wind", _fake_marine), \
         mock.patch.object(cli, "fetch_tide", _fake_tide), \
         mock.patch.object(cli, "get_drive_estimate", _fake_drive), \
         mock.patch.object(cli, "load_baseline", _fake_baseline), \
         mock.patch.object(cli, "geocode_zip", lambda z: ORIGIN):
        yield


def test_cli_runs_and_exits_clean(offline, capsys):
    assert cli.main(["--zip", "32118", "--top", "8"]) == 0
    out = capsys.readouterr().out
    assert "Florida surf check" in out
    assert "VERDICT" in out


def test_top_n_limits_rows(offline, capsys):
    cli.main(["--zip", "32118", "--top", "5"])
    out = capsys.readouterr().out
    assert "5 spots shown" in out


def test_details_flag_shows_breakdown(offline, capsys):
    cli.main(["--zip", "32118", "--top", "2", "--details"])
    out = capsys.readouterr().out
    assert "height" in out and "period" in out and "wind" in out


def test_max_miles_excludes_distant_spots(offline, capsys):
    cli.main(["--zip", "32118", "--top", "0", "--max-miles", "30"])
    out = capsys.readouterr().out
    # Miami is ~250mi from Daytona and must not appear
    assert "South Beach" not in out


def test_min_score_filters(offline, capsys):
    cli.main(["--zip", "32118", "--top", "0", "--min-score", "9.9"])
    out = capsys.readouterr().out
    assert "No spots matched" in out or "spots shown" in out


def test_results_are_sorted_by_value_descending(offline):
    """
    Ranking is on the value score, not the legacy blend. This test used to
    assert `worth` was descending and passed only because the fixture's
    baseline lacked log stats, so every sigma was None and the code silently
    fell back to that blend.
    """
    args = cli.build_parser().parse_args(["--zip", "32118", "--top", "0"])
    rows, _ = cli.gather(ORIGIN, args)
    rows = cli.filter_and_sort(rows, args)
    totals = [r["value"].total for r in rows]
    assert all(t is not None for t in totals), "value score should be live here"
    assert totals == sorted(totals, reverse=True)


def test_surf_weight_only_affects_the_no_history_fallback(offline):
    """
    --surf-weight belongs to the legacy blend, which now only runs when there
    is no baseline. With one present it must NOT change the ranking - the
    value score does not consult it.
    """
    base = cli.build_parser().parse_args(["--zip", "32118", "--top", "0"])
    weighted = cli.build_parser().parse_args(
        ["--zip", "32118", "--top", "0", "--surf-weight", "1.0"])
    rows_a, _ = cli.gather(ORIGIN, base)
    rows_b, _ = cli.gather(ORIGIN, weighted)
    a = [r["spot"].name for r in cli.filter_and_sort(rows_a, base)]
    b = [r["spot"].name for r in cli.filter_and_sort(rows_b, weighted)]
    assert a == b, "surf-weight should be inert while a baseline exists"


def test_minutes_per_sd_changes_the_ranking(offline):
    """The live equivalent: the exchange rate does move the order."""
    base = cli.build_parser().parse_args(["--zip", "32118", "--top", "0"])
    generous = cli.build_parser().parse_args(
        ["--zip", "32118", "--top", "0", "--minutes-per-sd", "600"])
    rows_a, _ = cli.gather(ORIGIN, base)
    rows_b, _ = cli.gather(ORIGIN, generous)
    a = [r["spot"].name for r in cli.filter_and_sort(rows_a, base)]
    b = [r["spot"].name for r in cli.filter_and_sort(rows_b, generous)]
    assert a != b, "drive-time exchange rate had no effect on ordering"


def test_invalid_surf_weight_rejected():
    assert cli.main(["--zip", "32118", "--surf-weight", "5"]) == 2


def test_missing_data_does_not_crash(capsys):
    """Every API can fail; the CLI must still produce output."""
    empty = lambda spots, hours_ahead=24: {
        s.name: [(0, Conditions(errors=("all sources down",)))] for s in spots
    }
    with mock.patch.object(cli, "fetch_marine_and_wind", empty), \
         mock.patch.object(cli, "fetch_tide", lambda st, tz="America/New_York", session=None, hours=48: (None, None)), \
         mock.patch.object(cli, "get_drive_estimate", _fake_drive), \
         mock.patch.object(cli, "load_baseline", _fake_baseline), \
         mock.patch.object(cli, "geocode_zip", lambda z: ORIGIN):
        assert cli.main(["--zip", "32118", "--top", "3", "--details"]) == 0
    assert "Florida surf check" in capsys.readouterr().out


# --- Tide timezone handling -------------------------------------------------
# NOAA is asked for time_zone=lst_ldt, so predictions come back in the
# STATION's local time. fetch_tide must therefore compare them against "now"
# in that station's timezone, not against the machine's local clock. The three
# Panhandle spots are US/Central while the rest of Florida is US/Eastern.

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _tide_payload_at(when):
    return {"predictions": [{"t": when.strftime("%Y-%m-%d %H:%M"), "type": "L", "v": "0.1"}]}


def test_fetch_tide_uses_station_timezone_not_machine_clock():
    """
    A tide 20 minutes away in Central time must be found when the station is
    Central, and must NOT be found when the same timestamp is read as Eastern
    (where it is already 40 minutes in the past). This pins the exact bug the
    live run hit: Pensacola skipped an imminent Low and reported the next
    morning's High instead, inverting tide_state from falling to rising.

    Deliberately independent of the timezone the test machine runs in.
    """
    central_now = dt.datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
    payload = _tide_payload_at(central_now + dt.timedelta(minutes=20))

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(payload)

    session = mock.Mock()
    session.get = fake_get

    state, label = conditions.fetch_tide("8729840", "America/Chicago", session)
    assert state == "falling" and label is not None, \
        "imminent Central tide should be picked up for a Central station"

    state_eastern, label_eastern = conditions.fetch_tide("8729840", "America/New_York", session)
    assert (state_eastern, label_eastern) == (None, None), \
        "same timestamp read as Eastern is in the past and must not be returned"


def test_every_spot_timezone_is_loadable_and_panhandle_is_central():
    """spots.py carries a tz per spot; it must be a real IANA zone."""
    for s in SPOTS:
        ZoneInfo(s.tz)  # raises if bogus
    panhandle = [s for s in SPOTS if "Panhandle" in s.region]
    assert panhandle, "expected Panhandle spots in the spot list"
    assert all(s.tz == "America/Chicago" for s in panhandle)
    assert all(s.tz == "America/New_York" for s in SPOTS if "Panhandle" not in s.region)


# --- default origin ---------------------------------------------------------

def test_zip_falls_back_to_the_environment(offline, capsys, monkeypatch):
    """Setting FL_SURF_ZIP once should replace typing --zip every run."""
    monkeypatch.setenv(cli.ZIP_ENV_VAR, "33613")
    assert cli.main(["--top", "2"]) == 0
    assert "Florida surf check" in capsys.readouterr().out


def test_explicit_zip_beats_the_environment(offline, capsys, monkeypatch):
    monkeypatch.setenv(cli.ZIP_ENV_VAR, "99999")
    assert cli.main(["--zip", "33613", "--top", "2"]) == 0


def test_missing_zip_explains_both_options(capsys, monkeypatch):
    monkeypatch.delenv(cli.ZIP_ENV_VAR, raising=False)
    assert cli.main(["--top", "2"]) == 2
    err = capsys.readouterr().err
    assert "--zip" in err and cli.ZIP_ENV_VAR in err


# --- the surf log, end to end through the CLI -------------------------------

def test_surfed_logs_a_session_without_network_or_zip(tmp_path, capsys):
    """
    --surfed is a local operation: it must work with no zip, no internet and
    no mocking, because it runs before any of that.
    """
    p = str(tmp_path / "log.json")
    assert cli.main(["--surfed", "apollo", "--on", "2026-08-10", "--log-path", p]) == 0
    out = capsys.readouterr().out
    assert "Apollo Beach (Canaveral NS)" in out
    from fl_surf_check.surflog import load_log
    assert load_log(p).total_sessions() == 1


def test_surfed_rejects_an_unmatchable_spot(tmp_path, capsys):
    p = str(tmp_path / "log.json")
    assert cli.main(["--surfed", "zzzz nowhere", "--log-path", p]) == 2
    assert "no spot matches" in capsys.readouterr().err


def test_surfed_rejects_a_future_date(tmp_path, capsys):
    p = str(tmp_path / "log.json")
    assert cli.main(["--surfed", "ponce", "--on", "2099-01-01", "--log-path", p]) == 2
    assert "future" in capsys.readouterr().err


def test_surfed_rejects_a_malformed_date(tmp_path, capsys):
    p = str(tmp_path / "log.json")
    assert cli.main(["--surfed", "ponce", "--on", "last tuesday", "--log-path", p]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_run_without_a_log_is_unaffected(offline, capsys, tmp_path):
    """The overwhelmingly common case: no log yet, nothing changes."""
    cli.main(["--zip", "32118", "--top", "3", "--log-path", str(tmp_path / "none.json")])
    out = capsys.readouterr().out
    assert "sessions logged" not in out
    assert "Florida surf check" in out


def test_no_personal_ignores_an_existing_log(offline, capsys, tmp_path):
    p = str(tmp_path / "log.json")
    cli.main(["--surfed", "ponce", "--log-path", p])
    capsys.readouterr()
    cli.main(["--zip", "32118", "--top", "3", "--log-path", p, "--no-personal"])
    out = capsys.readouterr().out
    assert "ignored for this run" in out


def test_logged_sessions_penalise_that_spot_relative_to_others(offline, tmp_path):
    """
    The whole point of novelty: surfing a spot repeatedly should cost it ground
    against untouched ones.

    Asserted as a change in the GAP rather than a change in rank. Whether the
    leader is actually displaced depends on how far ahead it was to begin with,
    which is a property of the fixture's random conditions, not of the feature.
    The gap closing is the real guarantee.
    """
    p = str(tmp_path / "log.json")

    def values():
        args = cli.build_parser().parse_args(
            ["--zip", "32118", "--top", "0", "--log-path", p])
        rows, _ = cli.gather(ORIGIN, args)
        rows = cli.filter_and_sort(rows, args)
        return rows, {r["spot"].name: r["value"].total for r in rows}

    rows, before = values()
    leader = rows[0]["spot"].name
    runner_up = rows[1]["spot"].name
    gap_before = before[leader] - before[runner_up]

    for _ in range(12):
        cli.main(["--surfed", leader, "--log-path", p])

    _, after = values()
    gap_after = after[leader] - after[runner_up]

    assert after[leader] < before[leader], "surfing it should lower its own score"
    assert gap_after < gap_before, (
        f"gap to {runner_up} was {gap_before:+.2f} and is now {gap_after:+.2f}"
    )


def test_novelty_eventually_displaces_a_leader(offline, tmp_path):
    """
    With enough sessions the handicap should be big enough to actually reorder,
    not merely narrow the gap.
    """
    p = str(tmp_path / "log.json")
    args = cli.build_parser().parse_args(
        ["--zip", "32118", "--top", "0", "--log-path", p])
    rows, _ = cli.gather(ORIGIN, args)
    leader = cli.filter_and_sort(rows, args)[0]["spot"].name

    for _ in range(150):
        cli.main(["--surfed", leader, "--log-path", p])

    args2 = cli.build_parser().parse_args(
        ["--zip", "32118", "--top", "0", "--log-path", p])
    rows2, _ = cli.gather(ORIGIN, args2)
    after = [r["spot"].name for r in cli.filter_and_sort(rows2, args2)]
    assert after[0] != leader, f"{leader} survived 150 logged sessions there"

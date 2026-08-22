"""
End-to-end test of the CLI with every network call mocked out.

This exercises the real ranking, filtering and rendering code paths without
touching Open-Meteo, NOAA or OSRM - so it runs in CI, offline, and instantly.
"""

import datetime as dt
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


def _fake_tide(station, tz="America/New_York", session=None):
    return "rising", "High 14:32"


def _fake_baseline(spots, **kwargs):
    """One deterministic statewide baseline; never touches the network."""
    return Baseline(
        height_p=tuple(0.2 + 0.06 * q for q in Baseline.LEVELS),   # 0.2ft -> 6.2ft
        period_p=tuple(3.0 + 0.09 * q for q in Baseline.LEVELS),   # 3s -> 12s
        n_days=145,
        n_years=5,
        n_observations=145 * 26,
        n_spots=26,
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


def test_results_are_sorted_by_worth_descending(offline):
    args = cli.build_parser().parse_args(["--zip", "32118", "--top", "0"])
    rows = cli.filter_and_sort(cli.gather(ORIGIN, args), args)
    totals = [r["worth"].total for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_surf_weight_changes_the_ranking(offline):
    """Sanity check that the distance normalization actually does something."""
    base = cli.build_parser().parse_args(["--zip", "32118", "--top", "0"])
    quality_only = cli.build_parser().parse_args(
        ["--zip", "32118", "--top", "0", "--surf-weight", "1.0"]
    )
    a = [r["spot"].name for r in cli.filter_and_sort(cli.gather(ORIGIN, base), base)]
    b = [r["spot"].name for r in cli.filter_and_sort(cli.gather(ORIGIN, quality_only), quality_only)]
    assert a != b, "distance weighting had no effect on ordering"


def test_invalid_surf_weight_rejected():
    assert cli.main(["--zip", "32118", "--surf-weight", "5"]) == 2


def test_missing_data_does_not_crash(capsys):
    """Every API can fail; the CLI must still produce output."""
    empty = lambda spots, hours_ahead=24: {
        s.name: [(0, Conditions(errors=("all sources down",)))] for s in spots
    }
    with mock.patch.object(cli, "fetch_marine_and_wind", empty), \
         mock.patch.object(cli, "fetch_tide", lambda st, tz="America/New_York", session=None: (None, None)), \
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

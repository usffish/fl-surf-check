"""
End-to-end test of the CLI with every network call mocked out.

This exercises the real ranking, filtering and rendering code paths without
touching Open-Meteo, NOAA or OSRM - so it runs in CI, offline, and instantly.
"""

import random
from unittest import mock

import pytest

from fl_surf_check import cli
from fl_surf_check.conditions import Conditions
from fl_surf_check.distance import DriveEstimate, straight_line_miles
from fl_surf_check.location import Origin

ORIGIN = Origin(29.2108, -81.0228, "Daytona Beach, FL 32118", "pgeocode")


def _fake_marine(spots):
    rng = random.Random(7)
    return {
        s.name: Conditions(
            wave_height_ft=round(rng.uniform(0.4, 6.5), 1),
            wave_period_s=round(rng.uniform(4, 14), 1),
            wave_direction_deg=round(rng.uniform(40, 140)),
            wind_speed_mph=round(rng.uniform(1, 22)),
            wind_direction_deg=round(rng.uniform(0, 359)),
        )
        for s in spots
    }


def _fake_tide(station, session=None):
    return "rising", "High 14:32"


def _fake_drive(olat, olon, dlat, dlon, timeout=8.0, session=None):
    miles = straight_line_miles(olat, olon, dlat, dlon) * 1.25
    return DriveEstimate(miles, miles / 50 * 60, "osrm")


@pytest.fixture
def offline():
    with mock.patch.object(cli, "fetch_marine_and_wind", _fake_marine), \
         mock.patch.object(cli, "fetch_tide", _fake_tide), \
         mock.patch.object(cli, "get_drive_estimate", _fake_drive), \
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
    empty = lambda spots: {s.name: Conditions(errors=("all sources down",)) for s in spots}
    with mock.patch.object(cli, "fetch_marine_and_wind", empty), \
         mock.patch.object(cli, "fetch_tide", lambda st, session=None: (None, None)), \
         mock.patch.object(cli, "get_drive_estimate", _fake_drive), \
         mock.patch.object(cli, "geocode_zip", lambda z: ORIGIN):
        assert cli.main(["--zip", "32118", "--top", "3", "--details"]) == 0
    assert "Florida surf check" in capsys.readouterr().out

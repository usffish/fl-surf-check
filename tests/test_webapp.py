"""
The Flask UI, exercised through its test client with every network call
mocked out - same approach as test_cli_offline.py, since webapp.py calls
through the `cli` module rather than duplicating its pipeline.
"""

import datetime as dt
import math
import random
from unittest import mock

import pytest

from fl_surf_check import cli
from fl_surf_check.conditions import Conditions
from fl_surf_check.distance import DriveEstimate, straight_line_miles
from fl_surf_check.climatology import Baseline
from fl_surf_check.location import GeocodeError, Origin
from fl_surf_check.webapp import app

ORIGIN = Origin(29.2108, -81.0228, "Daytona Beach, FL 32118", "pgeocode")


def _fake_marine(spots, hours_ahead=24):
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
    return Baseline(
        height_p=tuple(0.2 + 0.06 * q for q in Baseline.LEVELS),
        period_p=tuple(3.0 + 0.09 * q for q in Baseline.LEVELS),
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


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def test_blank_form_shows_no_results(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"fl-surf-check" in resp.data
    assert b"<table>" not in resp.data


def test_zip_returns_a_ranked_table(offline, client):
    resp = client.get("/?zip=32118&days=1&top=5")
    assert resp.status_code == 200
    assert b"Daytona Beach, FL 32118" in resp.data
    assert resp.data.count(b"<tr class=") == 5


def test_top_limits_row_count(offline, client):
    resp = client.get("/?zip=32118&top=2")
    assert resp.data.count(b"<tr class=") == 2


def test_invalid_days_is_rejected(offline, client):
    resp = client.get("/?zip=32118&days=99")
    assert b"Days must be between 1 and" in resp.data
    assert b"<table>" not in resp.data


def test_bad_zip_shows_the_geocode_error(client):
    def _raise(zip_code):
        raise GeocodeError(f"Could not resolve zip code {zip_code}.")
    with mock.patch.object(cli, "geocode_zip", _raise):
        resp = client.get("/?zip=00000")
    assert b"Could not resolve zip code" in resp.data


def test_worth_only_without_history_is_empty(offline, client):
    """No baseline means no sigma, so nothing can clear worth_it - same as the CLI."""
    resp = client.get("/?zip=32118&worth_only=on&no_history=on")
    assert b"No spots matched" in resp.data


def test_verdict_matches_the_cli_render(offline, client, capsys):
    """The web table and a CLI run must agree, since both call compute_verdict."""
    web_resp = client.get("/?zip=32118&top=1")
    origin = ORIGIN
    args = cli.build_parser().parse_args(["--zip", "32118", "--top", "1"])
    rows, _ = cli.gather(origin, args)
    rows = cli.filter_and_sort(rows, args)
    expected_verdict = cli.compute_verdict(rows[0])
    assert expected_verdict.encode() in web_resp.data


# --- /surfed -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _sandboxed_log(tmp_path, monkeypatch):
    """Every test in this file gets its own log file, never the real one."""
    monkeypatch.setenv("FL_SURF_LOG", str(tmp_path / "log.json"))


def test_surfed_logs_a_session_and_redirects_back(client):
    resp = client.post("/surfed", data={
        "spot": "apollo", "return_qs": "zip=32118&days=1",
    })
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/?zip=32118&days=1&logged=")
    assert "Apollo+Beach" in resp.headers["Location"]


def test_surfed_shows_a_confirmation_banner(client):
    client.post("/surfed", data={"spot": "apollo", "return_qs": ""})
    resp = client.get("/?logged=Apollo+Beach+%28Canaveral+NS%29")
    assert b"Logged a session at" in resp.data
    assert b"Apollo Beach" in resp.data


def test_surfed_rejects_an_unmatchable_spot(client):
    resp = client.post("/surfed", data={"spot": "zzzz nowhere", "return_qs": ""})
    assert resp.status_code == 302
    assert "log_error=" in resp.headers["Location"]
    follow = client.get(resp.headers["Location"])
    assert b"no spot matches" in follow.data


def test_surfed_rejects_a_blank_spot(client):
    resp = client.post("/surfed", data={"spot": "", "return_qs": ""})
    follow = client.get(resp.headers["Location"])
    assert b"Enter a spot name" in follow.data


def test_surfed_actually_writes_the_log(client, tmp_path):
    client.post("/surfed", data={"spot": "apollo", "return_qs": ""})
    from fl_surf_check.surflog import load_log
    log = load_log()
    assert log.total_sessions() == 1

"""
Tests for the surf log and the two personal scoring factors it feeds.

All offline - the log is a local JSON file and both factors are pure math.
"""

import datetime as dt
import json

import pytest

from fl_surf_check import surflog
from fl_surf_check.scoring import (
    ITCH_RATE_PER_DAY,
    MINUTES_PER_SIGMA,
    NOVELTY_WEIGHT,
    itch_bonus,
    novelty_penalties,
    value_score,
)
from fl_surf_check.spots import SPOTS
from fl_surf_check.surflog import load_log, record_session, resolve_spot_name

NAMES = [s.name for s in SPOTS]


# --- storage ---------------------------------------------------------------

def test_empty_log_when_file_is_missing(tmp_path):
    """First run for everyone: no file, no error, no sessions."""
    log = load_log(str(tmp_path / "nope.json"))
    assert log.sessions == []
    assert log.total_sessions() == 0
    assert log.days_since_last() is None


def test_corrupt_log_does_not_crash(tmp_path):
    p = tmp_path / "log.json"
    p.write_text("{not json")
    assert load_log(str(p)).sessions == []


def test_malformed_rows_are_skipped_not_fatal(tmp_path):
    """One bad row must not discard an otherwise good log."""
    p = tmp_path / "log.json"
    p.write_text(json.dumps({"sessions": [
        {"date": "2026-08-01", "spot": "Ponce Inlet"},
        {"date": "not-a-date", "spot": "Ponce Inlet"},
        {"spot": "missing date"},
        {"date": "2026-08-05", "spot": "Cocoa Beach Pier"},
    ]}))
    log = load_log(str(p))
    assert log.total_sessions() == 2


def test_record_session_round_trips(tmp_path):
    p = str(tmp_path / "log.json")
    record_session("Ponce Inlet", on=dt.date(2026, 8, 10), path=p)
    record_session("Ponce Inlet", on=dt.date(2026, 8, 14), path=p)
    record_session("Cocoa Beach Pier", on=dt.date(2026, 8, 12), path=p)

    log = load_log(p)
    assert log.total_sessions() == 3
    counts = log.visit_counts(NAMES)
    assert counts["Ponce Inlet"] == 2
    assert counts["Cocoa Beach Pier"] == 1
    assert counts["Jupiter Inlet"] == 0


def test_visit_counts_include_zeros_for_every_spot(tmp_path):
    """
    Novelty is a z-score across the whole spot list, so the never-visited spots
    are most of the distribution and must be present, not omitted.
    """
    p = str(tmp_path / "log.json")
    record_session("Ponce Inlet", path=p)
    counts = load_log(p).visit_counts(NAMES)
    assert len(counts) == len(NAMES)
    assert sum(counts.values()) == 1


def test_days_since_last_uses_the_most_recent_session(tmp_path):
    p = str(tmp_path / "log.json")
    record_session("Ponce Inlet", on=dt.date(2026, 8, 1), path=p)
    record_session("Cocoa Beach Pier", on=dt.date(2026, 8, 20), path=p)
    log = load_log(p)
    assert log.days_since_last(today=dt.date(2026, 8, 25)) == 5


def test_days_since_last_never_negative(tmp_path):
    """A session logged today (or a clock skew) must not produce negative itch."""
    p = str(tmp_path / "log.json")
    record_session("Ponce Inlet", on=dt.date(2026, 8, 25), path=p)
    assert load_log(p).days_since_last(today=dt.date(2026, 8, 25)) == 0


# --- fuzzy spot matching ---------------------------------------------------

def test_resolve_exact_and_fuzzy():
    assert resolve_spot_name("Ponce Inlet", NAMES) == "Ponce Inlet"
    assert resolve_spot_name("ponce inlet", NAMES) == "Ponce Inlet"
    assert resolve_spot_name("apollo", NAMES) == "Apollo Beach (Canaveral NS)"
    assert resolve_spot_name("clearwater", NAMES) == "Clearwater Beach"


def test_resolve_returns_none_rather_than_guessing():
    """
    A wrong guess silently logs a session against the wrong break, which then
    quietly skews novelty. Better to refuse.
    """
    assert resolve_spot_name("zzzz nowhere", NAMES) is None


# --- itch ------------------------------------------------------------------

def test_itch_is_zero_without_a_log():
    """
    None means "unknown", not "forever". A fresh install must not open by
    insisting the user drive across the state.
    """
    assert itch_bonus(None) == 0.0


def test_itch_is_zero_on_the_day_you_surfed():
    assert itch_bonus(0) == 0.0


def test_itch_grows_linearly_and_is_uncapped():
    """Deliberately unbounded - no ceiling, by request."""
    a, b, c = itch_bonus(10), itch_bonus(100), itch_bonus(1000)
    assert a < b < c
    assert b == pytest.approx(a * 10)
    assert c == pytest.approx(a * 100)


def test_itch_converts_to_sensible_drive_minutes():
    """A week out of the water should be worth roughly 40 minutes of driving."""
    assert itch_bonus(7) * MINUTES_PER_SIGMA == pytest.approx(42.0, abs=1.0)


# --- novelty ---------------------------------------------------------------

def _counts(**kw):
    c = {n: 0 for n in NAMES}
    c.update(kw)
    return c


def test_novelty_is_zero_for_an_empty_log():
    """No sessions means no opinion - and no division by zero."""
    pens = novelty_penalties(_counts())
    assert set(pens) == set(NAMES)
    assert all(v == 0.0 for v in pens.values())


@pytest.mark.parametrize("count", [1, 3, 4, 23, 24, 25, 27, 30, 100])
def test_novelty_is_zero_when_every_spot_is_equal(count):
    """
    A flat column has no spread to z-score and must yield zero.

    Parametrised deliberately: the spread is mathematically zero but floating
    point leaves sd near 1e-16 rather than on it, for some counts and not
    others, and differently across platforms. CI failed on a value this
    machine computed as exactly 0.0. Several of these counts produce a nonzero
    sd locally and would give z = +-1.0 from rounding noise alone.
    """
    pens = novelty_penalties({n: count for n in NAMES})
    assert all(v == 0.0 for v in pens.values()), \
        f"flat column of {count} produced nonzero penalties"


def test_novelty_penalises_visited_and_rewards_neglected():
    pens = novelty_penalties(_counts(**{"Ponce Inlet": 10, "Cocoa Beach Pier": 2}))
    assert pens["Ponce Inlet"] > pens["Cocoa Beach Pier"] > 0
    assert pens["Jupiter Inlet"] < 0, "never-surfed spots should get a small bonus"


def test_novelty_is_monotonic_in_visits():
    pens = novelty_penalties(_counts(**{
        "Ponce Inlet": 20, "Cocoa Beach Pier": 8,
        "Vero Beach": 3, "Jupiter Inlet": 1,
    }))
    ordered = [pens["Ponce Inlet"], pens["Cocoa Beach Pier"],
               pens["Vero Beach"], pens["Jupiter Inlet"]]
    assert ordered == sorted(ordered, reverse=True)


def test_a_single_session_does_not_destroy_a_spot():
    """
    Regression for the raw-z-score failure. Visit counts are extremely
    zero-inflated, so a plain z-score gives one logged session a z of +6.3 -
    a twelve-hour handicap from one surf.

    Note what actually rescues this case: the EVIDENCE SHRINKAGE, not the log
    transform. Measured, log and raw give an identical penalty at one session,
    because a single nonzero value has the same z either way. It is
    n/(n + prior) that keeps it small. The log transform earns its place
    elsewhere - see test_novelty_compresses_heavy_use.
    """
    pens = novelty_penalties(_counts(**{"Ponce Inlet": 1}))
    minutes = pens["Ponce Inlet"] * MINUTES_PER_SIGMA
    assert 0 < minutes < 30, f"one session cost {minutes:.0f} min of driving"


def test_novelty_compresses_heavy_use():
    """
    What the log transform is actually for. In log space, equal RATIOS of
    visits produce equal penalty increments, so going 1 -> 10 costs about the
    same as 10 -> 100. On raw counts the second jump would cost roughly ten
    times the first, and a heavily-used home break would run away.

    Measured against the alternative: on a realistic 200-session log, raw
    counts penalise the most-surfed spot 1.7x harder than log space does.
    """
    def top(visits):
        # A fixed backdrop of other spots so total evidence barely moves,
        # isolating the effect of the shape rather than the shrinkage.
        c = _counts(**{"Ponce Inlet": visits, "Cocoa Beach Pier": 40,
                       "Vero Beach": 25, "Sebastian Inlet (First Peak)": 15})
        return novelty_penalties(c)["Ponce Inlet"]

    first_jump = top(10) - top(1)
    second_jump = top(100) - top(10)
    assert second_jump == pytest.approx(first_jump, rel=0.45), (
        f"1->10 cost {first_jump:.3f} but 10->100 cost {second_jump:.3f}; "
        "penalty is not growing logarithmically"
    )


def test_novelty_is_damped_while_the_log_is_young():
    """Same Laplace pattern as shrink_percentile: thin evidence, quiet signal."""
    young = novelty_penalties(_counts(**{"Ponce Inlet": 3, "Vero Beach": 1}))
    mature = novelty_penalties(_counts(**{"Ponce Inlet": 60, "Vero Beach": 20}))
    assert mature["Ponce Inlet"] > young["Ponce Inlet"]


def test_novelty_is_uncapped_at_full_evidence():
    """
    No tanh, no ceiling - by request. With a lopsided mature log the penalty
    should be free to exceed the weight itself.
    """
    pens = novelty_penalties(_counts(**{"Ponce Inlet": 400}))
    assert pens["Ponce Inlet"] > NOVELTY_WEIGHT


def test_novelty_weight_zero_disables_it():
    pens = novelty_penalties(_counts(**{"Ponce Inlet": 50}), weight=0.0)
    assert all(v == 0.0 for v in pens.values())


# --- how they combine in the value score -----------------------------------

def test_itch_lifts_the_score_and_novelty_lowers_it():
    base = value_score(1.0, 60.0)
    itched = value_score(1.0, 60.0, itch=0.5)
    penalised = value_score(1.0, 60.0, novelty=0.5)
    assert itched.total > base.total
    assert penalised.total < base.total
    assert itched.total - base.total == pytest.approx(0.5)
    assert base.total - penalised.total == pytest.approx(0.5)


def test_itch_cannot_reorder_because_it_is_the_same_everywhere():
    """
    The property that distinguishes the two factors: itch shifts the whole
    board, moving the go/no-go line without changing which spot wins.
    """
    sigmas = [1.2, 0.4, -0.3]
    drives = [30.0, 60.0, 200.0]
    plain = [value_score(s, d).total for s, d in zip(sigmas, drives)]
    lifted = [value_score(s, d, itch=0.8).total for s, d in zip(sigmas, drives)]
    assert sorted(range(3), key=lambda i: -plain[i]) == \
           sorted(range(3), key=lambda i: -lifted[i])
    assert all(b > a for a, b in zip(plain, lifted))


def test_novelty_can_reorder():
    """The other half of that contrast: novelty is per-spot, so it does."""
    a_plain = value_score(1.0, 60.0).total
    b_plain = value_score(0.9, 60.0).total
    assert a_plain > b_plain
    a_pen = value_score(1.0, 60.0, novelty=0.4).total
    assert b_plain > a_pen, "a heavily-surfed better spot should fall behind"


def test_personal_factors_default_to_off():
    v = value_score(1.0, 60.0)
    assert v.itch == 0.0 and v.novelty == 0.0
    assert not v.has_personal


def test_has_personal_detects_either_factor():
    assert value_score(1.0, 60.0, itch=0.2).has_personal
    assert value_score(1.0, 60.0, novelty=0.2).has_personal


def test_value_still_works_without_a_baseline():
    v = value_score(None, 60.0, itch=0.5, novelty=0.2)
    assert v.total is None
    assert v.itch == 0.5 and v.novelty == 0.2

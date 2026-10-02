"""The training week, sorted into qualities.

Every activity is classified from its own measurements, because the names carry
nothing: 33 of the last 33 strength sessions are called "Gym" and 26 of 27
interval sessions "HIIT". The classification is one SQL expression so that the
dashboard and the morning email cannot disagree about the same week.

These tests exercise the SQL itself against a real schema rather than a Python
reimplementation of it — a reimplementation would be a second classifier, which
is the thing the single expression exists to avoid.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_mcp.db import init_db
from garmin_mcp.week import (
    BASE_MIN_MINUTES,
    INTERFERENCE_HOURS,
    LONG_MINUTES,
    QUALITY_HARD_MINUTES,
    interference_days,
    observations,
    sessions,
    summarize,
    tally,
    week_start,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


_next_id = [1000]


def add(conn, day, time, sport, minutes, *, load=50, z1=0, z2=0, z3=0, z4=0, z5=0, km=0.0):
    """One activity with its heart-rate zone split, in seconds."""
    _next_id[0] += 1
    aid = _next_id[0]
    conn.execute(
        "INSERT INTO activity (activity_id, activity_type, start_time_local, "
        "duration_seconds, distance_meters, training_load) VALUES (?,?,?,?,?,?)",
        (aid, sport, f"{day} {time}:00", minutes * 60, km * 1000, load))
    conn.execute(
        "INSERT INTO activity_hr_zones (activity_id, zone1_seconds, zone2_seconds, "
        "zone3_seconds, zone4_seconds, zone5_seconds) VALUES (?,?,?,?,?,?)",
        (aid, z1, z2, z3, z4, z5))
    conn.commit()
    return aid


def quality_of(conn, day="2026-09-28"):
    rows = sessions(conn, day, day)
    assert len(rows) == 1, f"expected one session, got {len(rows)}"
    return rows[0]["quality"]


# ---- the ladder of rules, in the order they are applied -------------------

def test_resistance_work_is_strength_whatever_its_heart_rate(db):
    """A gym session that drove the heart rate up is still resistance work.

    One of Áron's gym sessions carried load 94 and aerobic TE 2.7 — genuinely
    hybrid — but counting it as aerobic work would credit a week that did no
    running with having done some.
    """
    add(db, "2026-09-28", "11:00", "strength_training", 81, load=94, z1=1980, z3=300, z4=300)
    assert quality_of(db) == "strength"


def test_team_sport_is_mixed_even_when_the_average_is_low(db):
    """Football at 96 bpm average is still stop-start.

    Classifying it as steady aerobic work by its average would say the week had
    aerobic base in it when nothing steady happened.
    """
    add(db, "2026-09-28", "19:00", "soccer", 64, load=23, z1=600, z2=360)
    assert quality_of(db) == "mixed"


def test_eight_hard_minutes_make_a_session_quality(db):
    add(db, "2026-09-28", "18:00", "running", 51, load=155,
        z1=600, z2=1380, z3=0, z4=QUALITY_HARD_MINUTES * 60, z5=0)
    assert quality_of(db) == "aerobic_quality"


def test_just_under_the_threshold_is_not(db):
    add(db, "2026-09-28", "18:00", "running", 51, load=155,
        z1=600, z2=1380, z4=(QUALITY_HARD_MINUTES * 60) - 60)
    assert quality_of(db) != "aerobic_quality"


def test_a_long_easy_session_is_aerobic_base(db):
    add(db, "2026-09-28", "09:00", "cycling", 191, load=56, z1=4560, z2=3300, z3=2100, z4=150)
    assert quality_of(db) == "aerobic_base"


def test_easy_but_short_is_restorative_not_mixed(db):
    """A twenty-minute spin is a light session, not an unclassifiable one.

    This fell to "mixed" until the rule was added: the displayed minutes round
    up to the threshold while the real duration sits just under it.
    """
    add(db, "2026-09-28", "13:00", "cycling", BASE_MIN_MINUTES - 1, load=19, z1=380, z2=620, z3=80)
    assert quality_of(db) == "restorative"


def test_a_hard_session_in_any_sport_counts_as_quality(db):
    """HIIT is where most of Áron's hard work happens; it must not be 'mixed'."""
    add(db, "2026-09-28", "18:00", "hiit", 67, load=247, z1=720, z2=660, z3=330, z4=3200, z5=340)
    assert quality_of(db) == "aerobic_quality"


def test_yoga_is_restorative_by_sport(db):
    add(db, "2026-09-28", "07:00", "yoga", 29, load=0)
    assert quality_of(db) == "restorative"


def test_a_session_with_no_zone_data_does_not_crash(db):
    """Zone rows are missing for some activities; the week still has to render."""
    _next_id[0] += 1
    db.execute("INSERT INTO activity (activity_id, activity_type, start_time_local, "
               "duration_seconds, training_load) VALUES (?,?,?,?,?)",
               (_next_id[0], "running", "2026-09-28 10:00:00", 1800, 40))
    db.commit()
    assert quality_of(db) in ("mixed", "restorative", "aerobic_base")


# ---- the week as a whole --------------------------------------------------

def test_the_week_starts_on_monday():
    assert week_start("2026-10-02") == "2026-09-28"   # Friday -> Monday
    assert week_start("2026-09-28") == "2026-09-28"   # Monday -> itself
    assert week_start("2026-10-04") == "2026-09-28"   # Sunday -> the same Monday


def test_a_long_run_of_the_right_length_satisfies_the_long_session(db):
    add(db, "2026-09-28", "09:00", "running", LONG_MINUTES, load=90, z1=1200, z2=2400)
    s = summarize(db, "2026-09-28")
    assert s["has_long_base"] is True
    assert s["longest_base_minutes"] == LONG_MINUTES


def test_a_week_of_short_easy_runs_does_not(db):
    """The usual gap is not "no easy running" but "nothing long"."""
    for day in ("2026-09-28", "2026-09-29", "2026-09-30"):
        add(db, day, "09:00", "running", LONG_MINUTES - 10, load=70, z1=1200, z2=1800)
    s = summarize(db, "2026-09-30")
    assert s["counts"]["aerobic_base"] == 3
    assert s["has_long_base"] is False, "three medium runs are not a long one"


def test_the_tally_counts_days_not_just_sessions(db):
    add(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    add(db, "2026-09-28", "19:00", "soccer", 60, load=120, z4=900)
    s = summarize(db, "2026-09-28")
    assert s["sessions"] == 2 and s["days"] == 1


# ---- interference ---------------------------------------------------------

def test_two_sessions_close_together_are_reported(db):
    add(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    add(db, "2026-09-28", "13:00", "running", 40, load=80, z1=600, z2=1200)
    hits = summarize(db, "2026-09-28")["interference"]
    assert len(hits) == 1
    assert hits[0]["gap_hours"] == 2.0


def test_well_separated_sessions_are_not(db):
    add(db, "2026-09-28", "07:00", "strength_training", 70, load=12)
    add(db, "2026-09-28", f"{7 + int(INTERFERENCE_HOURS) + 1}:00", "running", 40,
        load=80, z1=600, z2=1200)
    assert summarize(db, "2026-09-28")["interference"] == []


def test_a_day_without_strength_is_not_an_interference_day(db):
    """The effect is strength being blunted; two easy rides are not that."""
    add(db, "2026-09-28", "09:00", "cycling", 60, load=30, z1=1800, z2=1800)
    add(db, "2026-09-28", "11:00", "cycling", 40, load=25, z1=1200, z2=1200)
    assert summarize(db, "2026-09-28")["interference"] == []


def test_a_single_session_day_is_never_flagged(db):
    add(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    assert summarize(db, "2026-09-28")["interference"] == []


# ---- the advice -----------------------------------------------------------

def test_a_week_with_no_long_session_says_so_first(db):
    add(db, "2026-09-28", "18:00", "running", 51, load=155, z1=600, z2=1380, z4=720)
    out = observations(summarize(db, "2026-09-28"))
    assert "long aerobic" in out[0]
    assert str(LONG_MINUTES) in out[0], "the advice states the threshold it is judging against"


def test_an_all_hard_week_is_called_out(db):
    add(db, "2026-09-28", "18:00", "hiit", 60, load=200, z4=1800)
    add(db, "2026-09-29", "18:00", "running", 50, load=160, z4=1200)
    out = " ".join(observations(summarize(db, "2026-09-29")))
    assert "hard" in out and "Easy volume" in out


def test_the_advice_never_prescribes_a_day(db):
    """Advisory by design: which day it lands on is settled on the day."""
    add(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    add(db, "2026-09-28", "13:00", "running", 40, load=80, z1=600, z2=1200)
    for line in observations(summarize(db, "2026-09-28")):
        low = line.lower()
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            assert weekday not in low, f"advice named a day: {line}"


def test_an_empty_week_still_produces_advice(db):
    out = observations(summarize(db, "2026-09-28"))
    assert out, "a week with nothing in it is exactly when advice matters"
    assert any("No aerobic" in line for line in out)

"""What the system said, and whether the day matched it.

The verdict was computed and thrown away, so the question worth asking about a
daily adviser had no data behind it. These tests are mostly about the two limits
the module claims for itself: it keeps the MORNING verdict, and it grades what
was done on four rungs rather than pretending it can see five.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_mcp.adherence import (
    DONE_LEVELS,
    SAID_LEVELS,
    adherence,
    done_level,
    history,
    observations,
    record_verdict,
    summarize,
)
from garmin_mcp.db import init_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def V(date, level, **kw):
    return {"date": date, "level": level, **kw}


def add(conn, day, time, sport, minutes, *, load=50, z1=0, z2=0, z3=0, z4=0, z5=0):
    from tests.test_week import add as _add
    return _add(conn, day, time, sport, minutes, load=load, z1=z1, z2=z2, z3=z3,
                z4=z4, z5=z5)


# ---- the ladders -----------------------------------------------------------

def test_the_said_ladder_matches_the_brief_s_own():
    from garmin_mcp.brief import LEVELS

    assert SAID_LEVELS == LEVELS


def test_the_done_ladder_is_deliberately_shorter():
    """"Moderate" cannot be separated from "easy" in the session data without
    inventing a threshold, so it is not claimed."""
    assert "moderate" not in DONE_LEVELS
    assert set(DONE_LEVELS) < set(SAID_LEVELS)


# ---- storing it once -------------------------------------------------------

def test_a_verdict_is_stored(db):
    assert record_verdict(db, V("2026-10-26", "hard", headline="Hard day",
                                limiter="nothing", phase="base")) is True
    row = db.execute("SELECT * FROM daily_verdict").fetchone()
    assert row["level"] == "hard" and row["headline"] == "Hard day"
    assert row["recorded_at"], "when it was taken is part of the record"


def test_the_first_verdict_of_the_day_wins(db):
    """training_readiness is recomputed after every session, so an evening call
    would otherwise replace the morning judgement with a post-session one — and
    the morning one is what was actually acted on."""
    assert record_verdict(db, V("2026-10-26", "hard")) is True
    assert record_verdict(db, V("2026-10-26", "rest")) is False
    assert db.execute("SELECT level FROM daily_verdict").fetchone()[0] == "hard"


def test_different_days_are_separate_rows(db):
    record_verdict(db, V("2026-10-25", "easy"))
    record_verdict(db, V("2026-10-26", "hard"))
    assert db.execute("SELECT count(*) FROM daily_verdict").fetchone()[0] == 2


def test_a_verdict_with_nothing_in_it_is_not_stored(db):
    assert record_verdict(db, {}) is False
    assert record_verdict(db, None) is False
    assert record_verdict(db, V("2026-10-26", None)) is False
    assert record_verdict(db, {"level": "hard"}) is False, "a date is required"
    assert db.execute("SELECT count(*) FROM daily_verdict").fetchone()[0] == 0


# ---- what the day came to --------------------------------------------------

def test_no_session_is_a_rest_day(db):
    assert done_level([]) == "rest"


def test_a_quality_session_makes_the_day_hard(db):
    assert done_level([{"quality": "aerobic_quality"}, {"quality": "strength"}]) == "hard"


def test_only_restorative_work_is_a_recovery_day(db):
    assert done_level([{"quality": "restorative"}]) == "recovery"


def test_anything_else_is_an_easy_day(db):
    for q in ("aerobic_base", "strength", "mixed"):
        assert done_level([{"quality": q}]) == "easy", q
    assert done_level([{"quality": "restorative"}, {"quality": "strength"}]) == "easy"


def test_the_grading_uses_week_py_s_own_classification(db):
    """"Hard" has to mean the same thing here as everywhere else: eight or more
    minutes above the zone-4 floor."""
    from garmin_mcp.week import QUALITY_HARD_MINUTES, sessions

    add(db, "2026-10-26", "18:00", "running", 50, load=160,
        z1=600, z2=1200, z4=QUALITY_HARD_MINUTES * 60)
    assert done_level(sessions(db, "2026-10-26", "2026-10-26")) == "hard"


# ---- the comparison --------------------------------------------------------

@pytest.mark.parametrize("said,done,expected", [
    ("hard", "hard", "followed"),
    ("rest", "rest", "followed"),
    ("easy", "easy", "followed"),
    ("rest", "hard", "exceeded"),
    ("recovery", "hard", "exceeded"),
    ("easy", "hard", "exceeded"),
    ("hard", "rest", "under"),
    ("easy", "rest", "under"),
])
def test_the_comparison_reads_the_way_it_should(said, done, expected):
    assert adherence(said, done) == expected


def test_a_moderate_verdict_is_satisfied_by_easy_or_hard():
    """The honest consequence of not being able to see "moderate" in the data."""
    assert adherence("moderate", "easy") == "followed"
    assert adherence("moderate", "hard") == "followed"
    assert adherence("moderate", "rest") == "under"
    assert adherence("moderate", "recovery") == "under"


def test_what_cannot_be_judged_says_so_rather_than_guessing():
    assert adherence(None, "hard") is None
    assert adherence("hard", None) is None
    assert adherence("hard", "moderate") is None, "not a rung the data can produce"
    assert adherence("unknown", "hard") is None


# ---- the history -----------------------------------------------------------

def test_a_stored_verdict_is_paired_with_the_day(db):
    record_verdict(db, V("2026-10-26", "rest", limiter="readiness"))
    add(db, "2026-10-26", "18:00", "hiit", 50, load=200, z4=1200)
    rows = history(db, days=7, through="2026-10-26")
    assert len(rows) == 1
    r = rows[0]
    assert r["level"] == "rest" and r["done"] == "hard"
    assert r["adherence"] == "exceeded"
    assert r["sessions"] == 1 and r["minutes"] == 50 and r["sports"] == ["hiit"]


def test_a_day_with_no_stored_verdict_is_not_invented(db):
    """It says nothing about days before it started recording, rather than
    back-filling a judgement it never made."""
    add(db, "2026-10-25", "18:00", "hiit", 50, load=200, z4=1200)
    record_verdict(db, V("2026-10-26", "easy"))
    rows = history(db, days=7, through="2026-10-26")
    assert [r["calendar_date"] for r in rows] == ["2026-10-26"]


def test_the_window_is_respected(db):
    for d in ("2026-10-20", "2026-10-25", "2026-10-26"):
        record_verdict(db, V(d, "easy"))
    assert len(history(db, days=3, through="2026-10-26")) == 2


def test_newest_first(db):
    record_verdict(db, V("2026-10-24", "easy"))
    record_verdict(db, V("2026-10-26", "hard"))
    assert [r["calendar_date"] for r in history(db, days=7, through="2026-10-26")] \
        == ["2026-10-26", "2026-10-24"]


def test_an_empty_history_is_empty(db):
    assert history(db, days=30, through="2026-10-26") == []


# ---- the counts ------------------------------------------------------------

def test_the_summary_counts_each_kind(db):
    for d, said in [("2026-10-22", "rest"), ("2026-10-23", "rest"),
                    ("2026-10-24", "hard"), ("2026-10-25", "easy")]:
        record_verdict(db, V(d, said))
    add(db, "2026-10-23", "18:00", "hiit", 50, load=200, z4=1200)   # rest -> hard
    add(db, "2026-10-24", "18:00", "hiit", 50, load=200, z4=1200)   # hard -> hard
    s = summarize(history(db, days=7, through="2026-10-26"))
    assert s["days"] == 4 and s["judged"] == 4
    assert s["followed"] == 2, "the rest day rested, and the hard day went hard"
    assert s["exceeded"] == 1
    assert s["under"] == 1, "the easy day had nothing in it"
    assert s["followed_pct"] == 50


def test_the_rate_over_no_judged_days_is_nothing_not_zero(db):
    """Zero per cent would be a claim; None is the absence of one."""
    assert summarize([])["followed_pct"] is None


def test_the_summary_cuts_by_what_was_said(db):
    """"It said rest 6 times; you rested 4 of them" is the useful cut."""
    for d in ("2026-10-20", "2026-10-21", "2026-10-22"):
        record_verdict(db, V(d, "rest"))
    add(db, "2026-10-22", "18:00", "hiit", 50, load=200, z4=1200)
    s = summarize(history(db, days=7, through="2026-10-26"))
    assert s["by_said"]["rest"] == {"days": 3, "followed": 2, "exceeded": 1, "under": 0}


# ---- the words -------------------------------------------------------------

def test_nothing_recorded_says_so(db):
    assert "No verdict" in observations([])[0]


def test_going_hard_against_a_rest_verdict_is_named(db):
    """The one case worth naming on its own."""
    record_verdict(db, V("2026-10-26", "rest"))
    add(db, "2026-10-26", "18:00", "hiit", 50, load=200, z4=1200)
    out = " ".join(observations(history(db, days=7, through="2026-10-26")))
    assert "went hard against a rest" in out and "2026-10-26" in out


def test_an_ordinary_week_is_not_scolded(db):
    record_verdict(db, V("2026-10-26", "easy"))
    add(db, "2026-10-26", "09:00", "running", 40, load=70, z1=1200, z2=1200)
    out = " ".join(observations(history(db, days=7, through="2026-10-26")))
    assert "went hard against" not in out
    assert "1 of 1" in out

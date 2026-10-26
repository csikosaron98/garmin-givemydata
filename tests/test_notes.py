"""What the watch cannot see, and when the system asks for it.

The tests are mostly about restraint. A note may lower the day's ceiling and
never raise it; only one tag does even that; and the asking is silent unless
something has been off for three days running, and silent again the moment there
is an answer. A daily prompt decays inside a fortnight, so the whole value of
this is in how rarely it speaks.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_mcp.db import init_db
from garmin_mcp.notes import (
    ANOMALY_DAYS,
    ASK_COOLDOWN_DAYS,
    RHR_OVER_MEAN,
    TAGS,
    active_tags,
    add_note,
    anomalies,
    cap_from_notes,
    delete_note,
    describe_notes,
    normalise_tags,
    notes_between,
    notes_for,
    should_ask,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def seed_days(conn, days, *, rhr=40, hrv=70, hrv_low=52, sleep_h=7.5, end="2026-10-26"):
    """`days` consecutive days ending on `end`, all unremarkable unless told."""
    import datetime as dt

    last = dt.date.fromisoformat(end)
    for i in range(days):
        d = (last - dt.timedelta(days=days - 1 - i)).isoformat()
        r = rhr(i) if callable(rhr) else rhr
        h = hrv(i) if callable(hrv) else hrv
        s = sleep_h(i) if callable(sleep_h) else sleep_h
        conn.execute("INSERT INTO daily_summary (calendar_date, resting_heart_rate) "
                     "VALUES (?,?)", (d, r))
        conn.execute("INSERT INTO hrv (calendar_date, last_night_avg, baseline_low) "
                     "VALUES (?,?,?)", (d, h, hrv_low))
        conn.execute("INSERT INTO sleep (calendar_date, sleep_time_seconds) "
                     "VALUES (?,?)", (d, int(s * 3600)))
    conn.commit()


# ---- writing it down -------------------------------------------------------

def test_a_note_is_stored(db):
    nid = add_note(db, "2026-10-26", "Náthás vagyok", "illness")
    rows = notes_for(db, "2026-10-26")
    assert len(rows) == 1 and rows[0]["note_id"] == nid
    assert rows[0]["note"] == "Náthás vagyok" and rows[0]["tags"] == "illness"
    assert rows[0]["created"], "when it was written is part of the record"


def test_several_notes_a_day_are_all_kept(db):
    """A morning note and an evening one are both real, and overwriting one with
    the other would lose it."""
    add_note(db, "2026-10-26", "Reggel húz a vádli", "niggle")
    add_note(db, "2026-10-26", "Estére elmúlt")
    assert len(notes_for(db, "2026-10-26")) == 2


def test_an_empty_note_is_refused(db):
    with pytest.raises(ValueError, match="note"):
        add_note(db, "2026-10-26", "   ")


def test_a_bad_date_is_refused(db):
    with pytest.raises(ValueError):
        add_note(db, "tegnap", "valami")


def test_a_note_can_be_removed(db):
    nid = add_note(db, "2026-10-26", "elírás")
    assert delete_note(db, nid) is True
    assert notes_for(db, "2026-10-26") == []
    assert delete_note(db, nid) is False


def test_the_window_is_respected(db):
    add_note(db, "2026-10-20", "régi")
    add_note(db, "2026-10-26", "mai")
    assert len(notes_between(db, "2026-10-25", "2026-10-26")) == 1


# ---- the tags --------------------------------------------------------------

def test_every_tag_says_what_it_does_and_why():
    for name, spec in TAGS.items():
        assert spec["label"], f"{name} has no label"
        assert spec["why"], f"{name} changes or explains something without saying why"
        assert spec["caps_at"] in (None, "rest", "recovery", "easy"), name


def test_only_illness_lowers_anything():
    """Deliberately narrow. Which tissue hurts and how much is not something this
    data can grade, and guessing would be worse than saying nothing."""
    capping = {k for k, v in TAGS.items() if v["caps_at"]}
    assert capping == {"illness"}


def test_unknown_tags_are_dropped_not_stored(db):
    """A tag nothing can read quietly stops meaning anything. The note keeps the
    words either way."""
    nid = add_note(db, "2026-10-26", "fáradt vagyok", "illness, hangulat, travel")
    assert notes_for(db, "2026-10-26")[0]["tags"] == "illness,travel"
    assert normalise_tags("nonsense") == ""
    assert normalise_tags(None) == "" and normalise_tags("") == ""


def test_tags_are_stored_in_a_stable_order(db):
    assert normalise_tags("travel,illness") == normalise_tags("illness, travel")


def test_the_days_tags_come_back(db):
    add_note(db, "2026-10-26", "a", "illness")
    add_note(db, "2026-10-26", "b", "niggle")
    assert active_tags(db, "2026-10-26") == {"illness", "niggle"}
    assert active_tags(db, "2026-10-25") == set()


# ---- what a note does to the day -------------------------------------------

def test_an_illness_note_lowers_the_ceiling(db):
    add_note(db, "2026-10-26", "Náthás", "illness")
    cap, why = cap_from_notes(db, "2026-10-26")
    assert cap == "recovery" and "ill" in why


def test_every_other_tag_leaves_the_day_alone(db):
    for tag in ("niggle", "travel", "stress", "alcohol"):
        add_note(db, "2026-10-26", "x", tag)
        assert cap_from_notes(db, "2026-10-26") == (None, ""), tag
        db.execute("DELETE FROM daily_note")
        db.commit()


def test_a_note_with_no_tag_changes_nothing(db):
    add_note(db, "2026-10-26", "jó napom volt")
    assert cap_from_notes(db, "2026-10-26") == (None, "")


def test_a_note_is_about_the_day_it_is_filed_under(db):
    add_note(db, "2026-10-25", "Náthás", "illness")
    assert cap_from_notes(db, "2026-10-26") == (None, "")


def test_the_note_reads_back_for_the_brief(db):
    add_note(db, "2026-10-26", "Náthás vagyok", "illness")
    line = describe_notes(db, "2026-10-26")
    assert "Náthás vagyok" in line and "ill" in line
    assert describe_notes(db, "2026-10-25") == ""


# ---- the asking ------------------------------------------------------------

def test_an_ordinary_fortnight_asks_nothing(db):
    seed_days(db, 21)
    assert anomalies(db, "2026-10-26") == []
    assert should_ask(db, "2026-10-26") == ""


def test_a_sustained_rise_in_resting_heart_rate_is_noticed(db):
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45)
    found = anomalies(db, "2026-10-26")
    assert [a["what"] for a in found] == ["resting heart rate"]
    assert found[0]["since"] == "2026-10-24"


def test_two_days_is_not_enough(db):
    """One is noise and two is a bad night. Three is a question."""
    seed_days(db, 21, rhr=lambda i: 40 if i < 19 else 45)
    assert anomalies(db, "2026-10-26") == []


def test_the_baseline_excludes_the_days_being_judged(db):
    """Otherwise a sustained rise lifts the mean it is measured against and hides
    itself — the longer it goes on, the less it shows."""
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 44)
    assert anomalies(db, "2026-10-26"), "a 4 bpm rise over a 40 bpm mean is visible"


def test_hrv_below_its_own_floor_is_noticed(db):
    seed_days(db, 21, hrv=lambda i: 70 if i < 18 else 48)
    assert [a["what"] for a in anomalies(db, "2026-10-26")] == ["overnight HRV"]


def test_three_short_nights_are_noticed(db):
    seed_days(db, 21, sleep_h=lambda i: 7.5 if i < 18 else 5.2)
    assert [a["what"] for a in anomalies(db, "2026-10-26")] == ["sleep"]


def test_several_signals_are_all_named(db):
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45,
              sleep_h=lambda i: 7.5 if i < 18 else 5.2)
    assert len(anomalies(db, "2026-10-26")) == 2
    q = should_ask(db, "2026-10-26")
    assert "resting heart rate" in q and "sleep" in q


def test_a_missing_today_asks_nothing(db):
    """The night has not landed. Asking about a day with no data is noise."""
    seed_days(db, 21, end="2026-10-25", rhr=lambda i: 40 if i < 18 else 45)
    assert anomalies(db, "2026-10-26") == []


def test_too_little_history_asks_nothing(db):
    seed_days(db, 4, rhr=45)
    assert anomalies(db, "2026-10-26") == []


def test_a_note_stops_the_asking(db):
    """Rarely asked is what makes it worth answering — so once answered, it stops."""
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45)
    assert should_ask(db, "2026-10-26") != ""
    add_note(db, "2026-10-26", "Náthás vagyok", "illness")
    assert should_ask(db, "2026-10-26") == ""
    assert anomalies(db, "2026-10-26"), "the signal is still off; only the asking stops"


def test_a_recent_note_also_stops_it(db):
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45)
    add_note(db, "2026-10-24", "Utazom", "travel")
    assert should_ask(db, "2026-10-26") == "", "asked and answered two days ago"


def test_an_old_note_does_not(db):
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45)
    add_note(db, "2026-10-10", "régi dolog")
    assert should_ask(db, "2026-10-26") != "", (
        f"a note older than {ASK_COOLDOWN_DAYS} days is a different episode")


def test_the_question_says_a_line_is_enough(db):
    seed_days(db, 21, rhr=lambda i: 40 if i < 18 else 45)
    q = should_ask(db, "2026-10-26")
    assert "A line is enough" in q
    assert "stops this asking" in q, "it says what answering buys"


def test_the_thresholds_are_the_ones_the_rules_already_use():
    from garmin_mcp.brief import RHR_OVER_BASELINE

    assert RHR_OVER_MEAN == RHR_OVER_BASELINE, (
        "the page, the email and the asking must agree about what elevated means")
    assert ANOMALY_DAYS == 3

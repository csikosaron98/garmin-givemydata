"""A record the sync counts as saved must actually be in the database.

Two handlers returned early when a record carried no key they recognised, while
the caller incremented its counter regardless. The result was a sync that
reported success every fifteen minutes, for months, over two permanently empty
tables:

    goals        gql_user_goals: 2 per run, 0 rows
    sleep_stats  sleep_stats: 1 per run,    0 rows

Measured on the live database on 2026-09-08: 947,686 cumulative "records
upserted", and `SELECT COUNT(*) FROM goals` = 0.

The counter now measures what reached the database instead of counting records
it walked past, so this whole class of failure reports itself.
"""

import logging
import sqlite3

import pytest

from garmin_mcp.db import init_db, replace_goals, save_to_db, upsert_goals, upsert_sleep_stats


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---- goals ---------------------------------------------------------------

def test_goal_without_an_id_is_still_stored(temp_db):
    """The exact drop: no id, so the old handler returned without inserting."""
    upsert_goals(temp_db, {"goalType": "STEPS", "goalValue": 12000})
    assert _count(temp_db, "goals") == 1
    row = temp_db.execute("SELECT * FROM goals").fetchone()
    assert row["goal_type"] == "STEPS"
    assert row["goal_value"] == 12000
    assert row["raw_json"], "the untouched payload is kept, whatever its shape"


def test_goal_with_an_id_keeps_it(temp_db):
    upsert_goals(temp_db, {"id": 4242, "goalType": "STEPS", "goalValue": 12000})
    assert temp_db.execute("SELECT goal_id FROM goals").fetchone()[0] == 4242


@pytest.mark.parametrize("key", ["id", "goalId", "userGoalPk", "goalPk", "pk"])
def test_any_of_garmin_s_id_spellings_is_honoured(temp_db, key):
    upsert_goals(temp_db, {key: 7, "goalType": "STEPS"})
    assert temp_db.execute("SELECT goal_id FROM goals").fetchone()[0] == 7


def test_a_numeric_string_id_is_stored_as_a_number(temp_db):
    upsert_goals(temp_db, {"goalId": "13", "goalType": "STEPS"})
    assert temp_db.execute("SELECT goal_id FROM goals").fetchone()[0] == 13


def test_a_non_numeric_id_does_not_lose_the_record(temp_db):
    """goal_id is INTEGER PRIMARY KEY; an opaque string id must not drop the row."""
    upsert_goals(temp_db, {"goalId": "abc-123", "goalType": "STEPS"})
    assert _count(temp_db, "goals") == 1


def test_goals_are_replaced_not_accumulated(temp_db):
    """Goals are current state, not history.

    With no reliable id, upserting row by row would append a row on every sync
    whose payload differed at all — an unbounded table describing two goals.
    """
    replace_goals(temp_db, [{"goalType": "STEPS", "goalValue": 12000},
                            {"goalType": "INTENSITY_MINUTES", "goalValue": 150}])
    assert _count(temp_db, "goals") == 2
    # The same two goals, one of them nudged: still two rows.
    replace_goals(temp_db, [{"goalType": "STEPS", "goalValue": 13000},
                            {"goalType": "INTENSITY_MINUTES", "goalValue": 150}])
    assert _count(temp_db, "goals") == 2
    values = sorted(r[0] for r in temp_db.execute("SELECT goal_value FROM goals"))
    assert values == [150, 13000]


def test_goals_arrive_through_save_to_db(temp_db):
    """End to end, under the endpoint name the sync actually uses."""
    n = save_to_db(temp_db, "gql_user_goals",
                   {"data": {"userGoalsScalar": [{"goalType": "STEPS", "goalValue": 12000}]}})
    assert _count(temp_db, "goals") == 1
    assert n == 1


# ---- sleep_stats ---------------------------------------------------------

def test_sleep_stats_without_a_date_falls_back_to_the_requested_one(temp_db):
    """A range endpoint's payload need not carry a per-record date."""
    upsert_sleep_stats(temp_db, {"avgSleepSeconds": 25000}, cal_date="2026-09-07")
    row = temp_db.execute("SELECT * FROM sleep_stats").fetchone()
    assert row["calendar_date"] == "2026-09-07"


def test_a_record_s_own_date_wins_over_the_requested_one(temp_db):
    """The range can cover more than the day we asked about."""
    upsert_sleep_stats(temp_db, {"calendarDate": "2026-09-05"}, cal_date="2026-09-07")
    assert temp_db.execute("SELECT calendar_date FROM sleep_stats").fetchone()[0] == "2026-09-05"


def test_sleep_stats_with_no_date_at_all_is_still_dropped(temp_db):
    """Nothing to key it by, so it cannot be stored — and must not be counted."""
    n = save_to_db(temp_db, "sleep_stats", [{"avgSleepSeconds": 25000}])
    assert _count(temp_db, "sleep_stats") == 0
    assert n == 0, "a record that was not stored must not be reported as saved"


def test_sleep_stats_arrive_through_save_to_db(temp_db):
    n = save_to_db(temp_db, "sleep_stats", [{"avgSleepSeconds": 25000}], cal_date="2026-09-07")
    assert _count(temp_db, "sleep_stats") == 1
    assert n == 1


# ---- the counter itself --------------------------------------------------

def test_the_counter_reports_what_was_stored_not_what_arrived(temp_db):
    """The general contract, on a handler that legitimately drops a record."""
    n = save_to_db(temp_db, "sleep_stats",
                   [{"calendarDate": "2026-09-07"}, {"no": "date"}, {"date": "2026-09-06"}])
    assert _count(temp_db, "sleep_stats") == 2
    assert n == 2, "three records arrived, two were stored"


def test_one_record_written_by_two_statements_counts_once(temp_db):
    """upsert_heart_rate does INSERT OR IGNORE then UPDATE on the same row.

    Counting statements rather than records would report 2 here, which is what
    made a naive conn.total_changes delta the wrong measure.
    """
    n = save_to_db(temp_db, "heart_rate",
                   [{"calendarDate": "2026-09-07", "restingHeartRate": 39,
                     "maxHeartRate": 187, "minHeartRate": 38}])
    assert _count(temp_db, "heart_rate") == 1
    assert n == 1


def test_storing_nothing_is_reported_as_a_warning(temp_db, caplog):
    """The symptom the old counter hid, now audible."""
    with caplog.at_level(logging.WARNING):
        n = save_to_db(temp_db, "sleep_stats", [{"no": "usable key"}])
    assert n == 0
    assert any("stored none" in r.getMessage() for r in caplog.records), "a total drop must warn"
    assert any("sleep_stats" in r.getMessage() for r in caplog.records), "and must name the endpoint"


def test_a_successful_save_does_not_warn(temp_db, caplog):
    with caplog.at_level(logging.WARNING):
        save_to_db(temp_db, "sleep_stats", [{"calendarDate": "2026-09-07"}])
    assert not [r for r in caplog.records if "stored none" in r.getMessage()]

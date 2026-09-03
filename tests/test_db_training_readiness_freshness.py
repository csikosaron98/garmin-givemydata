"""Tests that the freshest readiness snapshot of a day is the one kept.

Garmin produces several Training Readiness snapshots per day: one on waking
(``inputContext`` ``AFTER_WAKEUP_RESET``) and further ones during the day, for
instance after an activity. ``trainingReadinessRangeScalar`` returns them
newest-first, and ``training_readiness`` holds one row per calendar date, so
the previous ``INSERT OR REPLACE`` let the *last* write win -- the oldest
snapshot. The observed effect was a morning value sitting in the database all
day while Connect showed a newer one: on 2026-09-02 the stored row read
06:30 / 2689 minutes of recovery, while the app showed 07:45 "After Activity"
with a different figure.

Recovery time makes this worse than a stale display. It is a countdown, so a
reader who subtracts elapsed time from the stored value gets a number that
belongs to no timer at all once an activity has restarted it.
"""

import json

import pytest

from garmin_mcp.db import upsert_training_readiness


def _snapshot(date, ts_local, score, recovery, context="UPDATE_REALTIME_VARIABLES"):
    """A readiness record shaped like the API's, with UTC two hours behind."""
    return {
        "calendarDate": date,
        "timestamp": ts_local.replace("T", "T"),
        "timestampLocal": ts_local,
        "inputContext": context,
        "score": score,
        "level": "MODERATE",
        "recoveryTime": recovery,
        "recoveryTimeFactorPercent": 33,
        "hrvFactorPercent": 100,
        "sleepHistoryFactorPercent": 96,
        "stressHistoryFactorPercent": 100,
        "acwrFactorPercent": 86,
    }


def _row(conn, date="2026-09-02"):
    return conn.execute(
        "SELECT score, recovery_time, timestamp_local, input_context"
        " FROM training_readiness WHERE calendar_date = ?",
        (date,),
    ).fetchone()


@pytest.mark.unit
class TestTrainingReadinessFreshness:
    def test_newer_snapshot_replaces_older(self, temp_db):
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T06:30:25.0", 56, 2689, "AFTER_WAKEUP_RESET")
        )
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T19:45:00.0", 20, 4828)
        )
        score, recovery, ts_local, context = _row(temp_db)
        assert score == 20
        assert recovery == 4828
        assert ts_local == "2026-09-02T19:45:00.0"
        assert context == "UPDATE_REALTIME_VARIABLES"

    def test_older_snapshot_does_not_overwrite_newer(self, temp_db):
        """The real failure: the API returns newest-first, so the old one lands last."""
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T19:45:00.0", 20, 4828)
        )
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T06:30:25.0", 56, 2689, "AFTER_WAKEUP_RESET")
        )
        score, recovery, ts_local, _ = _row(temp_db)
        assert score == 20, "an earlier snapshot must not displace a later one"
        assert recovery == 4828
        assert ts_local == "2026-09-02T19:45:00.0"

    def test_every_snapshot_is_kept(self, temp_db):
        for ts, score, recovery in (
            ("2026-09-02T06:30:25.0", 56, 2689),
            ("2026-09-02T19:45:00.0", 20, 4828),
            ("2026-09-03T06:00:33.0", 20, 4828),
        ):
            upsert_training_readiness(temp_db, _snapshot(ts[:10], ts, score, recovery))
        rows = temp_db.execute(
            "SELECT calendar_date, timestamp_local FROM training_readiness_snapshot"
            " ORDER BY calendar_date, timestamp"
        ).fetchall()
        assert [r[1] for r in rows] == [
            "2026-09-02T06:30:25.0",
            "2026-09-02T19:45:00.0",
            "2026-09-03T06:00:33.0",
        ]
        assert temp_db.execute("SELECT COUNT(*) FROM training_readiness").fetchone()[0] == 2

    def test_resyncing_the_same_snapshot_still_refreshes(self, temp_db):
        """Equal timestamps must update: a corrected field would otherwise stick."""
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T06:30:25.0", 56, 2689)
        )
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T06:30:25.0", 58, 2600)
        )
        score, recovery, _, _ = _row(temp_db)
        assert score == 58
        assert recovery == 2600

    def test_untimestamped_snapshot_never_displaces_a_timestamped_one(self, temp_db):
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T19:45:00.0", 20, 4828)
        )
        stray = _snapshot("2026-09-02", "2026-09-02T06:30:25.0", 99, 0)
        stray["timestamp"] = None
        stray["timestampLocal"] = None
        upsert_training_readiness(temp_db, stray)
        score, _, _, _ = _row(temp_db)
        assert score == 20, "a snapshot that cannot be dated must not win"

    def test_row_predating_the_migration_is_replaced(self, temp_db):
        """A row with no timestamp is from before this change and always yields."""
        temp_db.execute(
            "INSERT INTO training_readiness (calendar_date, score, recovery_time)"
            " VALUES ('2026-09-02', 56, 2689)"
        )
        upsert_training_readiness(
            temp_db, _snapshot("2026-09-02", "2026-09-02T19:45:00.0", 20, 4828)
        )
        score, recovery, _, _ = _row(temp_db)
        assert score == 20
        assert recovery == 4828

    def test_missing_calendar_date_is_skipped(self, temp_db):
        upsert_training_readiness(temp_db, {"score": 50, "timestamp": "2026-09-02T06:30:25.0"})
        assert temp_db.execute("SELECT COUNT(*) FROM training_readiness").fetchone()[0] == 0
        assert (
            temp_db.execute("SELECT COUNT(*) FROM training_readiness_snapshot").fetchone()[0] == 0
        )

    def test_raw_json_round_trips(self, temp_db):
        rec = _snapshot("2026-09-02", "2026-09-02T19:45:00.0", 20, 4828)
        upsert_training_readiness(temp_db, rec)
        stored = temp_db.execute(
            "SELECT raw_json FROM training_readiness WHERE calendar_date = '2026-09-02'"
        ).fetchone()[0]
        assert json.loads(stored)["inputContext"] == "UPDATE_REALTIME_VARIABLES"

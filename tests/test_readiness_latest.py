"""The newest Training Readiness snapshot of a day must be the one kept.

Garmin recomputes readiness several times a day — one observed sync returned
five snapshots for a two-day window. `training_readiness.calendar_date` is a
primary key, so only one row per day survives, and `INSERT OR REPLACE` kept
whichever the API happened to list last. In practice that was the wake-up
snapshot: a dashboard reading this table showed a score computed at 00:53 while
the watch showed the current one at 09:04, and the difference looked like a
dashboard bug rather than a storage one.
"""

import json

from garmin_mcp.db import _readiness_snapshot_ts, save_to_db


def _snap(date: str, ts: str | None, score: float) -> dict:
    rec = {"calendarDate": date, "score": score, "level": "MODERATE"}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


def _stored(conn, date: str) -> dict:
    row = conn.execute(
        "SELECT score, raw_json FROM training_readiness WHERE calendar_date = ?", (date,)
    ).fetchone()
    return dict(row) if row else {}


class TestNewestSnapshotWins:
    def test_latest_kept_when_wakeup_arrives_last(self, temp_db):
        """The order that used to decide it: wake-up snapshot listed last."""
        records = [
            _snap("2026-09-05", "2026-09-05T07:15:00.0", 61),
            _snap("2026-09-05", "2026-09-05T09:04:00.0", 48),
            _snap("2026-09-05", "2026-09-05T00:53:00.0", 82),   # wake-up, listed last
        ]
        n = save_to_db(temp_db, "training_readiness", records)

        assert n == 3, "every snapshot is still counted as upserted"
        stored = _stored(temp_db, "2026-09-05")
        assert stored["score"] == 48, "the 09:04 snapshot must win, not the 00:53 one"
        assert json.loads(stored["raw_json"])["timestamp"] == "2026-09-05T09:04:00.0"

    def test_latest_kept_when_already_last(self, temp_db):
        """Ordering must not break the case that happened to work before."""
        records = [
            _snap("2026-09-05", "2026-09-05T00:53:00.0", 82),
            _snap("2026-09-05", "2026-09-05T09:04:00.0", 48),
        ]
        save_to_db(temp_db, "training_readiness", records)
        assert _stored(temp_db, "2026-09-05")["score"] == 48

    def test_separate_days_are_untouched(self, temp_db):
        """One row per day is the schema; the fix must not collapse days."""
        records = [
            _snap("2026-09-04", "2026-09-04T06:00:00.0", 70),
            _snap("2026-09-05", "2026-09-05T09:04:00.0", 48),
            _snap("2026-09-04", "2026-09-04T20:00:00.0", 55),
        ]
        save_to_db(temp_db, "training_readiness", records)
        assert _stored(temp_db, "2026-09-04")["score"] == 55
        assert _stored(temp_db, "2026-09-05")["score"] == 48

    def test_dated_snapshot_beats_undated(self, temp_db):
        """A snapshot with no timestamp must not displace one that has it."""
        records = [
            _snap("2026-09-05", "2026-09-05T09:04:00.0", 48),
            _snap("2026-09-05", None, 99),
        ]
        save_to_db(temp_db, "training_readiness", records)
        assert _stored(temp_db, "2026-09-05")["score"] == 48

    def test_all_undated_falls_back_to_arrival_order(self, temp_db):
        """With nothing to order by, behaviour is the old one: last wins."""
        records = [_snap("2026-09-05", None, 10), _snap("2026-09-05", None, 20)]
        save_to_db(temp_db, "training_readiness", records)
        assert _stored(temp_db, "2026-09-05")["score"] == 20

    def test_single_snapshot_unchanged(self, temp_db):
        save_to_db(temp_db, "training_readiness", [_snap("2026-09-05", "2026-09-05T09:04:00.0", 48)])
        assert _stored(temp_db, "2026-09-05")["score"] == 48


class TestSnapshotSortKey:
    def test_iso_timestamps_sort_chronologically_as_strings(self):
        a = _readiness_snapshot_ts({"timestamp": "2026-09-05T00:53:00.0"})
        b = _readiness_snapshot_ts({"timestamp": "2026-09-05T09:04:00.0"})
        assert a < b

    def test_missing_timestamp_sorts_first(self):
        assert _readiness_snapshot_ts({}) < _readiness_snapshot_ts({"timestamp": "2026-01-01T00:00:00.0"})

    def test_none_timestamp_sorts_first(self):
        assert _readiness_snapshot_ts({"timestamp": None}) == ""

    def test_non_dict_is_tolerated(self):
        assert _readiness_snapshot_ts("nonsense") == ""
        assert _readiness_snapshot_ts(None) == ""

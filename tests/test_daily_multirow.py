"""A day's health metrics and events must all survive storage.

Two tables were keyed on ``calendar_date`` alone while the API returns several
records a day. `INSERT OR REPLACE` then made every record overwrite the one
before it, so the day kept whichever the API happened to list last — silently,
with the sync still counting each one as saved.

Measured on a year-old database before the fix: every one of 353 health_status
rows was a SKIN_TEMP_F reading (five other metrics fetched and discarded daily),
and daily_events held one row per day where Garmin had reported about three.
"""

import json
import sqlite3

import pytest

from garmin_mcp.db import init_db, save_to_db, upsert_daily_events, upsert_health_status


def _rows(conn, table: str, date: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(f"SELECT * FROM {table} WHERE calendar_date = ? ORDER BY rowid", (date,))
    ]


def _legacy_db(create_sql: str) -> sqlite3.Connection:
    """A connection holding the pre-fix table, so the migration has work to do."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(create_sql)
    return conn


# ---------------------------------------------------------------------------
# health_status
# ---------------------------------------------------------------------------


HEALTH_METRICS = [
    {"type": "HRV", "status": "BALANCED", "value": 65},
    {"type": "RHR", "status": "IN_RANGE", "value": 48},
    {"type": "SKIN_TEMP_F", "status": "IN_RANGE", "value": -0.5},
]


@pytest.mark.unit
class TestHealthStatusMetrics:
    def test_every_metric_of_a_day_is_kept(self, temp_db):
        for metric in HEALTH_METRICS:
            upsert_health_status(temp_db, metric, cal_date="2026-09-06")

        rows = _rows(temp_db, "health_status", "2026-09-06")
        assert len(rows) == 3, "each metric needs its own row, not the last one only"
        assert {r["metric_type"] for r in rows} == {"HRV", "RHR", "SKIN_TEMP_F"}

    def test_scalar_columns_are_extracted(self, temp_db):
        upsert_health_status(temp_db, HEALTH_METRICS[0], cal_date="2026-09-06")
        row = _rows(temp_db, "health_status", "2026-09-06")[0]
        assert row["status"] == "BALANCED"
        assert row["value"] == pytest.approx(65.0)
        assert json.loads(row["raw_json"])["type"] == "HRV"

    def test_sync_path_keeps_every_metric(self, temp_db):
        """The shape the live sync actually delivers.

        The client strips the GraphQL ``data`` envelope before handing the
        record over, so what reaches save_to_db under the ``gql_`` name is the
        ``{"metrics": [...]}`` wrapper, which the unwrapper turns into one
        record per metric. Before the fix this returned 3 — three upserts
        counted — and left a single row behind. That mismatch is what made the
        loss invisible: the sync log said everything was saved.
        """
        n = save_to_db(temp_db, "gql_health_status", {"metrics": HEALTH_METRICS}, cal_date="2026-09-06")

        assert n == 3
        assert len(_rows(temp_db, "health_status", "2026-09-06")) == 3

    def test_full_graphql_envelope_is_also_handled(self, temp_db):
        """Callers that do not pre-flatten (import_json) pass the whole response."""
        resp = {"data": {"healthStatusSummary": {"metrics": HEALTH_METRICS}}}
        save_to_db(temp_db, "gql_health_status", resp, cal_date="2026-09-06")
        assert len(_rows(temp_db, "health_status", "2026-09-06")) == 3

    def test_unflattened_wrapper_is_also_accepted(self, temp_db):
        """The non-gql route hands over {"metrics": [...]} whole."""
        save_to_db(temp_db, "health_status", {"metrics": HEALTH_METRICS}, cal_date="2026-09-06")
        assert len(_rows(temp_db, "health_status", "2026-09-06")) == 3

    def test_days_stay_separate(self, temp_db):
        upsert_health_status(temp_db, HEALTH_METRICS[0], cal_date="2026-09-05")
        upsert_health_status(temp_db, HEALTH_METRICS[0], cal_date="2026-09-06")
        assert len(_rows(temp_db, "health_status", "2026-09-05")) == 1
        assert len(_rows(temp_db, "health_status", "2026-09-06")) == 1

    def test_resync_of_a_day_updates_in_place(self, temp_db):
        upsert_health_status(temp_db, {"type": "HRV", "status": "LOW", "value": 40}, cal_date="2026-09-06")
        upsert_health_status(temp_db, {"type": "HRV", "status": "BALANCED", "value": 65}, cal_date="2026-09-06")
        rows = _rows(temp_db, "health_status", "2026-09-06")
        assert len(rows) == 1, "the same metric re-fetched must not duplicate"
        assert rows[0]["status"] == "BALANCED"

    def test_summary_dict_without_metrics_still_stored(self, temp_db):
        save_to_db(
            temp_db,
            "health_status",
            {"calendarDate": "2026-09-06", "overallStatus": "GOOD"},
            cal_date="2026-09-06",
        )
        rows = _rows(temp_db, "health_status", "2026-09-06")
        assert len(rows) == 1
        assert rows[0]["overall_status"] == "GOOD"
        assert rows[0]["metric_type"] == ""

    def test_non_numeric_value_does_not_break_the_row(self, temp_db):
        upsert_health_status(
            temp_db, {"type": "ODD", "status": "X", "value": {"nested": 1}}, cal_date="2026-09-06"
        )
        row = _rows(temp_db, "health_status", "2026-09-06")[0]
        assert row["value"] is None
        assert json.loads(row["raw_json"])["value"] == {"nested": 1}


@pytest.mark.unit
class TestHealthStatusMigration:
    LEGACY = """
        CREATE TABLE health_status (
            calendar_date  TEXT PRIMARY KEY,
            overall_status TEXT,
            raw_json       TEXT
        );
    """

    def test_existing_rows_survive_and_gain_their_type(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO health_status VALUES (?, ?, ?)",
            ("2026-09-01", None, json.dumps({"type": "SKIN_TEMP_F", "status": "IN_RANGE", "value": -0.5})),
        )
        init_db(conn)

        rows = _rows(conn, "health_status", "2026-09-01")
        assert len(rows) == 1
        assert rows[0]["metric_type"] == "SKIN_TEMP_F"
        assert rows[0]["value"] == pytest.approx(-0.5)

    def test_wrapper_rows_are_exploded(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO health_status VALUES (?, ?, ?)",
            ("2026-09-01", None, json.dumps({"metrics": HEALTH_METRICS})),
        )
        init_db(conn)

        rows = _rows(conn, "health_status", "2026-09-01")
        assert {r["metric_type"] for r in rows} == {"HRV", "RHR", "SKIN_TEMP_F"}

    def test_migration_is_idempotent(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO health_status VALUES (?, ?, ?)",
            ("2026-09-01", None, json.dumps({"type": "HRV", "status": "BALANCED", "value": 65})),
        )
        init_db(conn)
        init_db(conn)
        assert len(_rows(conn, "health_status", "2026-09-01")) == 1


# ---------------------------------------------------------------------------
# daily_events
# ---------------------------------------------------------------------------


EVENTS = [
    {
        "activityType": "walking",
        "startTimestampLocal": "2026-09-06T08:10:00.0",
        "endTimestampLocal": "2026-09-06T08:24:00.0",
        "duration": 14,
        "deviceId": 3491364040,
    },
    {
        "activityType": "running",
        "startTimestampLocal": "2026-09-06T09:48:00.0",
        "endTimestampLocal": "2026-09-06T10:50:00.0",
        "duration": 62,
        "deviceId": 3491364040,
    },
    {
        "activityType": "cycling",
        "startTimestampLocal": "2026-09-06T17:18:00.0",
        "endTimestampLocal": "2026-09-06T17:40:00.0",
        "duration": 22,
        "deviceId": 3491364040,
    },
]


@pytest.mark.unit
class TestDailyEventsMultiRow:
    def test_every_event_of_a_day_is_kept(self, temp_db):
        for event in EVENTS:
            upsert_daily_events(temp_db, event, cal_date="2026-09-06")

        rows = _rows(temp_db, "daily_events", "2026-09-06")
        assert len(rows) == 3, "one row per event, not one per day"
        assert {r["activity_type"] for r in rows} == {"walking", "running", "cycling"}

    def test_a_list_stores_each_event(self, temp_db):
        """A list used to be reduced to its first typed event."""
        upsert_daily_events(temp_db, EVENTS, cal_date="2026-09-06")
        assert len(_rows(temp_db, "daily_events", "2026-09-06")) == 3

    def test_sync_path_keeps_all_three(self, temp_db):
        resp = {"data": {"dailyEventsScalar": EVENTS}}
        n = save_to_db(temp_db, "gql_daily_events", resp, cal_date="2026-09-06")
        assert n == 3
        assert len(_rows(temp_db, "daily_events", "2026-09-06")) == 3

    def test_scalar_fields_still_extracted(self, temp_db):
        upsert_daily_events(temp_db, EVENTS[1], cal_date="2026-09-06")
        row = _rows(temp_db, "daily_events", "2026-09-06")[0]
        assert row["activity_type"] == "running"
        assert row["start_timestamp_local"] == "2026-09-06T09:48:00.0"
        assert row["end_timestamp_local"] == "2026-09-06T10:50:00.0"
        assert row["duration_seconds"] == pytest.approx(62.0)
        assert row["device_id"] == "3491364040"

    def test_resync_of_an_event_updates_in_place(self, temp_db):
        upsert_daily_events(temp_db, EVENTS[0], cal_date="2026-09-06")
        upsert_daily_events(temp_db, {**EVENTS[0], "duration": 15}, cal_date="2026-09-06")
        rows = _rows(temp_db, "daily_events", "2026-09-06")
        assert len(rows) == 1
        assert rows[0]["duration_seconds"] == pytest.approx(15.0)

    def test_event_without_local_start_falls_back_to_gmt(self, temp_db):
        upsert_daily_events(
            temp_db,
            {"activityType": "walking", "startTimestampGMT": "2026-09-06T06:10:00.0"},
            cal_date="2026-09-06",
        )
        row = _rows(temp_db, "daily_events", "2026-09-06")[0]
        assert row["start_timestamp_local"] == "2026-09-06T06:10:00.0"

    def test_startless_event_still_lands(self, temp_db):
        upsert_daily_events(temp_db, {"duration": 60.0}, cal_date="2026-09-06")
        rows = _rows(temp_db, "daily_events", "2026-09-06")
        assert len(rows) == 1
        assert rows[0]["start_timestamp_local"] == ""

    def test_days_stay_separate(self, temp_db):
        upsert_daily_events(temp_db, EVENTS[0], cal_date="2026-09-06")
        upsert_daily_events(temp_db, {**EVENTS[0], "startTimestampLocal": "2026-09-05T08:10:00.0"},
                            cal_date="2026-09-05")
        assert len(_rows(temp_db, "daily_events", "2026-09-05")) == 1
        assert len(_rows(temp_db, "daily_events", "2026-09-06")) == 1


@pytest.mark.unit
class TestDailyEventsMigration:
    LEGACY = """
        CREATE TABLE daily_events (
            calendar_date         TEXT PRIMARY KEY,
            activity_type         TEXT,
            activity_sub_type     TEXT,
            start_timestamp_local TEXT,
            end_timestamp_local   TEXT,
            duration_seconds      REAL,
            device_id             TEXT,
            raw_json              TEXT
        );
    """

    def test_existing_row_survives_with_its_start_as_key(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO daily_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-01", "cycling", None, "2026-09-01T17:18:00.0",
             "2026-09-01T17:40:00.0", 22.0, "3491364040", json.dumps(EVENTS[2])),
        )
        init_db(conn)

        rows = _rows(conn, "daily_events", "2026-09-01")
        assert len(rows) == 1
        assert rows[0]["activity_type"] == "cycling"
        assert rows[0]["start_timestamp_local"] == "2026-09-01T17:18:00.0"

    def test_legacy_list_row_is_exploded(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO daily_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-06", "walking", None, None, None, None, None, json.dumps(EVENTS)),
        )
        init_db(conn)

        rows = _rows(conn, "daily_events", "2026-09-06")
        assert {r["activity_type"] for r in rows} == {"walking", "running", "cycling"}

    def test_migration_is_idempotent(self):
        conn = _legacy_db(self.LEGACY)
        conn.execute(
            "INSERT INTO daily_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-01", "cycling", None, "2026-09-01T17:18:00.0", None, 22.0, None,
             json.dumps(EVENTS[2])),
        )
        init_db(conn)
        init_db(conn)
        assert len(_rows(conn, "daily_events", "2026-09-01")) == 1

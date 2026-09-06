"""garmin_today must carry the readiness snapshot's own timestamp.

The dashboard refreshes garmin_today every five minutes but loads the readiness
history only once per page load. It then ages `recovery_time` — a countdown —
from whatever timestamp it holds. When the value came from the five-minute
refresh and the timestamp from page load, the interval between them was counted
twice and the countdown read shorter than it was: optimistic in exactly the
direction that matters, since it is what tells you whether to train today.

Carrying the timestamp beside the value makes the two refresh together.
"""

import json
import sqlite3
from datetime import date
from unittest.mock import patch

from garmin_mcp.db import upsert_training_readiness
from garmin_mcp.server import garmin_today


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(db_path: str, **fields) -> None:
    conn = _open(db_path)
    rec = {"calendarDate": str(date.today())}
    rec.update(fields)
    upsert_training_readiness(conn, rec)
    conn.commit()
    conn.close()


def _readiness(db_path: str) -> dict:
    fn = garmin_today.fn if hasattr(garmin_today, "fn") else garmin_today
    with patch("garmin_mcp.server.get_connection", lambda: _open(db_path)):
        return json.loads(fn())["training_readiness"]


def test_timestamp_is_returned(temp_db_file):
    today = str(date.today())
    _seed(temp_db_file, score=73, level="MODERATE", recoveryTime=288,
          timestamp=today + "T05:51:23.0", timestampLocal=today + "T07:51:23.0")

    tr = _readiness(temp_db_file)
    assert tr["score"] == 73
    assert tr["timestamp"] == today + "T05:51:23.0", (
        "without this the dashboard cannot tell how old the score is"
    )
    assert tr["timestamp_local"] == today + "T07:51:23.0"


def test_absent_timestamp_is_null_not_an_error(temp_db_file):
    _seed(temp_db_file, score=50, level="MODERATE")

    tr = _readiness(temp_db_file)
    assert tr["score"] == 50
    assert tr["timestamp"] is None
    assert tr["timestamp_local"] is None


def test_recovery_time_still_present(temp_db_file):
    """The value the timestamp exists to date must not have been dropped."""
    today = str(date.today())
    _seed(temp_db_file, score=73, recoveryTime=288, timestamp=today + "T05:51:23.0")

    assert _readiness(temp_db_file)["recovery_time"] == 288

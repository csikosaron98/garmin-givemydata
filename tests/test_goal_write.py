"""The only writable tool on this server.

Everything else is read-only at the SQLite engine level, so this tool is the one
place where a published page can change the database. The tests are therefore
less about it working and more about it being *narrow*: one table, no SQL, and
the same validator the command line uses.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from garmin_mcp import server as S
from garmin_mcp.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real file database, with the server pointed at it.

    The tool opens its own connection through get_connection(), so an in-memory
    database would not be the one it writes to.
    """
    path = tmp_path / "garmin.db"
    conn = sqlite3.connect(path)
    init_db(conn)
    conn.close()
    monkeypatch.setattr("garmin_mcp.db.DB_PATH", str(path))
    monkeypatch.setattr(S, "get_connection",
                        lambda *a, **k: sqlite3.connect(path))
    return path


def call(**kw):
    return json.loads(S.garmin_goal_write(**kw))


def rows(path, sql="SELECT * FROM training_goal"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


# ---- it writes what it says ------------------------------------------------

def test_a_race_can_be_added(db):
    out = call(action="add", kind="race", name="Wien Hyrox", discipline="hyrox",
               race_date="2027-02-20", target="64:00", priority=1)
    assert out["ok"] is True and out["goal_id"] == 1
    stored = rows(db)
    assert len(stored) == 1
    assert stored[0]["name"] == "Wien Hyrox" and stored[0]["race_date"] == "2027-02-20"


def test_a_standing_goal_can_be_added(db):
    out = call(action="add", kind="standing", name="Maintenance",
               standing="maintenance")
    assert out["ok"] is True
    assert rows(db)[0]["standing"] == "maintenance"
    assert rows(db)[0]["priority"] == 2, "the default priority is applied, not NULL"


def test_a_goal_can_be_updated(db):
    gid = call(action="add", kind="race", name="Wien Hyrox", discipline="hyrox",
               race_date="2027-02-20")["goal_id"]
    assert call(action="update", goal_id=gid, target="63:30")["ok"] is True
    assert rows(db)[0]["target"] == "63:30"
    assert rows(db)[0]["race_date"] == "2027-02-20", "nothing else moved"


def test_retiring_deactivates_and_keeps_the_row(db):
    gid = call(action="add", kind="standing", name="Fat loss", standing="fat_loss")["goal_id"]
    assert call(action="retire", goal_id=gid)["ok"] is True
    stored = rows(db)
    assert len(stored) == 1, "retiring must not delete history"
    assert stored[0]["active"] == 0


# ---- it is narrow ---------------------------------------------------------

def test_it_takes_no_sql(db):
    """The whole reason this is acceptable on a published page. Every field is a
    named parameter, so there is no string for a caller to put SQL into."""
    import inspect

    params = set(inspect.signature(S.garmin_goal_write).parameters)
    assert "sql" not in params and "query" not in params and "where" not in params
    assert params == {"action", "goal_id", "kind", "name", "discipline", "standing",
                      "race_date", "category", "target", "priority", "notes"}


def test_sql_in_a_field_is_stored_as_text_not_executed(db):
    """A name is a name. If it were interpolated anywhere, this would drop a table."""
    nasty = "x'; DROP TABLE activity; --"
    gid = call(action="add", kind="standing", name=nasty, standing="maintenance")["goal_id"]
    assert rows(db)[0]["name"] == nasty, "stored verbatim"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='activity'").fetchone()[0] == 1
    finally:
        conn.close()
    assert call(action="update", goal_id=gid, notes=nasty)["ok"] is True


def test_it_touches_no_other_table(db):
    """Garmin's own data must be untouchable through this tool."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO daily_summary (calendar_date, total_steps) VALUES ('2026-10-01', 9999)")
    conn.execute("INSERT INTO goals (goal_id, goal_type) VALUES (77, 'steps')")
    conn.commit()
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("daily_summary", "goals", "activity", "sleep")}
    conn.close()

    call(action="add", kind="race", name="R", discipline="running", race_date="2027-01-01")
    call(action="update", goal_id=1, notes="n")
    call(action="retire", goal_id=1)

    conn = sqlite3.connect(db)
    try:
        after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("daily_summary", "goals", "activity", "sleep")}
        assert after == before
        assert conn.execute("SELECT total_steps FROM daily_summary").fetchone()[0] == 9999
    finally:
        conn.close()


def test_the_garmin_goals_table_is_a_different_thing(db):
    """`goals` is Garmin's own step and intensity targets; `training_goal` is
    Áron's races. The tool name was garmin_goals first and silently shadowed the
    existing tool — the server logged "Tool already exists" and kept the old one."""
    assert hasattr(S, "garmin_goals"), "Garmin's own goals tool still exists"
    assert hasattr(S, "garmin_training_goals"), "and the training-goal reader is separate"
    assert S.garmin_goals is not S.garmin_training_goals


# ---- it validates exactly as the command line does ------------------------

def test_an_unknown_action_is_refused(db):
    assert "error" in call(action="delete", goal_id=1)
    assert rows(db) == []


def test_a_race_without_a_date_is_refused(db):
    out = call(action="add", kind="race", name="Someday", discipline="hyrox")
    assert "date" in out["error"]
    assert rows(db) == [], "nothing is stored on a rejected goal"


def test_an_unknown_discipline_is_refused_with_the_allowed_values(db):
    out = call(action="add", kind="race", name="R", discipline="underwater_chess",
               race_date="2027-01-01")
    assert "discipline" in out["error"] and "hyrox" in out["error"], (
        "the page shows this message, so it has to name what is allowed")


def test_an_unknown_standing_goal_is_refused(db):
    assert "standing" in call(action="add", kind="standing", name="x",
                              standing="get_swole")["error"]


def test_a_nameless_goal_is_refused(db):
    assert "name" in call(action="add", kind="standing", name="  ",
                          standing="maintenance")["error"]


def test_update_cannot_set_a_field_add_would_not_accept(db):
    """Otherwise the page has a back door into an unreadable goal."""
    call(action="add", kind="race", name="R", discipline="running", race_date="2027-01-01")
    assert "discipline" in call(action="update", goal_id=1,
                                discipline="underwater_chess")["error"]
    assert rows(db)[0]["discipline"] == "running"


def test_update_and_retire_need_an_id(db):
    assert "goal_id" in call(action="update", target="x")["error"]
    assert "goal_id" in call(action="retire")["error"]


def test_retiring_something_that_is_not_there_is_an_error_not_a_silent_ok(db):
    assert "error" in call(action="retire", goal_id=999)


def test_updating_nothing_says_so(db):
    call(action="add", kind="standing", name="M", standing="maintenance")
    assert "error" in call(action="update", goal_id=1)


# ---- the reader -----------------------------------------------------------

def test_the_reader_returns_the_derived_target_too(db, monkeypatch):
    """The page could compute the target itself from the rows, and does — but the
    email computes it here. Returning it means a difference between the two is
    visible rather than silent."""
    call(action="add", kind="race", name="Wien Hyrox", discipline="hyrox",
         race_date="2027-02-20", target="64:00", priority=1)
    out = json.loads(S.garmin_training_goals())
    assert out["driving_goal_id"] == 1
    assert out["phase"] in ("base", "build", "peak", "taper", "race_week")
    assert out["target"]["sessions"] > 0
    assert out["target"]["source"], "a target with no source is indistinguishable from invented"
    assert "Wien Hyrox" in out["describe"]
    g = out["goals"][0]
    assert g["label"] == "HYROX" and g["weeks_until"] is not None


def test_the_reader_includes_retired_goals_so_the_page_can_show_history(db):
    gid = call(action="add", kind="race", name="Roma Hyrox", discipline="hyrox",
               race_date="2026-09-27")["goal_id"]
    call(action="retire", goal_id=gid)
    out = json.loads(S.garmin_training_goals())
    assert len(out["goals"]) == 1 and out["goals"][0]["active"] is False
    assert out["driving_goal_id"] is None, "a retired race drives nothing"


def test_the_reader_works_with_no_goals_at_all(db):
    out = json.loads(S.garmin_training_goals())
    assert out["goals"] == [] and out["driving_goal_id"] is None
    assert out["target"]["sessions"] > 0, "the balanced default is still returned"
    assert out["phase"] is None

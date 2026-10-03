"""The morning brief: when it sends, and what it decides.

Two things have to hold. The ceiling may only ever be LOWERED, and whichever
rule set it has to be the one reported — a brief that says "hard" without being
able to say why nothing stopped it is an assertion, not a recommendation.

And it must not send outside the morning. `training_readiness` holds the day's
most recent snapshot, so an evening send would read a post-session score and
print it as a waking value. That is the exact failure the scheduled-assistant
version had: measured over 22 days it sent at 13:33 and at 21:18.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from garmin_mcp.brief import (
    LEVELS,
    already_sent,
    build,
    decide,
    in_send_window,
    is_hard,
    mark_sent,
    read_day,
    render_html,
    render_text,
    rhr_baseline,
    smtp_config,
)
from garmin_mcp.db import init_db


# A day on which nothing binds: readiness PRIME, no recovery outstanding,
# HRV mid-baseline, resting HR at its mean, a full night.
GOOD_DAY = {
    "calendar_date": "2026-10-02", "readiness": 100.0, "readiness_level": "PRIME",
    "recovery_time": 0.0, "sleep_time_seconds": 29459, "sleep_h": 8.18,
    "sleep_score": 93, "hrv": 62.0, "hrv_status": "BALANCED",
    "hrv_low": 51.0, "hrv_high": 83.0, "hrv_weekly": 63.0,
    "rhr": 40, "bb_wake": 94, "stress": 14,
    "training_status": "PRODUCTIVE_3", "acute_load": 457.0, "chronic_load": 682.0,
}


def day_with(**over):
    d = dict(GOOD_DAY)
    d.update(over)
    return d


# ---- the ladder ----------------------------------------------------------

def test_a_clear_day_reaches_hard():
    out = decide(GOOD_DAY, 39.7, [])
    assert out["level"] == "hard"
    assert out["limiter"] is None


def test_an_unbound_day_still_says_what_came_closest():
    """"Hard" with no explanation is an assertion. It has to show its working."""
    out = decide(GOOD_DAY, 39.7, [])
    assert "resting HR" in out["detail"]
    assert "40" in out["detail"]


@pytest.mark.parametrize("readiness,expected", [
    (100, "hard"), (80, "hard"), (74, "moderate"), (49, "recovery"), (20, "rest"),
])
def test_readiness_bands_set_the_ceiling(readiness, expected):
    out = decide(day_with(readiness=readiness), 39.7, [])
    assert out["level"] == expected


def test_recovery_time_binds_on_an_otherwise_perfect_day():
    """The signal that most often binds when everything else looks fine."""
    out = decide(day_with(recovery_time=2880), 39.7, [])   # 48 h
    assert out["level"] == "recovery"
    assert out["limiter"] == "recovery time"
    assert "48 h" in out["detail"]


def test_a_shorter_countdown_only_reaches_easy():
    out = decide(day_with(recovery_time=900), 39.7, [])    # 15 h
    assert out["level"] == "easy"
    assert out["limiter"] == "recovery time"


def test_hrv_below_his_own_floor_forces_easy():
    out = decide(day_with(hrv=48.0), 39.7, [])             # floor is 51
    assert out["level"] == "easy"
    assert out["limiter"] == "HRV"


def test_hrv_inside_the_baseline_does_not_bind():
    out = decide(day_with(hrv=52.0), 39.7, [])             # floor is 51
    assert out["limiter"] != "HRV"


def test_resting_heart_rate_binds_only_past_the_threshold():
    # Mean 39.7, so the line sits at 42.7: 42 is inside, 43 is not.
    assert decide(day_with(rhr=42), 39.7, [])["limiter"] != "resting heart rate"
    out = decide(day_with(rhr=43), 39.7, [])
    assert out["level"] == "easy"
    assert out["limiter"] == "resting heart rate"


def test_a_short_night_drops_one_level_rather_than_pinning_one():
    """Sleep is a modifier, not a verdict: it lowers whatever the day had."""
    assert decide(day_with(sleep_h=5.0), 39.7, [])["level"] == "moderate"
    # Already capped at moderate by readiness, so it drops to easy.
    assert decide(day_with(sleep_h=5.0, readiness=60), 39.7, [])["level"] == "easy"


def test_two_hard_days_in_three_caps_at_moderate():
    hard = {"d": "2026-10-01", "anaerobic_training_effect": 3.2, "aerobic_training_effect": 3.4}
    hard2 = dict(hard, d="2026-09-30")
    out = decide(GOOD_DAY, 39.7, [hard, hard2])
    assert out["level"] == "moderate"
    assert out["limiter"] == "recent load"


def test_two_sessions_on_one_day_are_one_hard_day():
    """Counting sessions rather than days would cap a day off two easy doubles."""
    hard = {"d": "2026-10-01", "anaerobic_training_effect": 3.2, "aerobic_training_effect": 3.4}
    out = decide(GOOD_DAY, 39.7, [hard, dict(hard)])
    assert out["level"] == "hard"


def test_the_lowest_ceiling_wins_and_is_the_one_reported():
    out = decide(day_with(readiness=60, recovery_time=3000, hrv=45.0), 39.7, [])
    assert out["level"] == "recovery"
    assert out["limiter"] == "recovery time", "the rule that set the final level is the one named"


def test_the_load_ratio_is_described_but_never_binds():
    """Post-2020 work refuted ACWR for injury prediction; it is context only."""
    out = decide(day_with(acute_load=1200.0, chronic_load=700.0), 39.7, [])
    assert out["level"] == "hard"
    assert any("1.71" in n for n in out["notes"])


def test_a_ceiling_is_never_raised():
    """The whole shape of the rule set: every signal can only lower it."""
    worst = decide(day_with(readiness=10, recovery_time=5000, hrv=20.0, rhr=60, sleep_h=3), 39.7, [])
    assert worst["level"] == "rest"
    assert LEVELS.index(worst["level"]) == 0


# ---- hard-day classification --------------------------------------------

def test_a_long_easy_run_is_not_a_hard_day():
    assert not is_hard({"aerobic_training_effect": 2.8, "anaerobic_training_effect": 0.0,
                        "training_load": 150})


def test_a_short_interval_set_is():
    assert is_hard({"aerobic_training_effect": 3.1, "anaerobic_training_effect": 3.2,
                    "training_load": 90})


def test_a_missing_training_effect_is_not_assumed_hard():
    assert not is_hard({"training_load": 400})


# ---- the send window -----------------------------------------------------

@pytest.mark.parametrize("hour,ok", [(4, False), (5, True), (7, True), (10, True), (11, False), (21, False)])
def test_the_send_window_is_the_morning(hour, ok):
    assert in_send_window(dt.datetime(2026, 10, 2, hour, 30)) is ok


def test_the_marker_stops_a_second_send_on_the_same_day(tmp_path):
    marker = tmp_path / ".brief-sent"
    assert not already_sent("2026-10-02", marker)
    mark_sent("2026-10-02", marker)
    assert already_sent("2026-10-02", marker)
    assert not already_sent("2026-10-03", marker), "a new day sends again"


def test_an_unwritable_marker_does_not_crash(tmp_path):
    """Better a duplicate tomorrow than an exception after the mail has gone."""
    mark_sent("2026-10-02", tmp_path / "nope" / "deeper" / ".brief-sent")


# ---- configuration -------------------------------------------------------

def test_missing_mail_settings_are_named_without_leaking_values(monkeypatch):
    for k in ("BRIEF_TO", "BRIEF_SMTP_USER", "BRIEF_SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    cfg = smtp_config()
    assert set(cfg["missing"]) == {"BRIEF_TO", "BRIEF_SMTP_USER", "BRIEF_SMTP_PASSWORD"}


def test_a_missing_setting_is_named_as_the_variable_that_is_actually_read(monkeypatch):
    """The first live run reported BRIEF_PASSWORD, which nothing reads.

    An error message that sends you to add the wrong variable is worse than no
    message, so the names reported and the names looked up come from one map.
    """
    from garmin_mcp.brief import ENV_KEYS
    for env in ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    reported = set(smtp_config()["missing"])
    assert reported == set(ENV_KEYS.values())
    for env in reported:
        monkeypatch.setenv(env, "x")
        assert env not in smtp_config()["missing"], (
            f"{env} was reported missing, but setting it changes nothing - "
            "the message names a variable the code does not read")


def test_a_configured_password_is_never_put_in_the_missing_report(monkeypatch):
    monkeypatch.setenv("BRIEF_TO", "a@b.c")
    monkeypatch.setenv("BRIEF_SMTP_USER", "a@b.c")
    monkeypatch.setenv("BRIEF_SMTP_PASSWORD", "hunter2")
    cfg = smtp_config()
    assert cfg["missing"] == []
    assert "hunter2" not in str(cfg["missing"])


# ---- against a real database --------------------------------------------

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed(conn, date, **over):
    d = day_with(calendar_date=date, **over)
    conn.execute("INSERT OR REPLACE INTO training_readiness (calendar_date, score, level, recovery_time) VALUES (?,?,?,?)",
                 (date, d["readiness"], d["readiness_level"], d["recovery_time"]))
    conn.execute("INSERT OR REPLACE INTO sleep (calendar_date, sleep_time_seconds, sleep_score_overall) VALUES (?,?,?)",
                 (date, d["sleep_time_seconds"], d["sleep_score"]))
    conn.execute("INSERT OR REPLACE INTO hrv (calendar_date, last_night_avg, status, baseline_low, baseline_upper) VALUES (?,?,?,?,?)",
                 (date, d["hrv"], d["hrv_status"], d["hrv_low"], d["hrv_high"]))
    conn.execute("INSERT OR REPLACE INTO daily_summary (calendar_date, resting_heart_rate, body_battery_at_wake, average_stress_level) VALUES (?,?,?,?)",
                 (date, d["rhr"], d["bb_wake"], d["stress"]))
    conn.commit()


def test_nothing_is_built_before_the_night_has_landed(db):
    """The watch has not uploaded. Not an error — the next run retries."""
    db.execute("INSERT INTO training_readiness (calendar_date, score) VALUES ('2026-10-02', 100)")
    db.commit()
    assert read_day(db, "2026-10-02") is None, "readiness without sleep is not enough"
    assert build(db, "2026-10-02") is None


def test_a_complete_day_builds_all_three_parts(db):
    _seed(db, "2026-10-02")
    subject, text, html = build(db, "2026-10-02")
    assert subject == "Training brief — Friday 2 October"
    assert "TODAY" in text
    assert html.lstrip().startswith("<div")
    assert "<style" not in html, "mail clients strip style blocks; everything must be inline"


def test_the_baseline_excludes_the_day_itself(db):
    """Averaging today into its own baseline flattens the excursion worth seeing."""
    for i, rhr in enumerate([39, 39, 40, 40]):
        db.execute("INSERT INTO daily_summary (calendar_date, resting_heart_rate) VALUES (?,?)",
                   (f"2026-09-2{i + 5}", rhr))
    db.execute("INSERT INTO daily_summary (calendar_date, resting_heart_rate) VALUES ('2026-10-02', 60)")
    db.commit()
    assert rhr_baseline(db, "2026-10-02") == 39.5


def test_the_html_carries_no_unresolved_placeholders(db):
    _seed(db, "2026-10-02")
    _, _, html = build(db, "2026-10-02")
    assert "{" not in html.replace("{", "", 0) or "None" not in html
    assert "None" not in html, "a missing value must render as a dash, not as the word None"


# ---- the window belongs to the athlete, not the server -------------------

def test_the_window_is_read_in_the_athletes_timezone(monkeypatch):
    """The VM runs on UTC. A naive now() made the window 07:00-13:00 Budapest in
    summer, so a 06:00 riser waited an hour for a brief that was already ready.
    """
    from garmin_mcp.brief import local_now
    monkeypatch.setenv("BRIEF_TZ", "Europe/Budapest")
    assert local_now().utcoffset() is not None, "the time must carry a zone"
    monkeypatch.setenv("BRIEF_TZ", "UTC")
    assert local_now().utcoffset() == dt.timedelta(0)


def test_an_early_riser_is_inside_the_window():
    """03:30 UTC is 05:30 in Budapest in summer — inside, not outside."""
    utc = dt.datetime(2026, 7, 1, 3, 30, tzinfo=dt.timezone.utc)
    assert not in_send_window(utc), "the raw UTC hour is outside"
    budapest = utc.astimezone(dt.timezone(dt.timedelta(hours=2)))
    assert in_send_window(budapest), "the same instant in his own morning is inside"


def test_an_unknown_timezone_does_not_crash(monkeypatch):
    from garmin_mcp.brief import local_now
    monkeypatch.setenv("BRIEF_TZ", "Mars/Olympus_Mons")
    assert isinstance(local_now(), dt.datetime)


# ---- the goal reaches the email ------------------------------------------

def _add_activity(conn, day, time, sport, minutes, *, load=50, z1=0, z2=0, z4=0):
    cur = conn.execute("SELECT COALESCE(MAX(activity_id), 5000) + 1 FROM activity")
    aid = cur.fetchone()[0]
    conn.execute("INSERT INTO activity (activity_id, activity_type, start_time_local, "
                 "duration_seconds, distance_meters, training_load) VALUES (?,?,?,?,?,?)",
                 (aid, sport, f"{day} {time}:00", minutes * 60, 0, load))
    conn.execute("INSERT INTO activity_hr_zones (activity_id, zone1_seconds, "
                 "zone2_seconds, zone3_seconds, zone4_seconds, zone5_seconds) "
                 "VALUES (?,?,?,?,?,?)", (aid, z1, z2, 0, z4, 0))
    conn.commit()


def test_with_no_goal_the_brief_still_reads_the_week(db):
    """A goal is optional. "Set a goal first" is a useless thing to receive at
    six in the morning."""
    _seed(db, "2026-10-02")
    _add_activity(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    subject, text, html = build(db, "2026-10-02")
    assert "THIS WEEK" in text
    assert "goal" not in text.lower().split("THIS WEEK")[0], \
        "nothing should claim a goal that was not set"


def test_the_goal_appears_in_both_halves_of_the_email(db):
    """The plain-text and HTML halves each used to call the advice themselves,
    which is one edit away from the two describing the same morning differently."""
    from garmin_mcp.goals import Goal, add_goal

    _seed(db, "2026-10-02")
    add_goal(db, Goal(kind="standing", name="Down to 73 kg", standing="fat_loss"))
    _add_activity(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    subject, text, html = build(db, "2026-10-02")
    for half, body in (("text", text), ("html", html)):
        assert "Fat loss" in body, f"the goal is missing from the {half} half"
        assert "Down to 73 kg" in body, f"the goal's name is missing from the {half} half"


def test_a_race_puts_its_phase_in_the_brief(db):
    from garmin_mcp.goals import Goal, add_goal

    _seed(db, "2026-10-02")
    add_goal(db, Goal(kind="race", name="Hyrox Budapest", discipline="hyrox",
                      race_date="2026-12-19", target="62:00"))
    _add_activity(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    subject, text, html = build(db, "2026-10-02")
    assert "Hyrox Budapest" in text and "Build phase" in text
    assert "62:00" in text, "the target time is part of the goal"


def test_a_broken_goal_table_costs_the_brief_nothing_but_the_goal(db):
    """The rest of the email is about today, which does not depend on the goal."""
    _seed(db, "2026-10-02")
    db.execute("DROP TABLE training_goal")
    db.commit()
    built = build(db, "2026-10-02")
    assert built is not None, "the brief must still go out"
    assert "TODAY" in built[1]


def test_the_week_s_remainder_reaches_both_halves_of_the_email(db):
    """The whole point of the feature is that the morning email says it. Mutation
    found this missing: deleting the line from the plain-text half broke no test
    at all, because every assertion lived one layer down in goals.py."""
    from garmin_mcp.goals import Goal, add_goal

    _seed(db, "2026-10-02")
    add_goal(db, Goal(kind="race", name="Wien Hyrox", discipline="hyrox",
                      race_date="2027-02-20", priority=1))
    # A thin week against a base-phase target: something is certainly owed.
    _add_activity(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    subject, text, html = build(db, "2026-10-02")
    for half, body in (("text", text), ("html", html)):
        assert "the week still owes" in body, f"the focus line is missing from the {half} half"


def test_the_two_halves_say_the_same_thing_about_today(db):
    """They each call the advice themselves, which is one edit away from the two
    describing the same morning differently."""
    from garmin_mcp.goals import Goal, add_goal

    _seed(db, "2026-10-02")
    add_goal(db, Goal(kind="race", name="Wien Hyrox", discipline="hyrox",
                      race_date="2027-02-20", priority=1))
    _add_activity(db, "2026-09-28", "11:00", "strength_training", 70, load=12)
    subject, text, html = build(db, "2026-10-02")
    from garmin_mcp.brief import decide, read_day, recent_sessions, rhr_baseline, todays_focus_line
    from garmin_mcp.week import summarize

    day = read_day(db, "2026-10-02")
    line = todays_focus_line(
        decide(day, rhr_baseline(db, "2026-10-02"), recent_sessions(db, "2026-10-02")),
        summarize(db, "2026-10-02"),
        Goal(kind="race", name="Wien Hyrox", discipline="hyrox",
             race_date="2027-02-20", priority=1),
        "2026-10-02")
    assert line and line in text and line in html


def test_a_rest_day_email_does_not_ask_for_the_week_s_work(db):
    """The ceiling comes first, and the email has to read that way too."""
    from garmin_mcp.goals import Goal, add_goal

    # Short sleep alone only lowers the ceiling a step; it takes a poor readiness
    # with it to reach a rest day, which is the case worth testing here.
    _seed(db, "2026-10-02", sleep_time_seconds=3 * 3600, readiness=10)
    add_goal(db, Goal(kind="race", name="Wien Hyrox", discipline="hyrox",
                      race_date="2027-02-20", priority=1))
    subject, text, html = build(db, "2026-10-02")
    assert "Rest day" in text
    assert "not today" in text, "it names what waits rather than silently dropping it"
    assert "fits today" not in text, "and asks for nothing"


def test_the_prescriptions_name_no_sport(db):
    """They describe the intensity the day allows; WHICH session comes from the
    week's remainder. Naming a sport here contradicted that line directly."""
    from garmin_mcp.brief import PRESCRIPTIONS

    for level, (headline, body) in PRESCRIPTIONS.items():
        joined = (headline + " " + body).lower()
        for sport in ("running", "run.", "a run", "cycling", "swim", "gym"):
            assert sport not in joined, f"{level} prescribes a sport: {headline}"

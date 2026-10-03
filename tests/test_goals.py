"""The goal system: the store, which goal drives a week, and the gap to it.

These tests go through the real table and the real target tables, because the
value of this module is entirely in those numbers being the ones that ship. A
test against a mock of the targets would pass on any numbers at all.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_mcp.db import init_db
from garmin_mcp.goals import (
    DEFAULT_TARGET,
    PHASE_BASE,
    PHASE_BUILD,
    PHASE_PEAK,
    PHASE_RACE,
    PHASE_TAPER,
    RACE_DISCIPLINES,
    RACE_TARGETS,
    STANDING_KINDS,
    STANDING_TARGETS,
    Goal,
    active_goals,
    add_goal,
    all_goals,
    describe_goal,
    driving_goal,
    gaps,
    met,
    observations,
    phase_for,
    retire_goal,
    target_for,
    weeks_until,
)
from garmin_mcp.week import summarize


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def race(db, name="Roma Hyrox", date="2026-12-20", discipline="hyrox", **kw):
    return add_goal(db, Goal(kind="race", name=name, discipline=discipline,
                             race_date=date, **kw))


def standing(db, what="fat_loss", name=None, **kw):
    return add_goal(db, Goal(kind="standing", name=name or what, standing=what, **kw))


# ---- the store ------------------------------------------------------------

def test_a_race_and_a_standing_goal_both_store_and_come_back(db):
    race(db)
    standing(db)
    got = all_goals(db)
    assert {g.kind for g in got} == {"race", "standing"}
    assert all(g.goal_id for g in got), "every stored goal gets an id"


def test_a_race_without_a_date_is_refused(db):
    """A race with no date has no phase, so it would silently train as base
    forever — which is the one failure mode that looks like it is working."""
    with pytest.raises(ValueError, match="date"):
        add_goal(db, Goal(kind="race", name="Someday", discipline="hyrox"))


def test_an_unknown_discipline_is_refused(db):
    with pytest.raises(ValueError, match="discipline"):
        race(db, discipline="underwater_chess")


def test_an_unknown_standing_goal_is_refused(db):
    """Not a typo-catcher: an unreadable value falls back to the DEFAULT target,
    so the page would show plausible advice for a goal nobody set."""
    with pytest.raises(ValueError, match="standing"):
        standing(db, what="get_swole")


def test_a_nameless_goal_is_refused(db):
    with pytest.raises(ValueError, match="name"):
        add_goal(db, Goal(kind="standing", name="   ", standing="maintenance"))


def test_retiring_hides_a_goal_without_destroying_it(db):
    gid = race(db)
    assert retire_goal(db, gid) is True
    assert all_goals(db) == []
    assert len(all_goals(db, include_retired=True)) == 1, "the history is kept"


def test_retiring_something_that_is_not_there_says_so(db):
    assert retire_goal(db, 9999) is False


def test_a_race_already_run_stops_applying(db):
    """Nobody retires a goal the morning after a race, and the day after one is
    not still race week."""
    race(db, date="2026-09-27")
    assert all_goals(db), "it is still stored"
    assert active_goals(db, "2026-10-02") == [], "but it no longer applies"


# ---- which goal drives the week ------------------------------------------

def test_a_race_outranks_a_standing_goal(db):
    standing(db, "fat_loss")
    race(db, date="2026-12-20")
    g = driving_goal(db, "2026-10-02")
    assert g.kind == "race", "a date does not move; a standing goal does"


def test_the_nearer_race_wins(db):
    race(db, name="Later", date="2027-03-01")
    race(db, name="Sooner", date="2026-11-15")
    assert driving_goal(db, "2026-10-02").name == "Sooner"


def test_priority_breaks_a_tie_between_two_races_on_a_day(db):
    race(db, name="B race", date="2026-11-15", priority=2)
    race(db, name="A race", date="2026-11-15", priority=1)
    assert driving_goal(db, "2026-10-02").name == "A race"


def test_with_several_standing_goals_priority_decides(db):
    standing(db, "maintenance", priority=3)
    standing(db, "strength", priority=1)
    assert driving_goal(db, "2026-10-02").standing == "strength"


def test_no_goal_at_all_is_a_legitimate_state(db):
    assert driving_goal(db, "2026-10-02") is None
    target, phase = target_for(None, "2026-10-02")
    assert target is DEFAULT_TARGET
    assert phase is None, "a default target is in no phase"


# ---- phases ---------------------------------------------------------------

def test_weeks_until_rounds_up(db):
    """Nine days out is the last-but-one week. Rounding down would taper early."""
    assert weeks_until("2026-10-11", "2026-10-02") == 2
    assert weeks_until("2026-10-09", "2026-10-02") == 1
    assert weeks_until("2026-10-02", "2026-10-02") == 0
    assert weeks_until("2026-09-27", "2026-10-02") == 0, "a past race is zero, not negative"


@pytest.mark.parametrize("race_date,expected", [
    ("2027-06-01", PHASE_BASE),     # far out
    ("2026-12-01", PHASE_BUILD),    # ~9 weeks
    ("2026-10-26", PHASE_PEAK),     # ~4 weeks
    ("2026-10-14", PHASE_TAPER),    # 2 weeks
    ("2026-10-05", PHASE_RACE),     # this week
])
def test_the_phase_follows_the_date(race_date, expected):
    assert phase_for(race_date, "2026-10-02") == expected


def test_every_discipline_defines_every_phase():
    """A missing phase is a KeyError on the one morning it matters."""
    for discipline in RACE_DISCIPLINES:
        table = RACE_TARGETS[discipline]
        for phase in (PHASE_BASE, PHASE_BUILD, PHASE_PEAK, PHASE_TAPER, PHASE_RACE):
            assert phase in table, f"{discipline} has no {phase} target"


def test_the_taper_asks_for_less_than_the_peak():
    """The whole point of a taper. Getting this backwards would be invisible in
    any test that only checked the phases existed."""
    for discipline in RACE_DISCIPLINES:
        peak = RACE_TARGETS[discipline][PHASE_PEAK]
        taper = RACE_TARGETS[discipline][PHASE_TAPER]
        week = RACE_TARGETS[discipline][PHASE_RACE]
        assert taper.sessions < peak.sessions, f"{discipline} does not taper"
        assert week.sessions <= taper.sessions, f"{discipline} race week is not easiest"
        assert taper.quality >= 1, (
            f"{discipline} drops all intensity in the taper — volume comes down, "
            "intensity is kept")


def test_every_standing_goal_has_a_target_and_a_source():
    for kind in STANDING_KINDS:
        t = STANDING_TARGETS[kind]
        assert t.source, f"{kind} has no stated source — an unsourced target is invented"
        assert t.focus, f"{kind} says nothing about what the week is for"


def test_the_strength_goals_ask_for_the_wider_separation():
    """Interference falls with the gap, so a goal about strength asks for more
    than the general six hours."""
    for kind in ("strength", "muscle"):
        assert STANDING_TARGETS[kind].separation_hours >= 8.0
    assert STANDING_TARGETS["fat_loss"].separation_hours == 6.0


def test_muscle_gain_asks_for_more_strength_than_fat_loss_or_maintenance():
    s = STANDING_TARGETS
    assert s["muscle"].strength > s["maintenance"].strength
    assert s["muscle"].strength >= s["strength"].strength


def test_only_the_strength_side_goals_cap_hard_aerobic_work():
    s = STANDING_TARGETS
    assert s["strength"].max_hard == 1 and s["muscle"].max_hard == 1
    assert s["fat_loss"].max_hard is None, (
        "capping the aerobic side of a fat-loss week would be the wrong lever")
    assert s["maintenance"].max_hard is None


def test_fat_loss_keeps_the_strength_volume():
    """It is the finding, not a detail: the weight comes off either way, and what
    is kept is decided by the resistance work."""
    assert STANDING_TARGETS["fat_loss"].strength >= 3


# ---- the gap --------------------------------------------------------------

def _week(db, *sessions):
    """sessions = (day, time, sport, minutes, kwargs) tuples."""
    from tests.test_week import add
    for s in sessions:
        add(db, *s[:4], **(s[4] if len(s) > 4 else {}))
    return summarize(db, "2026-10-02")


def test_a_shortfall_is_named_with_both_numbers(db):
    s = _week(db, ("2026-09-28", "11:00", "strength_training", 70, {"load": 12}))
    g = gaps(s, STANDING_TARGETS["muscle"])
    assert any("strength sessions" == x.what for x in g)
    strength = [x for x in g if x.what == "strength sessions"][0]
    assert (strength.have, strength.want) == (1, 4)


def test_a_week_that_meets_its_target_reports_nothing_missing(db):
    s = _week(db,
              ("2026-09-28", "11:00", "strength_training", 70, {"load": 12}),
              ("2026-09-29", "11:00", "strength_training", 70, {"load": 12}),
              ("2026-09-30", "09:00", "running", 70, {"load": 80, "z1": 1200, "z2": 3000}),
              ("2026-10-01", "09:00", "running", 65, {"load": 80, "z1": 1200, "z2": 2700}),
              ("2026-10-01", "18:00", "hiit", 50, {"load": 160, "z4": 1200}),
              ("2026-10-02", "09:00", "cycling", 40, {"load": 30, "z1": 1200, "z2": 1200}))
    target = STANDING_TARGETS["maintenance"]
    assert met(s, target), [f"{x.what} {x.have}/{x.want}" for x in gaps(s, target)]


def test_the_ceiling_is_reported_as_an_overshoot_not_a_shortfall(db):
    s = _week(db,
              ("2026-09-28", "18:00", "hiit", 50, {"load": 200, "z4": 1200}),
              ("2026-09-30", "18:00", "running", 50, {"load": 160, "z4": 1200}))
    over = [x for x in gaps(s, STANDING_TARGETS["strength"]) if x.over]
    assert len(over) == 1
    assert over[0].what == "hard aerobic sessions"
    assert (over[0].have, over[0].want) == (2, 1)


def test_a_long_session_is_measured_in_minutes_not_sessions(db):
    s = _week(db, ("2026-09-28", "09:00", "running", 40,
                   {"load": 70, "z1": 1200, "z2": 1200}))
    long_gap = [x for x in gaps(s, STANDING_TARGETS["maintenance"])
                if "longest aerobic" in x.what]
    assert len(long_gap) == 1
    assert long_gap[0].want == 60 and long_gap[0].have == 40
    assert long_gap[0].unit == "min", (
        "without a unit this reads as a count of sessions, not a duration")


# ---- the advice -----------------------------------------------------------

def test_the_advice_opens_by_naming_the_goal(db):
    standing(db, "fat_loss", name="Down to 72 kg")
    s = _week(db, ("2026-09-28", "11:00", "strength_training", 70, {"load": 12}))
    out = observations(s, driving_goal(db, "2026-10-02"), "2026-10-02")
    assert "Fat loss" in out[0] and "Down to 72 kg" in out[0]


def test_the_advice_states_what_it_is_judged_against(db):
    """A target with no stated source cannot be argued with, which makes it
    indistinguishable from one that was made up."""
    standing(db, "muscle")
    s = _week(db)
    out = observations(s, driving_goal(db, "2026-10-02"), "2026-10-02")
    assert any("Judged against" in line for line in out)
    assert any("Schoenfeld" in line for line in out)


def test_the_advice_never_names_a_day(db):
    race(db, date="2026-11-15")
    s = _week(db,
              ("2026-09-28", "11:00", "strength_training", 70, {"load": 12}),
              ("2026-09-28", "13:00", "running", 40, {"load": 80, "z1": 600, "z2": 1200}))
    for line in observations(s, driving_goal(db, "2026-10-02"), "2026-10-02"):
        low = line.lower()
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday",
                        "saturday", "sunday"):
            assert weekday not in low, f"advice named a day: {line}"


def test_a_race_week_says_how_far_away_the_race_is(db):
    race(db, name="Budapest Half", date="2026-11-15", discipline="running",
         target="1:28:00")
    line = describe_goal(driving_goal(db, "2026-10-02"), "2026-10-02")
    assert "2026-11-15" in line and "7 weeks" in line
    assert "1:28:00" in line, "the target time is part of the goal, not decoration"
    assert "Peak" in line or "Build" in line


def test_the_race_is_singular_a_week_before(db):
    race(db, date="2026-10-09")
    assert "in 1 week" in describe_goal(driving_goal(db, "2026-10-02"), "2026-10-02")


def test_interference_is_judged_against_this_goal_s_own_gap(db):
    """Seven hours is fine for a fat-loss week and not for a strength one. A
    single global threshold would have to be wrong for one of them."""
    sessions = (("2026-10-02", "11:00", "strength_training", 70, {"load": 12}),
                ("2026-10-02", "18:00", "running", 40,
                 {"load": 160, "z4": 900, "z1": 600}))
    s = _week(db, *sessions)
    assert s["interference"] == [], "7 h is outside week.py's own 6 h window"

    strength_out = observations(s, Goal(kind="standing", name="s", standing="strength"),
                                "2026-10-02")
    # week.py only reports gaps under six hours, so nothing reaches the goal
    # layer here — the goal's wider window is applied to what it is given, and
    # the test pins that the two thresholds are not silently assumed equal.
    assert not any("8 h between" in line for line in strength_out)
    assert STANDING_TARGETS["strength"].separation_hours > 6.0


def test_an_empty_week_still_gets_advice(db):
    standing(db, "maintenance")
    out = observations(_week(db), driving_goal(db, "2026-10-02"), "2026-10-02")
    assert len(out) >= 3, "a week with nothing in it is when advice matters most"
    assert any("of 2" in line or "of 6" in line for line in out)


# ---- the command line -----------------------------------------------------
# Thin, but it is the only way a goal gets set, so the one thing worth pinning
# is that it does not lie about what it just stored.

def test_the_cli_reports_the_same_phase_as_the_listing(db, tmp_path, capsys):
    """`race` and `list` ran different values through phase_for: the race's own
    positional `date` and the global --date flag shared one argparse attribute,
    so the race date became "today" and a race twelve weeks out was announced as
    race week."""
    import sqlite3

    from garmin_mcp.goals import main

    path = tmp_path / "goals.db"
    conn = sqlite3.connect(path)
    init_db(conn)
    conn.close()

    args = ["--db", str(path), "--as-of", "2026-10-02"]
    assert main(args + ["race", "Hyrox Budapest", "2026-12-19",
                        "--discipline", "hyrox"]) == 0
    added = capsys.readouterr().out
    assert main(args + ["list"]) == 0
    listed = capsys.readouterr().out
    assert "build" in added.lower(), added
    assert "Build phase" in listed, listed


def test_the_cli_refuses_a_bad_goal_loudly(db, tmp_path):
    """A stored goal nobody can read falls back to the default target, so the
    page would show confident advice for a goal that was never set."""
    import sqlite3

    from garmin_mcp.goals import main

    path = tmp_path / "goals.db"
    conn = sqlite3.connect(path)
    init_db(conn)
    conn.close()
    with pytest.raises(SystemExit):     # argparse rejects the choice itself
        main(["--db", str(path), "goal", "get_swole"])


def test_the_shape_of_the_week_still_gets_said_with_a_goal_set(db):
    """"All of it was hard" is a judgement no target can carry as a number, and
    it was only reachable through the goal-free reading before."""
    standing(db, "maintenance")
    s = _week(db, ("2026-09-28", "18:00", "hiit", 60, {"load": 200, "z4": 1800}))
    out = " ".join(observations(s, driving_goal(db, "2026-10-02"), "2026-10-02"))
    assert "Easy volume" in out


def test_that_line_is_defined_in_one_place(db):
    """Two modules wording the same finding differently is the drift the
    week/goal split exists to avoid."""
    from garmin_mcp.week import insights

    s = _week(db, ("2026-09-28", "18:00", "hiit", 60, {"load": 200, "z4": 1800}))
    shared = insights(s)
    assert shared, "the fixture should trigger an insight"
    goal_lines = observations(s, Goal(kind="standing", name="m", standing="maintenance"),
                              "2026-10-02")
    for line in shared:
        assert line in goal_lines, f"the goal layer reworded: {line}"


# ---- the taper's ceiling --------------------------------------------------
# Found on live data: a taper asking for 4 sessions said nothing about a week
# with 6 in it. `sessions` was a floor everywhere, so the one week whose whole
# purpose is less work could not tell itself apart from any other week.

def test_every_taper_and_race_week_caps_the_session_count():
    for discipline in RACE_DISCIPLINES:
        for phase in (PHASE_TAPER, PHASE_RACE):
            t = RACE_TARGETS[discipline][phase]
            assert t.max_sessions == t.sessions, (
                f"{discipline} {phase} does not cap its session count, so going "
                "over it passes silently")


def test_the_building_phases_do_not_cap_it():
    """Nagging someone who trains most days for training is worse than silence."""
    for discipline in RACE_DISCIPLINES:
        for phase in (PHASE_BASE, PHASE_BUILD, PHASE_PEAK):
            assert RACE_TARGETS[discipline][phase].max_sessions is None, \
                f"{discipline} {phase} caps sessions, which is not what a build week is"
    for kind in STANDING_KINDS:
        assert STANDING_TARGETS[kind].max_sessions is None


def test_a_taper_week_with_too_many_sessions_says_so(db):
    """Áron's real week when this was found: six sessions against a taper's four."""
    race(db, name="Spar", date="2026-10-11", discipline="running", priority=1)
    s = _week(db,
              ("2026-09-28", "09:00", "running", 51, {"load": 155, "z4": 740, "z1": 600, "z2": 1380}),
              ("2026-09-29", "11:00", "strength_training", 72, {"load": 12}),
              ("2026-09-29", "22:00", "soccer", 64, {"load": 23, "z1": 600, "z2": 360}),
              ("2026-09-30", "11:00", "strength_training", 81, {"load": 94, "z4": 324, "z1": 1980}),
              ("2026-10-01", "11:00", "strength_training", 81, {"load": 12, "z1": 4000}),
              ("2026-10-02", "12:00", "running", 23, {"load": 80, "z1": 600, "z2": 780}))
    goal = driving_goal(db, "2026-10-02")
    target, phase = target_for(goal, "2026-10-02")
    assert phase == PHASE_TAPER and target.max_sessions == 4

    over = [g for g in gaps(s, target) if g.over]
    assert len(over) == 1 and over[0].what == "sessions this week"
    assert (over[0].have, over[0].want) == (6, 4)

    out = " ".join(observations(s, goal, "2026-10-02"))
    assert "at most 4" in out
    assert "Coming down is the point" in out, (
        "the taper's overshoot needs its own sentence — the interference wording "
        "is about strength and says nothing about tapering")


def test_the_two_ceilings_do_not_borrow_each_other_s_sentence(db):
    """One phrasing for both read as "the interference costs more strength" on a
    taper week, which is true of neither the cause nor the remedy."""
    standing(db, "strength")
    s = _week(db,
              ("2026-09-28", "18:00", "hiit", 50, {"load": 200, "z4": 1200}),
              ("2026-09-30", "18:00", "running", 50, {"load": 160, "z4": 1200}))
    out = " ".join(observations(s, driving_goal(db, "2026-10-02"), "2026-10-02"))
    assert "interference costs more" in out, "the hard-aerobic ceiling keeps its wording"
    assert "Coming down is the point" not in out


# ---- editing a stored goal -----------------------------------------------

def test_a_target_can_be_added_after_the_race_was_entered(db):
    """The usual case: the race goes in when it is booked, the target time is
    settled later. Retiring and re-adding would lose the id and the created date
    for what is only a correction."""
    from garmin_mcp.goals import update_goal

    gid = race(db, name="Hyrox BP", date="2026-10-17")
    assert update_goal(db, gid, target="64:00", notes="Doubles, with Levi") is True
    g = [x for x in all_goals(db) if x.goal_id == gid][0]
    assert g.target == "64:00" and g.notes == "Doubles, with Levi"
    assert g.race_date == "2026-10-17", "nothing else moved"


def test_setting_nothing_changes_nothing(db):
    from garmin_mcp.goals import update_goal

    gid = race(db)
    assert update_goal(db, gid) is False
    assert update_goal(db, gid, target=None) is False


def test_a_field_that_is_not_editable_is_refused(db):
    """`active` has retire_goal, and `kind` would strand a goal in a target table
    that cannot read it."""
    from garmin_mcp.goals import update_goal

    gid = race(db)
    with pytest.raises(ValueError, match="active"):
        update_goal(db, gid, active=0)
    with pytest.raises(ValueError, match="kind"):
        update_goal(db, gid, kind="standing")


def test_an_invalid_value_is_refused_on_update_too(db):
    """The add path validates; so must this one, or the same unreadable goal gets
    in through the back door."""
    from garmin_mcp.goals import update_goal

    gid = race(db)
    with pytest.raises(ValueError, match="discipline"):
        update_goal(db, gid, discipline="underwater_chess")
    with pytest.raises(ValueError):
        update_goal(db, gid, race_date="not-a-date")


def test_updating_a_goal_that_is_not_there_says_so(db):
    from garmin_mcp.goals import update_goal

    assert update_goal(db, 9999, target="x") is False


# ---- what the week still owes, and what of it fits today ------------------
# The brief already decides how HARD today may be. This is the other half: WHAT
# to spend the day on. The ceiling is never raised here — a week short of a long
# run is not a reason to train on a day the body says no.

from garmin_mcp.goals import (  # noqa: E402
    FOCUS_ORDER,
    LEVELS,
    QUALITY_MIN_LEVEL,
    days_left,
    todays_focus,
    week_remainder,
)


def test_the_level_ladder_matches_the_brief_s_own():
    """A copy, because brief imports this module — so it has to be pinned."""
    from garmin_mcp.brief import LEVELS as BRIEF_LEVELS

    assert LEVELS == BRIEF_LEVELS


def test_every_focus_kind_has_a_minimum_level():
    for kind in FOCUS_ORDER:
        assert QUALITY_MIN_LEVEL[kind] in LEVELS


def test_days_left_counts_today(db):
    assert days_left("2026-10-05") == 7, "Monday leaves the whole week"
    assert days_left("2026-10-09") == 3, "Friday leaves Friday, Saturday, Sunday"
    assert days_left("2026-10-11") == 1, "Sunday leaves only Sunday"


def _target(db, goal_kw, date):
    gid = add_goal(db, Goal(**goal_kw))
    return target_for(driving_goal(db, date), date)[0], gid


def test_a_hard_day_goes_to_the_quality_session(db):
    """Scarcity: a long run only needs a day with time in it, a quality session
    needs a day the body will take — and those are rarer."""
    t, _ = _target(db, dict(kind="race", name="W", discipline="hyrox",
                            race_date="2027-02-20", priority=1), "2026-11-04")
    s = _week(db, ("2026-11-02", "11:00", "strength_training", 70, {"load": 12}))
    out = todays_focus("hard", summarize(db, "2026-11-04"), t, "2026-11-04")
    assert out["focus"] == "aerobic_quality"
    assert "quality session" in out["line"]


def test_a_moderate_day_cannot_take_the_quality_session(db):
    """Which is exactly why a hard day is spent on one."""
    t, _ = _target(db, dict(kind="race", name="W", discipline="hyrox",
                            race_date="2027-02-20", priority=1), "2026-11-04")
    _week(db, ("2026-11-02", "11:00", "strength_training", 70, {"load": 12}))
    out = todays_focus("moderate", summarize(db, "2026-11-04"), t, "2026-11-04")
    assert out["focus"] == "long", "it falls to the next thing the day allows"
    assert str(t.long_minutes) in out["line"], "and states the length that counts"


def test_a_rest_day_is_never_overridden_by_the_week(db):
    """The ceiling comes first. This is the rule that keeps the feature honest."""
    t, _ = _target(db, dict(kind="race", name="W", discipline="hyrox",
                            race_date="2027-02-20", priority=1), "2026-11-04")
    _week(db)
    for level in ("rest", "recovery"):
        out = todays_focus(level, summarize(db, "2026-11-04"), t, "2026-11-04")
        assert out["focus"] is None
        assert "not today" in out["line"]


def test_a_week_already_at_its_goal_says_so(db):
    t, _ = _target(db, dict(kind="standing", name="M", standing="maintenance"),
                   "2026-11-04")
    s = _week(db,
              ("2026-11-02", "11:00", "strength_training", 70, {"load": 12}),
              ("2026-11-03", "11:00", "strength_training", 70, {"load": 12}),
              ("2026-11-03", "09:00", "running", 70, {"load": 80, "z1": 1200, "z2": 3000}),
              ("2026-11-04", "09:00", "running", 65, {"load": 80, "z1": 1200, "z2": 2700}),
              ("2026-11-04", "18:00", "hiit", 50, {"load": 160, "z4": 1200}),
              ("2026-11-04", "06:00", "cycling", 40, {"load": 30, "z1": 1200, "z2": 1200}))
    out = todays_focus("hard", summarize(db, "2026-11-04"), t, "2026-11-04")
    assert out["focus"] is None and "met its goal" in out["line"]


def test_a_taper_at_its_ceiling_owes_nothing(db):
    """Asking for more in a taper would contradict the point of the week, however
    much is nominally "missing"."""
    t, _ = _target(db, dict(kind="race", name="Spar", discipline="running",
                            race_date="2026-10-11", priority=1), "2026-10-02")
    _week(db, *[(d, "11:00", "strength_training", 70, {"load": 12})
                for d in ("2026-09-28", "2026-09-29", "2026-09-30",
                          "2026-10-01", "2026-10-02")])
    s = summarize(db, "2026-10-02")
    rem = week_remainder(s, t, "2026-10-02")
    assert rem["capped"] is True and rem["sessions_owed"] == 0
    out = todays_focus("hard", s, t, "2026-10-02")
    assert out["focus"] is None and "taper" in out["line"]


def test_more_owed_than_days_left_is_said_plainly(db):
    """Honest beats encouraging: a plan that cannot be followed is worse than
    being told to pick what matters."""
    t, _ = _target(db, dict(kind="race", name="W", discipline="hyrox",
                            race_date="2027-02-20", priority=1), "2026-11-07")
    _week(db)
    out = todays_focus("hard", summarize(db, "2026-11-07"), t, "2026-11-07")
    assert "will fall short" in out["line"]
    assert "2 days left" in out["line"]


def test_a_feasible_week_is_not_told_it_will_fall_short(db):
    t, _ = _target(db, dict(kind="race", name="W", discipline="hyrox",
                            race_date="2027-02-20", priority=1), "2026-11-02")
    _week(db)
    out = todays_focus("hard", summarize(db, "2026-11-02"), t, "2026-11-02")
    assert "fall short" not in out["line"], "Monday has the whole week ahead"


def test_the_long_session_is_not_counted_as_an_extra_session(db):
    """It is a property of one of the base sessions, not work of its own —
    counting it separately would overstate what the week owes."""
    t, _ = _target(db, dict(kind="standing", name="M", standing="maintenance"),
                   "2026-11-04")
    _week(db,
          ("2026-11-02", "09:00", "running", 40, {"load": 70, "z1": 1200, "z2": 1200}),
          ("2026-11-03", "09:00", "running", 40, {"load": 70, "z1": 1200, "z2": 1200}))
    rem = week_remainder(summarize(db, "2026-11-04"), t, "2026-11-04")
    assert rem["long_missing"] is True
    assert rem["owed"].get("aerobic_base") is None, "both base sessions are done"
    # One quality and two strength. The long session is missing as well, and the
    # count stays at three: it is a longer version of a base session that is
    # already counted, not a fourth session to fit in.
    assert rem["owed"] == {"aerobic_quality": 1, "strength": 2}
    assert rem["sessions_owed"] == 3


def test_an_unknown_level_returns_nothing_rather_than_guessing(db):
    t, _ = _target(db, dict(kind="standing", name="M", standing="maintenance"),
                   "2026-11-04")
    out = todays_focus("extremely hard", summarize(db, "2026-11-04"), t, "2026-11-04")
    assert out["focus"] is None and out["line"] == ""


# ---- the half Ironman ------------------------------------------------------

def test_the_half_ironman_is_its_own_discipline():
    """Not "other", and not the full distance: the same framework with shorter
    long sessions. Áron named it, so it has to exist as itself."""
    assert "half_ironman" in RACE_DISCIPLINES
    table = RACE_TARGETS["half_ironman"]
    for phase in (PHASE_BASE, PHASE_BUILD, PHASE_PEAK, PHASE_TAPER, PHASE_RACE):
        assert phase in table
        assert table[phase].source, f"{phase} has no stated source"


def test_the_half_ironman_asks_for_less_than_the_full_one():
    """Otherwise the distinction is decorative."""
    for phase in (PHASE_BASE, PHASE_BUILD, PHASE_PEAK):
        half = RACE_TARGETS["half_ironman"][phase]
        full = RACE_TARGETS["ironman"][phase]
        assert half.long_minutes < full.long_minutes, (
            f"the 70.3 {phase} long session is not shorter than the full distance's")


def test_the_half_ironman_long_session_grows_through_the_block():
    t = RACE_TARGETS["half_ironman"]
    assert (t[PHASE_BASE].long_minutes < t[PHASE_BUILD].long_minutes
            < t[PHASE_PEAK].long_minutes), "the long ride is what the block builds"
    assert t[PHASE_TAPER].long_minutes < t[PHASE_PEAK].long_minutes


# ---- what the notes say ----------------------------------------------------
# Free text, read by recognising a documented vocabulary rather than by
# interpreting the sentence: the morning email runs on a server with no model in
# it, so anything the page could interpret the email could not.

from garmin_mcp.goals import (  # noqa: E402
    NOTE_MARKERS,
    WeeklyTarget,
    apply_notes,
    read_notes,
    unread_note,
)


def _race(notes, discipline="hyrox"):
    return Goal(kind="race", name="R", discipline=discipline,
                race_date="2027-01-01", notes=notes)


def test_doubles_is_read_from_a_sentence_in_hungarian(db):
    """Áron's real note. Whole-word matching, so it is found inside a sentence."""
    r = read_notes(_race("Doubles, Levivel — mint Rómában (66:30)"))
    assert r["found"] == ["doubles"]
    assert r["effect"] == {"base": 1}
    assert r["why"] and "8 km" in r["why"][0], "it says why, not just what"


def test_doubles_moves_the_target_it_claims_to(db):
    base_target = RACE_TARGETS["hyrox"][PHASE_BUILD]
    moved, _ = apply_notes(base_target, _race("doubles"))
    assert moved.base == base_target.base + 1
    assert moved.sessions == base_target.sessions, (
        "a note changes the MIX of a week, not how much of it there is")


def test_the_pro_category_moves_the_strength_side(db):
    base_target = RACE_TARGETS["hyrox"][PHASE_BUILD]
    moved, _ = apply_notes(base_target, _race("Pro category"))
    assert moved.strength == base_target.strength + 1
    assert read_notes(_race("elite"))["found"] == ["pro category"], "elite means pro"


def test_the_standard_markers_change_nothing(db):
    """Open and singles are what the targets already assume. They are recognised
    so the page can say it read them, not because they move anything."""
    base_target = RACE_TARGETS["hyrox"][PHASE_BUILD]
    for note in ("open", "singles"):
        moved, r = apply_notes(base_target, _race(note))
        assert r["found"], f"{note} is recognised"
        assert moved == base_target, f"{note} moves nothing"


def test_a_contradictory_note_applies_neither(db):
    """"Pro kategória, open nem" names both. Adding the effects would be nonsense
    and picking one would be a guess."""
    r = read_notes(_race("Pro kategória, open nem"))
    assert r["found"] == [] and r["effect"] == {}
    assert len(r["conflicts"]) == 1 and "category" in r["conflicts"][0]
    assert "contradicts itself" in unread_note(_race("Pro kategória, open nem"))


def test_two_markers_from_different_groups_both_apply(db):
    r = read_notes(_race("doubles, pro"))
    assert set(r["found"]) == {"doubles", "pro category"}
    assert r["effect"] == {"base": 1, "strength": 1}


def test_a_marker_for_another_discipline_is_ignored(db):
    """"Doubles" means nothing in a running race, and reading it there would move
    a target for no reason."""
    assert read_notes(_race("doubles", discipline="running"))["found"] == []


def test_a_note_the_rules_do_not_know_says_so(db):
    """A note that looks acted upon and is not is worse than one plainly skipped."""
    said = unread_note(_race("Szóló, Bécs"))
    assert "Nothing in the note changes the targets" in said
    assert "doubles" in said, "and it lists what would have worked"


def test_an_empty_note_says_nothing_at_all(db):
    assert unread_note(_race(None)) == ""
    assert unread_note(_race("")) == ""
    assert read_notes(None)["found"] == []


def test_a_marker_inside_a_longer_word_is_not_a_marker(db):
    """Whole words only, or "openly" would set the category."""
    assert read_notes(_race("openly competitive"))["found"] == []


def test_the_notes_reach_the_target_everyone_uses(db):
    """Applied inside target_for, so the page, the email and the plan all see the
    adjusted target without having to remember to ask for it."""
    gid = add_goal(db, Goal(kind="race", name="Hyrox BP", discipline="hyrox",
                            race_date="2027-01-01", priority=1,
                            notes="Doubles, Levivel"))
    goal = [g for g in all_goals(db) if g.goal_id == gid][0]
    adjusted, phase = target_for(goal, "2026-10-03")
    plain = RACE_TARGETS["hyrox"][phase]
    assert adjusted.base == plain.base + 1


def test_a_floor_is_never_pushed_below_zero(db):
    """A relay lowers the aerobic side, and a target with -1 base sessions in it
    would make every gap calculation nonsense."""
    t = WeeklyTarget(3, base=0, quality=1, strength=1, long_minutes=0)
    moved, _ = apply_notes(t, _race("relay"))
    assert moved.base == 0


def test_every_marker_declares_a_group_and_a_reason():
    for marker, spec in NOTE_MARKERS.items():
        assert spec["group"], f"{marker} has no group, so it can contradict nothing"
        assert spec["why"], f"{marker} changes a target without saying why"
        assert spec["disciplines"], f"{marker} applies to no discipline"

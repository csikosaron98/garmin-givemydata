"""What the athlete is training FOR, and what that asks of a week.

The week module says what a week CONTAINS. This one says what it should contain,
which is a different question and has a different answer depending on the goal.
A race is a date with a shape that changes as it approaches; a standing goal —
maintenance, strength, muscle, fat loss — has no date and the same shape every
week.

Three deliberate choices:

1. **A goal is never required.** Without one the week is still described and
   still compared against a sensible default, because "set a goal first" is a
   useless answer to "how is my week going".

2. **The targets are expressed in the same five qualities week.py classifies
   into.** A goal that asked for "3 runs" would need a second classifier, and
   two classifiers disagree sooner or later. A goal asks for aerobic base,
   quality, strength — which is also what the body responds to.

3. **Advisory, not prescriptive.** A target is a number to compare against, not
   a schedule. Nothing here names a day: which day a session lands on is
   settled on the day, usually by work and weather.

Nothing here is medical advice, and nothing here prescribes food.
"""

from __future__ import annotations

import datetime as _dt
import re as _re
import sqlite3
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

RACE_DISCIPLINES = ("hyrox", "running", "half_ironman", "ironman", "other")
STANDING_KINDS = ("maintenance", "strength", "muscle", "fat_loss")

STANDING_LABELS = {
    "maintenance": "Maintenance",
    "strength": "Strength gain",
    "muscle": "Muscle gain",
    "fat_loss": "Fat loss",
}

DISCIPLINE_LABELS = {
    "hyrox": "HYROX",
    "running": "Running race",
    "half_ironman": "Half Ironman (70.3)",
    "ironman": "Ironman",
    "other": "Race",
}

# Phase boundaries in weeks remaining. These are the conventional blocks of a
# linear periodisation; the names are what the plan literature calls them.
PHASE_BASE = "base"
PHASE_BUILD = "build"
PHASE_PEAK = "peak"
PHASE_TAPER = "taper"
PHASE_RACE = "race_week"

PHASE_LABELS = {
    PHASE_BASE: "Base",
    PHASE_BUILD: "Build",
    PHASE_PEAK: "Peak",
    PHASE_TAPER: "Taper",
    PHASE_RACE: "Race week",
}

# Five phases need four boundaries, each read as "fewer than this many whole
# weeks remain". The 13-week outer edge is deliberate: the published 12-week
# race blocks start where this one ends, so "base" is what a week is before any
# block has begun, not a phase inside one.
RACE_WEEK_UNDER = 2     # the last seven days, and the race day itself
TAPER_UNDER = 4         # two weeks of coming down
PEAK_UNDER = 7          # the three hardest weeks
BUILD_UNDER = 13        # the race-specific block


@dataclass(frozen=True)
class WeeklyTarget:
    """What one goal asks of one week. Every field is a floor except max_hard."""

    sessions: int
    base: int
    quality: int
    strength: int
    long_minutes: int
    # A CEILING, not a floor, and only the strength-side goals set one: beyond
    # about one hard aerobic session a week the interference starts to cost more
    # strength than the aerobic work is worth to that goal. None = no ceiling.
    max_hard: int | None = None
    # The other ceiling, and only a taper or a race week sets one. Everywhere
    # else `sessions` is a floor and going over it is fine — but a taper whose
    # whole purpose is less work has to be able to say "that is more than this
    # week was for", or it cannot tell a taper from any other week. A base week
    # does not set it: nagging someone who trains most days for training is
    # worse than silence.
    max_sessions: int | None = None
    # The separation this goal cares about between resistance work and hard
    # endurance work. Six hours is where interference becomes measurable; past
    # eight it is minimal, so a strength goal asks for the wider gap.
    separation_hours: float = 6.0
    # One line, in the athlete's language, saying what this week is for.
    focus: str = ""
    # Where the numbers come from. Printed with the advice, because a target
    # nobody can check is indistinguishable from one that was invented.
    source: str = ""


# ---------------------------------------------------------------------------
# what each goal asks of a week
# ---------------------------------------------------------------------------
# Races change shape as the date approaches, so they are a phase → target map.
# Standing goals do not, so they are a single target.
#
# Six sessions a week is the assumption throughout, because that is what Áron
# actually trains, football and hiking included. A plan built for ten would be
# arithmetically correct and useless.

_HYROX_SOURCE = ("HYROX coaching consensus: three runs a week — one easy Z2, one "
                 "interval, one compromised run — alongside two to three strength "
                 "or station sessions")

RACE_TARGETS: dict[str, dict[str, WeeklyTarget]] = {
    "hyrox": {
        PHASE_BASE: WeeklyTarget(6, base=3, quality=1, strength=2, long_minutes=75,
                                 focus="Aerobic base and strength, before the race-specific work",
                                 source=_HYROX_SOURCE),
        PHASE_BUILD: WeeklyTarget(6, base=2, quality=2, strength=2, long_minutes=70,
                                  focus="The race shape: a compromised run every week",
                                  source=_HYROX_SOURCE),
        PHASE_PEAK: WeeklyTarget(6, base=2, quality=2, strength=2, long_minutes=60,
                                 focus="Race pace and the stations under fatigue",
                                 source=_HYROX_SOURCE),
        PHASE_TAPER: WeeklyTarget(4, max_sessions=4, base=2, quality=1, strength=1, long_minutes=45,
                                  focus="Volume down, intensity kept — sharpness is not lost in two weeks, freshness is gained",
                                  source=_HYROX_SOURCE),
        PHASE_RACE: WeeklyTarget(3, max_sessions=3, base=1, quality=1, strength=1, long_minutes=0,
                                 focus="Short and easy. Nothing this week makes you fitter; several things can make you slower",
                                 source=_HYROX_SOURCE),
    },
    # 80/20: most of the week easy, one or two quality sessions, and the long run
    # is the session the distance is actually built on.
    "running": {
        PHASE_BASE: WeeklyTarget(6, base=4, quality=1, strength=1, long_minutes=90,
                                 focus="Easy volume, and the long run growing",
                                 source="80/20 polarised distribution; the long run as the distance-specific session"),
        PHASE_BUILD: WeeklyTarget(6, base=3, quality=2, strength=1, long_minutes=105,
                                  focus="Threshold and interval work on top of the base",
                                  source="80/20 polarised distribution"),
        PHASE_PEAK: WeeklyTarget(6, base=3, quality=2, strength=1, long_minutes=120,
                                 focus="Race-pace work inside the long run",
                                 source="80/20 polarised distribution"),
        PHASE_TAPER: WeeklyTarget(4, max_sessions=4, base=3, quality=1, strength=1, long_minutes=60,
                                  focus="Volume down about 40%, one short quality session kept",
                                  source="80/20 polarised distribution"),
        PHASE_RACE: WeeklyTarget(3, max_sessions=3, base=2, quality=1, strength=0, long_minutes=0,
                                 focus="Easy, short, and off your feet otherwise",
                                 source="80/20 polarised distribution"),
    },
    # 70.3 is the same framework as the full distance with shorter long sessions.
    # The published plans run six to nine sessions a week — one or two swims, two
    # or three rides, three runs — so six is the lean end of it, and the long ride
    # is the session the distance is actually built on.
    "half_ironman": {
        PHASE_BASE: WeeklyTarget(6, base=4, quality=1, strength=1, long_minutes=120,
                                 focus="Aerobic volume across all three disciplines",
                                 source="70.3 plans: 6-9 sessions a week (1-2 swims, 2-3 rides, "
                                        "3 runs), the long ride building through the block"),
        PHASE_BUILD: WeeklyTarget(6, base=4, quality=2, strength=1, long_minutes=150,
                                  focus="The long ride, and a run off the bike every week",
                                  source="70.3 plans: a structured bike session for sustainable "
                                         "power, and a short run off the bike for durability"),
        PHASE_PEAK: WeeklyTarget(6, base=4, quality=2, strength=1, long_minutes=180,
                                 focus="The long ride at 3 hours, race nutrition rehearsed on it",
                                 source="70.3 plans: long rides progressing to 3-4 h at "
                                        "controlled aerobic effort"),
        PHASE_TAPER: WeeklyTarget(4, max_sessions=4, base=3, quality=1, strength=1,
                                  long_minutes=75,
                                  focus="Volume down, the intensity kept short and sharp",
                                  source="70.3 plans: two weeks of coming down"),
        PHASE_RACE: WeeklyTarget(3, max_sessions=3, base=2, quality=1, strength=0,
                                 long_minutes=0,
                                 focus="Openers only — short, with a few race-pace minutes",
                                 source="70.3 plans: race week is openers"),
    },
    # Three disciplines in six sessions is already a compromise, and the advice
    # says so rather than pretending the week is sufficient.
    "ironman": {
        PHASE_BASE: WeeklyTarget(6, base=4, quality=1, strength=1, long_minutes=150,
                                 focus="Aerobic volume across all three disciplines",
                                 source="Long-course triathlon periodisation: aerobic volume first, one long session per week"),
        PHASE_BUILD: WeeklyTarget(7, base=4, quality=2, strength=1, long_minutes=210,
                                  focus="The long ride, and race nutrition rehearsed on it",
                                  source="Long-course triathlon periodisation"),
        PHASE_PEAK: WeeklyTarget(7, base=4, quality=2, strength=1, long_minutes=270,
                                 focus="The longest sessions of the whole build",
                                 source="Long-course triathlon periodisation"),
        PHASE_TAPER: WeeklyTarget(5, max_sessions=5, base=3, quality=1, strength=1, long_minutes=90,
                                  focus="Three weeks of coming down, not one",
                                  source="Long-course triathlon periodisation"),
        PHASE_RACE: WeeklyTarget(3, max_sessions=3, base=2, quality=1, strength=0, long_minutes=0,
                                 focus="Openers only",
                                 source="Long-course triathlon periodisation"),
    },
    "other": {
        PHASE_BASE: WeeklyTarget(6, base=3, quality=1, strength=2, long_minutes=60,
                                 focus="Aerobic base and strength",
                                 source="General preparation, no discipline given"),
        PHASE_BUILD: WeeklyTarget(6, base=2, quality=2, strength=2, long_minutes=60,
                                  focus="More specific work, same volume",
                                  source="General preparation, no discipline given"),
        PHASE_PEAK: WeeklyTarget(6, base=2, quality=2, strength=2, long_minutes=60,
                                 focus="Sharpening",
                                 source="General preparation, no discipline given"),
        PHASE_TAPER: WeeklyTarget(4, max_sessions=4, base=2, quality=1, strength=1, long_minutes=45,
                                  focus="Volume down, intensity kept",
                                  source="General preparation, no discipline given"),
        PHASE_RACE: WeeklyTarget(3, max_sessions=3, base=1, quality=1, strength=1, long_minutes=0,
                                 focus="Short and easy",
                                 source="General preparation, no discipline given"),
    },
}

STANDING_TARGETS: dict[str, WeeklyTarget] = {
    "maintenance": WeeklyTarget(
        6, base=2, quality=1, strength=2, long_minutes=60,
        focus="Hold what is there: one long easy session, one hard one, two in the gym",
        source="Minimum effective dose: fitness is maintained on markedly less work "
               "than it was built with, provided the intensity is kept"),
    # Hard aerobic work is capped rather than removed: one session a week keeps
    # the aerobic side alive at a cost the strength side can absorb.
    "strength": WeeklyTarget(
        6, base=2, quality=1, strength=3, long_minutes=60, max_hard=1,
        separation_hours=8.0,
        focus="Three strength sessions, and the hard aerobic work kept to one and "
              "held well away from them",
        source="Wilson et al. meta-analysis: concurrent endurance work costs 5-10% of "
               "maximal strength gain, and the cost falls with the gap — minimal past 8 h"),
    # Four sessions, because frequency is what gets each muscle group trained
    # twice a week, and twice beats once at matched volume.
    "muscle": WeeklyTarget(
        6, base=2, quality=1, strength=4, long_minutes=45, max_hard=1,
        separation_hours=8.0,
        focus="Four strength sessions so every muscle group is trained twice, 10-20 "
              "hard sets each across the week",
        source="Schoenfeld et al.: a dose-response to weekly sets with 10+ sets per "
               "muscle group, diminishing past ~20; twice-weekly frequency beats "
               "once-weekly at matched volume"),
    # The strength volume is the point, not a side note: it is what the research
    # ties to keeping lean mass while the weight comes down.
    "fat_loss": WeeklyTarget(
        6, base=3, quality=1, strength=3, long_minutes=60,
        focus="Keep the strength volume and the easy volume — the weight comes off "
              "either way, what is kept is decided by the training",
        source="Murphy & Koehler, and Roth et al.: resistance training at 10+ weekly "
               "sets per muscle group in a deficit shows little to no lean-mass loss; "
               "~0.5-1% of body weight per week is the band associated with retaining it"),
}

# Without any goal at all. Not nothing — the same week still deserves a reading,
# and this is the shape of a balanced week for someone training six times.
DEFAULT_TARGET = WeeklyTarget(
    6, base=2, quality=1, strength=2, long_minutes=60,
    focus="No goal set — judged against a balanced week",
    source="80/20 distribution with resistance work twice a week")


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    goal_id: int | None = None
    kind: str = "standing"
    name: str = ""
    discipline: str | None = None
    standing: str | None = None
    race_date: str | None = None
    target: str | None = None
    priority: int = 2
    active: bool = True
    created: str = ""
    notes: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "race":
            return DISCIPLINE_LABELS.get(self.discipline or "other", "Race")
        return STANDING_LABELS.get(self.standing or "", "Goal")


def _row_to_goal(row) -> Goal:
    return Goal(
        goal_id=row["goal_id"], kind=row["kind"], name=row["name"],
        discipline=row["discipline"], standing=row["standing"],
        race_date=row["race_date"], target=row["target"],
        priority=row["priority"], active=bool(row["active"]),
        created=row["created"], notes=row["notes"])


def add_goal(conn: sqlite3.Connection, goal: Goal) -> int:
    """Store one goal. Validated here, because a goal the targets cannot read is
    worse than no goal: it would silently fall back to the default."""
    if goal.kind not in ("race", "standing"):
        raise ValueError(f"kind must be 'race' or 'standing', not {goal.kind!r}")
    if not (goal.name or "").strip():
        raise ValueError("a goal needs a name")
    if goal.kind == "race":
        if goal.discipline not in RACE_DISCIPLINES:
            raise ValueError(f"discipline must be one of {RACE_DISCIPLINES}")
        if not goal.race_date:
            raise ValueError("a race needs a date")
        _dt.date.fromisoformat(goal.race_date)      # raises on a bad date
    else:
        if goal.standing not in STANDING_KINDS:
            raise ValueError(f"standing must be one of {STANDING_KINDS}")
    created = goal.created or _dt.date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO training_goal (kind, name, discipline, standing, race_date, "
        "target, priority, active, created, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (goal.kind, goal.name.strip(), goal.discipline, goal.standing,
         goal.race_date, goal.target, goal.priority, 1 if goal.active else 0,
         created, goal.notes))
    conn.commit()
    return int(cur.lastrowid)


def retire_goal(conn: sqlite3.Connection, goal_id: int) -> bool:
    """Deactivate rather than delete: a race that has been run is history worth
    keeping, and the week it was run in still has to be explicable later."""
    cur = conn.execute("UPDATE training_goal SET active = 0 WHERE goal_id = ?",
                       (goal_id,))
    conn.commit()
    return cur.rowcount > 0


def update_goal(conn: sqlite3.Connection, goal_id: int, **fields) -> bool:
    """Change a stored goal in place.

    Retiring and re-adding would work, but it loses the id and the created date
    for what is usually a correction — a target time settled after the race was
    entered, or the format of it written down.
    """
    allowed = ("name", "target", "notes", "priority", "race_date", "discipline",
               "standing")
    bad = sorted(set(fields) - set(allowed))
    if bad:
        raise ValueError(f"cannot set {bad}; only {allowed}")
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return False
    if "race_date" in fields:
        _dt.date.fromisoformat(fields["race_date"])
    if "discipline" in fields and fields["discipline"] not in RACE_DISCIPLINES:
        raise ValueError(f"discipline must be one of {RACE_DISCIPLINES}")
    if "standing" in fields and fields["standing"] not in STANDING_KINDS:
        raise ValueError(f"standing must be one of {STANDING_KINDS}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE training_goal SET {sets} WHERE goal_id = ?",
                       (*fields.values(), goal_id))
    conn.commit()
    return cur.rowcount > 0


def all_goals(conn: sqlite3.Connection, *, include_retired: bool = False) -> list[Goal]:
    conn.row_factory = sqlite3.Row
    sql = ("SELECT * FROM training_goal"
           + ("" if include_retired else " WHERE active = 1")
           + " ORDER BY priority, COALESCE(race_date, '9999-12-31'), goal_id")
    return [_row_to_goal(r) for r in conn.execute(sql)]


def active_goals(conn: sqlite3.Connection, date: str) -> list[Goal]:
    """Goals that still apply on `date`. A race in the past no longer does, even
    if nobody has got round to retiring it — the day after a race is not still
    race week."""
    out = []
    for g in all_goals(conn):
        if g.kind == "race" and g.race_date and g.race_date < date:
            continue
        out.append(g)
    return out


# ---------------------------------------------------------------------------
# which goal drives this week
# ---------------------------------------------------------------------------

def weeks_until(race_date: str, date: str) -> int:
    """Whole weeks from `date` to the race, rounded UP.

    Rounded up on purpose: with nine days to go you are in the last-but-one
    week, and calling that "one week" would taper a week early.
    """
    d0 = _dt.date.fromisoformat(date)
    d1 = _dt.date.fromisoformat(race_date)
    days = (d1 - d0).days
    if days <= 0:
        return 0
    return -(-days // 7)


def phase_for(race_date: str, date: str) -> str:
    """Which block of the plan this week falls in, from the date alone.

    The ladder is checked from the race backwards, so each boundary means "and
    nothing nearer" — getting the order wrong is how a peak week ends up labelled
    as a taper, which is exactly the mistake that costs a race.
    """
    w = weeks_until(race_date, date)
    if w < RACE_WEEK_UNDER:
        return PHASE_RACE
    if w < TAPER_UNDER:
        return PHASE_TAPER
    if w < PEAK_UNDER:
        return PHASE_PEAK
    if w < BUILD_UNDER:
        return PHASE_BUILD
    return PHASE_BASE


def driving_goal(conn: sqlite3.Connection, date: str) -> Goal | None:
    """The one goal this week is shaped by.

    The nearest race wins over any standing goal, because a date does not move.
    Between two races the earlier one wins, and at equal dates the higher
    priority. With no race, the first standing goal by priority.
    """
    goals = active_goals(conn, date)
    races = [g for g in goals if g.kind == "race" and g.race_date]
    if races:
        races.sort(key=lambda g: (g.race_date, g.priority, g.goal_id or 0))
        return races[0]
    standing = [g for g in goals if g.kind == "standing"]
    if standing:
        standing.sort(key=lambda g: (g.priority, g.goal_id or 0))
        return standing[0]
    return None


def target_for(goal: Goal | None, date: str) -> tuple[WeeklyTarget, str | None]:
    """The week's target, and the phase it came from (None for a standing goal)."""
    if goal is None:
        return DEFAULT_TARGET, None
    if goal.kind == "race" and goal.race_date:
        phase = phase_for(goal.race_date, date)
        table = RACE_TARGETS.get(goal.discipline or "other", RACE_TARGETS["other"])
        # Applied here rather than at each call site, so the page, the email and
        # the plan all see the same adjusted target without having to remember to
        # ask for it.
        adjusted, _notes = apply_notes(table[phase], goal)
        return adjusted, phase
    return STANDING_TARGETS.get(goal.standing or "", DEFAULT_TARGET), None


# ---------------------------------------------------------------------------
# the week against the target
# ---------------------------------------------------------------------------

@dataclass
class Gap:
    """One shortfall or overshoot, named so it can be printed in any order."""

    what: str
    have: float
    want: float
    over: bool = False        # True when the target is a ceiling, not a floor
    unit: str = ""            # minutes, where the gap is not a count of sessions


def gaps(summary: dict, target: WeeklyTarget) -> list[Gap]:
    """Where the week stands against the target. Shortfalls first, then ceilings
    that have been exceeded. An empty list means the week has met its goal."""
    c = summary["counts"]
    out: list[Gap] = []
    if c["aerobic_base"] < target.base:
        out.append(Gap("aerobic base sessions", c["aerobic_base"], target.base))
    if c["aerobic_quality"] < target.quality:
        out.append(Gap("quality sessions", c["aerobic_quality"], target.quality))
    if c["strength"] < target.strength:
        out.append(Gap("strength sessions", c["strength"], target.strength))
    if target.long_minutes and summary["longest_base_minutes"] < target.long_minutes:
        out.append(Gap("the longest aerobic session",
                       summary["longest_base_minutes"], target.long_minutes,
                       unit="min"))
    if summary["sessions"] < target.sessions:
        out.append(Gap("sessions in total", summary["sessions"], target.sessions))
    if target.max_hard is not None and c["aerobic_quality"] > target.max_hard:
        out.append(Gap("hard aerobic sessions", c["aerobic_quality"],
                       target.max_hard, over=True))
    if target.max_sessions is not None and summary["sessions"] > target.max_sessions:
        out.append(Gap("sessions this week", summary["sessions"],
                       target.max_sessions, over=True))
    return out


def met(summary: dict, target: WeeklyTarget) -> bool:
    return not gaps(summary, target)


# ---------------------------------------------------------------------------
# what the notes say
# ---------------------------------------------------------------------------
# A goal's `notes` is free text, and the format of a race lives there: open or
# pro category, doubles or singles, a relay leg. Those genuinely change what a
# week should contain, so the advice reads them.
#
# It reads them by RECOGNISING a small documented vocabulary, not by
# interpreting the sentence. Two reasons. The morning email runs on a server with
# no model in it, so anything the page could interpret the email could not — and
# the two describing the same goal differently is the thing this module exists to
# prevent. And a rule that can be stated can be checked; "the computer read your
# note and decided" cannot.
#
# What it does NOT recognise, it says so, rather than leaving a note silently
# ignored. That matters more than the recognising: a note that looks acted upon
# and is not is worse than one plainly skipped.

# marker -> (which disciplines it applies to, what it changes, why)
NOTE_MARKERS = {
    "doubles": {
        "disciplines": ("hyrox",),
        "group": "format",
        "label": "doubles",
        "effect": {"base": +1},
        "why": "In doubles the station reps are shared but both athletes run the "
               "whole 8 km, so the running is a larger share of your race than in "
               "singles — one more aerobic session, one less station-dominated one.",
    },
    "singles": {
        "disciplines": ("hyrox",),
        "group": "format",
        "label": "singles",
        "effect": {},
        "why": "Singles: the standard split of running and stations, which the "
               "targets already assume.",
    },
    "relay": {
        "disciplines": ("hyrox",),
        "group": "format",
        "label": "relay",
        "effect": {"base": -1},
        "why": "A relay leg is a fraction of the race, so the aerobic demand is "
               "lower than a full one.",
    },
    "pro": {
        "disciplines": ("hyrox",),
        "group": "category",
        "label": "pro category",
        "effect": {"strength": +1},
        "why": "The pro category carries heavier implements throughout, so the "
               "strength side needs more of the week than open does.",
    },
    "elite": {
        "disciplines": ("hyrox",),
        "group": "category",
        "label": "pro category",
        "effect": {"strength": +1},
        "why": "The elite/pro category carries heavier implements throughout, so "
               "the strength side needs more of the week than open does.",
    },
    "open": {
        "disciplines": ("hyrox",),
        "group": "category",
        "label": "open category",
        "effect": {},
        "why": "Open category: the standard weights, which the targets assume.",
    },
}


def read_notes(goal: Goal | None) -> dict:
    """Which markers the notes carry, and what they change.

    Matched on whole words, case-insensitively, so "Doubles, Levivel" is read and
    a word that merely contains a marker is not.
    """
    out = {"found": [], "effect": {}, "why": [], "conflicts": [],
           "text": (goal.notes if goal else None)}
    if not goal or not goal.notes:
        return out
    text = goal.notes.lower()
    discipline = goal.discipline or ""
    by_group: dict[str, list[dict]] = {}
    for marker, spec in NOTE_MARKERS.items():
        if discipline not in spec["disciplines"]:
            continue
        if not _re.search(r"(?<![\w])" + _re.escape(marker) + r"(?![\w])", text):
            continue
        hits = by_group.setdefault(spec["group"], [])
        if any(h["label"] == spec["label"] for h in hits):
            continue                      # pro and elite mean the same thing
        hits.append(spec)

    for group, hits in sorted(by_group.items()):
        if len(hits) > 1:
            # "Pro kategória, open nem" names both. Adding their effects would be
            # nonsense and picking one would be a guess, so neither is applied and
            # the contradiction is reported — it is the note that needs fixing.
            out["conflicts"].append(
                group + ": the note names " +
                " and ".join(sorted(h["label"] for h in hits)) +
                ", so neither is applied")
            continue
        spec = hits[0]
        out["found"].append(spec["label"])
        for field, delta in spec["effect"].items():
            out["effect"][field] = out["effect"].get(field, 0) + delta
        out["why"].append(spec["why"])
    return out


def apply_notes(target: WeeklyTarget, goal: Goal | None) -> tuple[WeeklyTarget, dict]:
    """The target as the notes modify it, and what was read to get there.

    A floor is never pushed below zero, and the session total is left alone: a
    note changes the MIX of a week, not how much of it there is.
    """
    notes = read_notes(goal)
    if not notes["effect"]:
        return target, notes
    fields = {"base": target.base, "quality": target.quality,
              "strength": target.strength}
    for field, delta in notes["effect"].items():
        if field in fields:
            fields[field] = max(0, fields[field] + delta)
    return (WeeklyTarget(
        sessions=target.sessions, base=fields["base"], quality=fields["quality"],
        strength=fields["strength"], long_minutes=target.long_minutes,
        max_hard=target.max_hard, max_sessions=target.max_sessions,
        separation_hours=target.separation_hours, focus=target.focus,
        source=target.source), notes)


def unread_note(goal: Goal | None) -> str:
    """Said out loud when a note carries nothing the rules know.

    A note that looks acted upon and is not is worse than one plainly skipped.
    """
    notes = read_notes(goal)
    if notes["conflicts"]:
        return "The note contradicts itself — " + "; ".join(notes["conflicts"]) + "."
    if not goal or not goal.notes or notes["found"]:
        return ""
    known = ", ".join(sorted({v["label"] for v in NOTE_MARKERS.values()
                              if (goal.discipline or "") in v["disciplines"]}))
    if not known:
        return (f"The note on this goal is kept as it is written, and nothing in it "
                f"changes the targets — no marker is defined for a "
                f"{DISCIPLINE_LABELS.get(goal.discipline or 'other', 'race').lower()}.")
    return (f"Nothing in the note changes the targets. For this race the rules "
            f"recognise: {known}.")


# ---------------------------------------------------------------------------
# what the week still owes, and what of it fits today
# ---------------------------------------------------------------------------
# The daily brief already decides how HARD today may be, from readiness, recovery
# and sleep. That ceiling is not negotiable and nothing here raises it: a week
# short of a long run is not a reason to train on a day the body says no.
#
# What was missing is the other half — WHAT to spend the day on. Picking it from
# the week's remainder is the difference between "moderate day" and "moderate
# day, and the long easy session is the one thing this week still owes".

# The readiness ladder, lowest first. A copy of brief.LEVELS rather than an
# import, because brief imports this module; a test pins the two together.
LEVELS = ("rest", "recovery", "easy", "moderate", "hard")

# The lowest ceiling each kind of session can be done under. Quality needs a hard
# day and nothing less — that is what makes a hard day worth spending on it.
# Strength and easy aerobic work sit at "easy": both are possible on a moderate
# day too, but neither needs one.
QUALITY_MIN_LEVEL = {
    "aerobic_quality": "hard",
    "aerobic_base": "easy",
    "long": "easy",
    "strength": "easy",
}

# Scarcity order, most constrained first. Quality outranks the long session on a
# day that allows both: a long run only needs a day with time in it, while a
# quality session needs a day the body will take — and those are rarer.
FOCUS_ORDER = ("aerobic_quality", "long", "aerobic_base", "strength")

FOCUS_LABELS = {
    "aerobic_quality": "a quality session",
    "long": "the long easy session",
    "aerobic_base": "an easy aerobic session",
    "strength": "a strength session",
}


def days_left(date: str) -> int:
    """Days remaining in the week, today included. Monday is day one of seven."""
    return 7 - _dt.date.fromisoformat(date).weekday()


def week_remainder(summary: dict, target: WeeklyTarget, date: str) -> dict:
    """What the week still owes, and whether there is time left to pay it."""
    c = summary["counts"]
    owed: dict[str, int] = {}
    if c["aerobic_base"] < target.base:
        owed["aerobic_base"] = target.base - c["aerobic_base"]
    if c["aerobic_quality"] < target.quality:
        owed["aerobic_quality"] = target.quality - c["aerobic_quality"]
    if c["strength"] < target.strength:
        owed["strength"] = target.strength - c["strength"]
    # The long session is a property of one of the base sessions, not a session of
    # its own — counting it as extra work would overstate what the week owes.
    long_missing = bool(target.long_minutes
                        and summary["longest_base_minutes"] < target.long_minutes)
    left = days_left(date)
    sessions_owed = sum(owed.values())
    # A taper that is already at its ceiling owes nothing, whatever is "missing":
    # the point of the week is less work, so asking for more would contradict it.
    capped = (target.max_sessions is not None
              and summary["sessions"] >= target.max_sessions)
    return {
        "owed": {} if capped else owed,
        "long_missing": False if capped else long_missing,
        "sessions_owed": 0 if capped else sessions_owed,
        "days_left": left,
        "capped": capped,
        # Honest rather than encouraging: with more owed than days left, saying so
        # is more use than a plan that cannot be followed.
        "feasible": capped or sessions_owed <= left,
    }


def todays_focus(level: str, summary: dict, target: WeeklyTarget, date: str) -> dict:
    """Which of the week's remaining work fits today's ceiling.

    `level` is the brief's own decision about how hard today may be. This never
    raises it — it only chooses among what that ceiling already allows.
    """
    rem = week_remainder(summary, target, date)
    out = {"focus": None, "line": "", "remainder": rem}

    if level not in LEVELS:
        return out
    ceiling = LEVELS.index(level)

    if rem["capped"]:
        out["line"] = ("This week has had its sessions — it is a taper, and coming "
                       "down is the point. Nothing is owed.")
        return out

    if not rem["owed"] and not rem["long_missing"]:
        out["line"] = "The week has met its goal already; anything today is a bonus."
        return out

    if ceiling <= LEVELS.index("recovery"):
        out["line"] = ("The week still owes work, but not today — today's ceiling "
                       "is set by how you recovered, and that comes first.")
        return out

    for kind in FOCUS_ORDER:
        wanted = rem["long_missing"] if kind == "long" else rem["owed"].get(kind)
        if not wanted:
            continue
        if LEVELS.index(QUALITY_MIN_LEVEL[kind]) > ceiling:
            continue
        out["focus"] = kind
        if kind == "long":
            out["line"] = (f"Of what the week still owes, {FOCUS_LABELS[kind]} is the "
                           f"one that fits today — {target.long_minutes} min or more, "
                           "and the longest so far is "
                           f"{summary['longest_base_minutes']:.0f}.")
        else:
            out["line"] = (f"Of what the week still owes, {FOCUS_LABELS[kind]} is the "
                           f"one that fits today ({wanted} short).")
        break

    if out["focus"] is None:
        # Everything outstanding needs a harder day than this one allows. Saying
        # which, and that it has to wait, beats saying nothing.
        waiting = [FOCUS_LABELS[k] for k in FOCUS_ORDER
                   if (rem["long_missing"] if k == "long" else rem["owed"].get(k))]
        out["line"] = ("What the week still owes — " + ", ".join(waiting) +
                       " — needs a harder day than today allows.")

    if not rem["feasible"]:
        out["line"] += (f" {rem['sessions_owed']} sessions outstanding with "
                        f"{rem['days_left']} days left: the week will fall short, "
                        "and choosing what matters most beats chasing all of it.")
    return out


def describe_goal(goal: Goal | None, date: str) -> str:
    """One line naming the goal and where in it this week sits."""
    if goal is None:
        return "No goal set — the week is read against a balanced one."
    if goal.kind == "race" and goal.race_date:
        w = weeks_until(goal.race_date, date)
        phase = PHASE_LABELS[phase_for(goal.race_date, date)]
        when = "this week" if w == 0 else (f"in {w} week" if w == 1 else f"in {w} weeks")
        tgt = f", target {goal.target}" if goal.target else ""
        return (f"{goal.label}: {goal.name}, {goal.race_date} — {when}. "
                f"{phase} phase{tgt}.")
    return f"{goal.label}: {goal.name}."


def observations(summary: dict, goal: Goal | None, date: str) -> list[str]:
    """Plain statements about the week against its goal, strongest first.

    Advisory by construction: it names what is missing and what it is judged
    against, and never says which day to do it on.
    """
    target, _phase = target_for(goal, date)
    out: list[str] = [describe_goal(goal, date)]
    if target.focus:
        out.append(f"This week is for: {target.focus}.")

    # What the note changed, and why. Printed BEFORE the gaps, because it changed
    # the numbers those gaps are measured against — reading "1 of 3" without
    # knowing the 3 came from "doubles" is reading a number out of nowhere.
    notes = read_notes(goal)
    if notes["found"]:
        out.append("From your note (" + ", ".join(notes["found"]) + "): " +
                   " ".join(notes["why"]))
    skipped = unread_note(goal)
    if skipped:
        out.append(skipped)

    shortfalls = gaps(summary, target)
    if not shortfalls:
        out.append(f"The week has met its goal: {summary['sessions']} sessions, "
                   f"{summary['total_minutes']} min.")
    for g in shortfalls:
        if g.over and g.what == "sessions this week":
            out.append(f"{g.what}: {g.have:.0f}, and this week asks for at most "
                       f"{g.want:.0f}. Coming down is the point of it — nothing "
                       "added now arrives in time to help.")
        elif g.over:
            out.append(f"{g.what}: {g.have:.0f}, and this goal asks for at most "
                       f"{g.want:.0f} — past that the interference costs more "
                       f"strength than the aerobic work is worth here.")
        else:
            unit = f" {g.unit}" if g.unit else ""
            out.append(f"{g.what}: {g.have:.0f} of {g.want:.0f}{unit}.")

    # What the shape of the week says, which no target can express as a number.
    # Imported from week.py rather than restated, so the page, the email and this
    # cannot word the same finding three ways.
    from .week import insights
    out.extend(insights(summary))

    # Interference is reported against the gap THIS goal cares about, which is
    # wider for the strength-side goals than the general six hours.
    for day in summary["interference"]:
        if day["gap_hours"] < target.separation_hours:
            out.append(
                f"{day['d']}: {' + '.join(day['sports'])} {day['gap_hours']} h apart. "
                f"This goal asks for {target.separation_hours:.0f} h between "
                "resistance and hard endurance work.")

    if target.source:
        out.append(f"Judged against: {target.source}.")
    return out


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
# Goals are set from Áron's own terminal, never from the page: the dashboard
# reads this table through garmin_query, which is a read-only connection, and
# that is what makes publishing it acceptable. Giving the page a write tool to
# save typing would be a poor trade.

def _fmt_goal(g: Goal, date: str) -> str:
    head = f"  [{g.goal_id}] {g.label}: {g.name}"
    if g.kind == "race" and g.race_date:
        w = weeks_until(g.race_date, date)
        head += (f"  {g.race_date} ({w} week{'' if w == 1 else 's'}, "
                 f"{PHASE_LABELS[phase_for(g.race_date, date)].lower()})")
    if g.target:
        head += f"  target {g.target}"
    if not g.active:
        head += "  [retired]"
    return head


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .db import DB_PATH, get_connection

    ap = argparse.ArgumentParser(
        prog="python -m garmin_mcp.goals",
        description="Set what the training is for. The dashboard and the morning "
                    "brief both read these.")
    ap.add_argument("--db", default=DB_PATH, help=f"database (default: {DB_PATH})")
    # NOT --date: the `race` subcommand takes a positional `date`, and argparse
    # writes both to the same attribute. The race's own date then became "today",
    # so adding a race twelve weeks out reported it as race week.
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="pretend today is this ISO date")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show the goals, and this week's target")
    p.add_argument("--all", action="store_true", help="include retired goals")

    p = sub.add_parser("race", help="add a race")
    p.add_argument("name")
    p.add_argument("date", help="ISO date of the race")
    p.add_argument("--discipline", choices=RACE_DISCIPLINES, default="other")
    p.add_argument("--target", default=None, help="e.g. 65:00, sub-3:30")
    p.add_argument("--priority", type=int, default=2, help="1 = A race (default 2)")
    p.add_argument("--notes", default=None)

    p = sub.add_parser("goal", help="add a standing goal")
    p.add_argument("standing", choices=STANDING_KINDS)
    p.add_argument("--name", default=None, help="defaults to the kind")
    p.add_argument("--target", default=None, help="e.g. 72 kg")
    p.add_argument("--priority", type=int, default=2)
    p.add_argument("--notes", default=None)

    p = sub.add_parser("set", help="change a stored goal in place")
    p.add_argument("goal_id", type=int)
    p.add_argument("--name", default=None)
    p.add_argument("--target", default=None, help="e.g. 65:00, 72 kg")
    p.add_argument("--notes", default=None)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--race-date", dest="race_date", default=None)

    p = sub.add_parser("retire", help="deactivate a goal, keeping its history")
    p.add_argument("goal_id", type=int)

    a = ap.parse_args(argv)
    today = a.as_of or _dt.date.today().isoformat()
    conn = get_connection(a.db)

    if a.cmd == "list":
        goals = all_goals(conn, include_retired=a.all)
        if not goals:
            print("No goals set. The week is read against a balanced one.")
        else:
            print("Goals:")
            for g in goals:
                print(_fmt_goal(g, today))
        drive = driving_goal(conn, today)
        target, phase = target_for(drive, today)
        print(f"\nThis week ({today}) is judged against:")
        print(f"  {describe_goal(drive, today)}")
        print(f"  {target.sessions} sessions — {target.base} aerobic base, "
              f"{target.quality} quality, {target.strength} strength"
              + (f", longest {target.long_minutes} min" if target.long_minutes else "")
              + (f", at most {target.max_hard} hard aerobic" if target.max_hard is not None else ""))
        print(f"  {target.separation_hours:.0f} h between resistance and hard endurance work")
        print(f"  Source: {target.source}")
        return 0

    if a.cmd == "race":
        gid = add_goal(conn, Goal(kind="race", name=a.name, discipline=a.discipline,
                                  race_date=a.date, target=a.target,
                                  priority=a.priority, notes=a.notes))
        print(f"Added race [{gid}]: {a.name}, {a.date} "
              f"({PHASE_LABELS[phase_for(a.date, today)].lower()} phase this week)")
        return 0

    if a.cmd == "goal":
        gid = add_goal(conn, Goal(kind="standing", name=a.name or STANDING_LABELS[a.standing],
                                  standing=a.standing, target=a.target,
                                  priority=a.priority, notes=a.notes))
        print(f"Added goal [{gid}]: {STANDING_LABELS[a.standing]}")
        return 0

    if a.cmd == "set":
        changed = update_goal(conn, a.goal_id, name=a.name, target=a.target,
                              notes=a.notes, priority=a.priority,
                              race_date=a.race_date)
        if not changed:
            print(f"Nothing changed — no goal {a.goal_id}, or nothing to set.")
            return 0
        for g in all_goals(conn, include_retired=True):
            if g.goal_id == a.goal_id:
                print("Updated:" + _fmt_goal(g, today))
                if g.notes:
                    print(f"    notes: {g.notes}")
        return 0

    if a.cmd == "retire":
        print(f"Retired goal {a.goal_id}." if retire_goal(conn, a.goal_id)
              else f"No goal with id {a.goal_id}.")
        return 0

    return 1


if __name__ == "__main__":       # pragma: no cover - a thin argparse wrapper
    raise SystemExit(main())

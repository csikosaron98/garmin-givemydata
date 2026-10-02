"""The training week, classified by quality rather than by sport.

Any movement counts — football, hiking, skiing, a swim — so the week cannot be
described in sports. It is described in *training qualities*, which is what the
body actually responds to, and every activity is sorted into one from its own
measurements rather than from its name (33 of the last 33 strength sessions are
called "Gym", so the name carries nothing).

The classification lives in SQL, as one expression, because two consumers need
exactly the same answer: this module, and the dashboard page, which reads it
through the read-only `garmin_query` tool. A test pins the two copies together.

What this deliberately does NOT do is prescribe a schedule. The advice is a
balance sheet — what the week contains and what it is missing — because which
day a session lands on is usually decided on the day.

Nothing here is medical advice.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------
# Minutes above the zone-4 floor that make a session "quality" rather than
# steady work. Eight is deliberately low: a session with eight hard minutes has
# a different recovery cost from one with none, whatever its average says.
QUALITY_HARD_MINUTES = 8

# Below this a session is too short to be anyone's aerobic base. Easy work that
# falls short of it is restorative, not "mixed" — a twenty-minute spin is not an
# unclassifiable session, it is a light one.
BASE_MIN_MINUTES = 20

# Share of measured zone time that must sit in Z1-Z2 for "base".
BASE_EASY_SHARE = 0.7

# A "long" one is tracked separately from the quality: the usual weekly gap is
# not "no easy running" but "nothing longer than fifty minutes".
LONG_MINUTES = 60

# Below this a session is restorative whatever else it looks like.
RESTORATIVE_MAX_LOAD = 10

# Intermittent by nature, whatever the average heart rate says: a kickabout at
# 96 bpm average is still stop-start, and treating it as steady aerobic work
# would credit a week that never did any.
INTERMITTENT_SPORTS = ("soccer", "volleyball", "basketball", "tennis", "squash",
                       "racquetball", "badminton", "hockey", "american_football")

RESTORATIVE_SPORTS = ("yoga", "pilates", "breathwork", "meditation")

STRENGTH_SPORTS = ("strength_training", "indoor_climbing", "bouldering")

QUALITIES = ("strength", "aerobic_quality", "aerobic_base", "restorative", "mixed")

# Concurrent-training research: under ~6 h between a strength session and hard
# endurance work there is measurable interference; over 8 h it is minimal, and
# at 24 h it is absent. See docs — this is reported, never used to forbid.
INTERFERENCE_HOURS = 6.0


def _sql_list(values) -> str:
    return ", ".join("'" + v + "'" for v in values)


# One expression, two consumers. The dashboard carries a copy of this string and
# a test asserts the two are identical — if they drift, the page and the email
# would describe the same week differently, which is worse than either being
# wrong on its own.
CLASSIFY_SQL = f"""SELECT substr(a.start_time_local,1,10) AS d,
       substr(a.start_time_local,12,5) AS t,
       a.activity_type AS sport,
       ROUND(a.duration_seconds/60.0,0) AS minutes,
       ROUND(a.distance_meters/1000.0,2) AS km,
       ROUND(a.training_load,0) AS load,
       ROUND(COALESCE(z.zone4_seconds+z.zone5_seconds,0)/60.0,1) AS hard_min,
       CASE
         WHEN a.activity_type IN ({_sql_list(STRENGTH_SPORTS)}) THEN 'strength'
         WHEN a.activity_type IN ({_sql_list(RESTORATIVE_SPORTS)}) THEN 'restorative'
         WHEN a.activity_type IN ({_sql_list(INTERMITTENT_SPORTS)}) THEN 'mixed'
         WHEN COALESCE(z.zone4_seconds+z.zone5_seconds,0)/60.0 >= {QUALITY_HARD_MINUTES}
              THEN 'aerobic_quality'
         WHEN a.duration_seconds/60.0 >= {BASE_MIN_MINUTES}
              AND COALESCE(z.zone1_seconds+z.zone2_seconds,0) >= {BASE_EASY_SHARE} *
                  COALESCE(NULLIF(z.zone1_seconds+z.zone2_seconds+z.zone3_seconds
                                  +z.zone4_seconds+z.zone5_seconds, 0), 1)
              THEN 'aerobic_base'
         WHEN COALESCE(z.zone1_seconds+z.zone2_seconds,0) >= {BASE_EASY_SHARE} *
              COALESCE(NULLIF(z.zone1_seconds+z.zone2_seconds+z.zone3_seconds
                              +z.zone4_seconds+z.zone5_seconds, 0), 1)
              THEN 'restorative'
         WHEN COALESCE(a.training_load,0) < {RESTORATIVE_MAX_LOAD} THEN 'restorative'
         ELSE 'mixed'
       END AS quality
  FROM activity a
  LEFT JOIN activity_hr_zones z ON z.activity_id = a.activity_id
 WHERE substr(a.start_time_local,1,10) >= ?
   AND substr(a.start_time_local,1,10) <= ?
 ORDER BY a.start_time_local"""


def week_start(date: str) -> str:
    """The Monday of the week containing `date`."""
    d = _dt.date.fromisoformat(date)
    return (d - _dt.timedelta(days=d.weekday())).isoformat()


def sessions(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(CLASSIFY_SQL, (start, end))]


def interference_days(rows: list[dict]) -> list[dict]:
    """Days where a strength session sits within INTERFERENCE_HOURS of other work.

    Reported, never forbidden: the trade-off is the athlete's to make, and the
    effect is a few per cent of maximum strength, not an injury.
    """
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["d"], []).append(r)
    out = []
    for day, items in sorted(by_day.items()):
        if len(items) < 2 or not any(i["quality"] == "strength" for i in items):
            continue
        times = []
        for i in items:
            try:
                h, m = i["t"].split(":")
                times.append(int(h) + int(m) / 60.0)
            except (ValueError, AttributeError):
                continue
        if len(times) < 2:
            continue
        gap = max(times) - min(times)
        if gap < INTERFERENCE_HOURS:
            out.append({"d": day, "gap_hours": round(gap, 1),
                        "sports": [i["sport"] for i in items]})
    return out


def tally(rows: list[dict]) -> dict:
    """What the week contains. No targets here — those depend on the goal."""
    counts = {q: 0 for q in QUALITIES}
    minutes = {q: 0.0 for q in QUALITIES}
    for r in rows:
        q = r["quality"]
        if q in counts:
            counts[q] += 1
            minutes[q] += r["minutes"] or 0
    longest_base = max(
        [r["minutes"] or 0 for r in rows if r["quality"] == "aerobic_base"] or [0])
    longest_endurance = max(
        [r["minutes"] or 0 for r in rows
         if r["quality"] in ("aerobic_base", "aerobic_quality")] or [0])
    return {
        "sessions": len(rows),
        "days": len({r["d"] for r in rows}),
        "total_minutes": round(sum(r["minutes"] or 0 for r in rows)),
        "counts": counts,
        "minutes": {q: round(m) for q, m in minutes.items()},
        "longest_base_minutes": round(longest_base),
        "has_long_base": longest_base >= LONG_MINUTES,
        "longest_endurance_minutes": round(longest_endurance),
        "interference": interference_days(rows),
    }


def summarize(conn: sqlite3.Connection, date: str) -> dict:
    """The week containing `date`, up to and including it."""
    start = week_start(date)
    rows = sessions(conn, start, date)
    out = tally(rows)
    out["week_start"] = start
    out["through"] = date
    out["rows"] = rows
    return out


# ---------------------------------------------------------------------------
# advice
# ---------------------------------------------------------------------------
# Advisory, not prescriptive: it names what is missing and leaves the placing to
# the athlete, because which day a session lands on is usually settled on the
# day. Each line has to be actionable on its own — "do more" is not advice.

def insights(summary: dict) -> list[str]:
    """What the SHAPE of the week says, independent of any goal.

    Kept separate from observations() because the goal layer needs exactly these
    lines and none of the others: a goal expresses "two base sessions short" as a
    number, but "all of it was hard" is a judgement about the shape that no
    target can carry. Defined once here so the page, the brief and the goal layer
    cannot word the same finding three ways.
    """
    c = summary["counts"]
    out: list[str] = []
    if c["aerobic_base"] == 0 and c["aerobic_quality"] > 0:
        out.append("All of this week's aerobic work was hard. Easy volume is what "
                   "most of the adaptation comes from.")
    if c["strength"] and not (c["aerobic_base"] or c["aerobic_quality"]):
        out.append("Strength only so far, no aerobic work.")
    return out


def observations(summary: dict, target_sessions: int = 6) -> list[str]:
    """Plain statements about the week so far, strongest first.

    This is the goal-free reading. With a goal set, goals.observations() says the
    same things against that goal's own targets instead.
    """
    out: list[str] = []

    if not summary["has_long_base"]:
        longest = summary["longest_endurance_minutes"]
        out.append(
            f"No long aerobic session yet — the longest was {longest:.0f} min, "
            f"against the {LONG_MINUTES} min that counts as one."
            if longest else "No aerobic session yet this week.")

    out.extend(insights(summary))

    for day in summary["interference"]:
        out.append(
            f"{day['d']}: {' + '.join(day['sports'])} {day['gap_hours']} h apart. "
            f"Under {INTERFERENCE_HOURS:.0f} h the two blunt each other; "
            "separating them is worth a few per cent of strength.")

    done = summary["sessions"]
    if done < target_sessions:
        out.append(f"{done} of about {target_sessions} sessions, "
                   f"{summary['total_minutes']} min so far.")

    return out

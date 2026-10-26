"""What the system said, and what actually happened.

The daily verdict used to be computed and thrown away. So the one question worth
asking about a daily adviser — is it any good, and do I act on it? — had no data
behind it at all. You either believed it or you did not.

Two deliberate limits on what this claims.

**It grades what was done on four rungs, not five.** "Rest", "recovery", "easy"
and "hard" are all readable from the session data. "Moderate" is not: there is no
measurement that separates a moderate day from an easy one without inventing a
threshold. So a moderate verdict counts as followed by either an easy or a hard
day, and that is stated rather than hidden behind a number.

**It flags disagreement, it does not explain it.** A hard day against a rest
verdict might be a mistake, or might be the day the rules did not know about. The
record says the two differ; why they differ is not in the data.

Nothing here is medical advice.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from . import week as _week

# The verdict's own ladder, lowest first. A copy of brief.LEVELS, pinned by a
# test, because brief imports this module's siblings and a cycle helps nobody.
SAID_LEVELS = ("rest", "recovery", "easy", "moderate", "hard")

# What a day's sessions can actually be graded as. Four rungs, not five: see the
# module docstring for why "moderate" is missing.
DONE_LEVELS = ("rest", "recovery", "easy", "hard")

# A moderate verdict is satisfied by either of these, because the data cannot
# tell a moderate day from an easy one.
MODERATE_SATISFIED_BY = ("easy", "hard")


def record_verdict(conn: sqlite3.Connection, v: dict, *, now: str | None = None) -> bool:
    """Store the day's verdict, once. Returns True when a row was written.

    INSERT OR IGNORE on purpose: the first verdict of the day is the morning's,
    and training_readiness is recomputed after every session — so an evening call
    must not replace the judgement that was actually acted on.
    """
    if not v or not v.get("date") or not v.get("level"):
        return False
    stamp = now or _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT OR IGNORE INTO daily_verdict (calendar_date, level, headline, "
        "limiter, detail, race, goal_id, phase, week_focus, recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (v["date"], v["level"], v.get("headline"), v.get("limiter"),
         v.get("detail"), v.get("race"), v.get("goal_id"), v.get("phase"),
         v.get("week_focus"), stamp))
    conn.commit()
    return cur.rowcount > 0


def done_level(sessions: list[dict]) -> str:
    """What a day's sessions actually came to, on the four-rung ladder.

    Uses week.py's own classification, so "hard" means exactly what it means
    everywhere else in this system: eight or more minutes above the zone-4 floor.
    """
    if not sessions:
        return "rest"
    qualities = {s.get("quality") for s in sessions}
    if "aerobic_quality" in qualities:
        return "hard"
    if qualities <= {"restorative"}:
        return "recovery"
    return "easy"


def adherence(said: str | None, done: str | None) -> str | None:
    """"followed", "exceeded", "under", or None when it cannot be judged.

    "exceeded" is the case that matters: the day it said rest and the session was
    hard. "under" is not a failure — a missed easy day costs nothing — but it is
    worth counting, because an adviser nobody follows is worth knowing about.
    """
    if said not in SAID_LEVELS or done not in DONE_LEVELS:
        return None
    if said == "moderate":
        return "followed" if done in MODERATE_SATISFIED_BY else "under"
    if said == done:
        return "followed"
    return "exceeded" if DONE_LEVELS.index(done) > SAID_LEVELS.index(said) else "under"


def history(conn: sqlite3.Connection, days: int = 30,
            through: str | None = None) -> list[dict]:
    """Each stored verdict beside what the day actually came to, newest first."""
    end = through or _dt.date.today().isoformat()
    start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=days - 1)).isoformat()
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_verdict WHERE calendar_date >= ? AND calendar_date <= ? "
        "ORDER BY calendar_date DESC", (start, end))]
    if not rows:
        return []

    by_day: dict[str, list[dict]] = {}
    for s in _week.sessions(conn, start, end):
        by_day.setdefault(s["d"], []).append(s)

    out = []
    for r in rows:
        sessions = by_day.get(r["calendar_date"], [])
        done = done_level(sessions)
        out.append({
            **r,
            "done": done,
            "adherence": adherence(r["level"], done),
            "sessions": len(sessions),
            "minutes": round(sum(s["minutes"] or 0 for s in sessions)),
            "sports": [s["sport"] for s in sessions],
        })
    return out


def summarize(rows: list[dict]) -> dict:
    """How the advice and the days line up, counted.

    `by_said` is the useful cut: "it said rest 6 times; you rested 4 of them".
    """
    counts = {"followed": 0, "exceeded": 0, "under": 0, "unknown": 0}
    by_said: dict[str, dict] = {}
    for r in rows:
        key = r.get("adherence") or "unknown"
        counts[key] += 1
        said = r.get("level")
        if said:
            slot = by_said.setdefault(said, {"days": 0, "followed": 0,
                                             "exceeded": 0, "under": 0})
            slot["days"] += 1
            if key in slot:
                slot[key] += 1
    judged = counts["followed"] + counts["exceeded"] + counts["under"]
    return {
        "days": len(rows),
        "judged": judged,
        **counts,
        # None rather than 0 when nothing could be judged: a rate over no days is
        # not zero per cent, it is nothing.
        "followed_pct": round(counts["followed"] / judged * 100) if judged else None,
        "by_said": by_said,
    }


def observations(rows: list[dict]) -> list[str]:
    """Plain statements about the advice itself, strongest first."""
    s = summarize(rows)
    if not s["days"]:
        return ["No verdict has been recorded yet."]
    out: list[str] = []
    if s["judged"]:
        out.append(f"{s['followed']} of {s['judged']} days matched the verdict "
                   f"({s['followed_pct']}%).")
    # The one case worth naming on its own — and it has to BE that case. An easy
    # session against a recovery verdict also counts as "exceeded", and calling
    # that "went hard" overstates it. Only a hard day earns the sentence.
    hard_over_rest = [r for r in rows
                      if r.get("done") == "hard"
                      and r.get("level") in ("rest", "recovery")]
    if hard_over_rest:
        days = ", ".join(r["calendar_date"] for r in hard_over_rest[:3])
        out.append(f"{len(hard_over_rest)} day(s) went hard against a rest or "
                   f"recovery verdict: {days}"
                   + ("…" if len(hard_over_rest) > 3 else "") + ".")
    # Everything else that went over is counted, not dramatised.
    milder = s["exceeded"] - len(hard_over_rest)
    if milder > 0:
        out.append(f"{milder} further day(s) went beyond the verdict without "
                   "going hard.")
    for said in ("rest", "hard"):
        slot = s["by_said"].get(said)
        if slot and slot["days"] >= 3:
            out.append(f'It said "{said}" on {slot["days"]} days; '
                       f'{slot["followed"]} of them matched.')
    if s["unknown"]:
        out.append(f"{s['unknown']} day(s) could not be judged — the verdict or "
                   "the session data is missing.")
    return out

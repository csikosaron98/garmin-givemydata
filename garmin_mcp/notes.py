"""What the watch cannot see, and when to ask for it.

Everything else in this database is measured. The things that actually explain a
bad week are not: ill, travelling, a deadline, a calf that hurts. Without them the
system reads "resting heart rate up four beats" and has no idea it was a head
cold.

Two halves.

**A place to write it.** Free text, with an optional tag from a small documented
vocabulary — documented because a tag the rules can read is worth more than one
they can only display, and a vocabulary that is not written down is one nobody
can use.

**A reason to write it.** A daily prompt decays inside a fortnight. So the system
asks only when it sees something it cannot explain, at most once per episode, and
stops asking the moment there is an answer. Rarely asked is what makes it worth
answering.

What a tag may do is deliberately narrow. `illness` lowers the day's ceiling to
recovery, which is the same conservative direction the existing sleep and
readiness rules already move in, and it is reported as the limiting factor like
any other. `niggle` is reported and changes nothing: which tissue hurts and how
much is not something this data can grade, and guessing would be worse than
saying nothing.

Nothing here is medical advice.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

# tag -> (label, what it does, why)
TAGS = {
    "illness": {
        "label": "ill",
        "caps_at": "recovery",
        "why": "Training through an illness is the one case where the measured "
               "signals lag what you already know.",
    },
    "niggle": {
        "label": "a niggle",
        "caps_at": None,
        "why": "Reported, not graded: which tissue hurts and how much is not "
               "something this data can judge.",
    },
    "travel": {
        "label": "travelling",
        "caps_at": None,
        "why": "Explains a short night and a high resting heart rate without "
               "either meaning anything about your training.",
    },
    "stress": {
        "label": "under load outside training",
        "caps_at": None,
        "why": "Shows up in HRV and resting heart rate exactly like training "
               "load does, and is not training load.",
    },
    "alcohol": {
        "label": "alcohol",
        "caps_at": None,
        "why": "Moves overnight HRV and resting heart rate for a night or two.",
    },
}

# How many consecutive days a signal has to be off before it is worth a question.
# Three, because one is noise and two is a bad night.
ANOMALY_DAYS = 3

# Resting heart rate this far above the mean counts as off. Taken FROM the daily
# rules rather than restated, so "elevated" cannot come to mean two numbers.
from .brief_thresholds import RHR_OVER_BASELINE as RHR_OVER_MEAN  # noqa: E402

# Having asked, do not ask again for this long unless a new episode starts.
ASK_COOLDOWN_DAYS = 7


def add_note(conn: sqlite3.Connection, date: str, note: str,
             tags: str | None = None, *, now: str | None = None) -> int:
    """Store one note. Returns its id."""
    if not (note or "").strip():
        raise ValueError("a note needs something in it")
    _dt.date.fromisoformat(date)                      # raises on a bad date
    clean = normalise_tags(tags)
    cur = conn.execute(
        "INSERT INTO daily_note (calendar_date, note, tags, created) VALUES (?,?,?,?)",
        (date, note.strip(), clean or None,
         now or _dt.datetime.now().astimezone().isoformat(timespec="seconds")))
    conn.commit()
    return int(cur.lastrowid)


def normalise_tags(tags: str | None) -> str:
    """The known tags from a comma-separated string, in a stable order.

    Unknown words are dropped rather than stored: a tag nothing can read is a
    tag that will quietly stop meaning anything. The note itself keeps the words.
    """
    if not tags:
        return ""
    wanted = {t.strip().lower() for t in str(tags).split(",") if t.strip()}
    return ",".join(t for t in TAGS if t in wanted)


def notes_between(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM daily_note WHERE calendar_date >= ? AND calendar_date <= ? "
        "ORDER BY calendar_date DESC, note_id DESC", (start, end))]


def notes_for(conn: sqlite3.Connection, date: str) -> list[dict]:
    return notes_between(conn, date, date)


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM daily_note WHERE note_id = ?", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def active_tags(conn: sqlite3.Connection, date: str) -> set[str]:
    """Tags written for `date` itself. A note is about the day it is filed under."""
    out: set[str] = set()
    for row in notes_for(conn, date):
        for t in (row.get("tags") or "").split(","):
            if t:
                out.add(t)
    return out


def cap_from_notes(conn: sqlite3.Connection, date: str) -> tuple[str | None, str]:
    """The ceiling the day's notes impose, and the reason — or (None, "").

    Only ever lowers, and only `illness` lowers anything. The same direction the
    sleep and readiness rules already move in.
    """
    for tag in active_tags(conn, date):
        caps = TAGS[tag]["caps_at"]
        if caps:
            return caps, f"you noted {TAGS[tag]['label']} today"
    return None, ""


def describe_notes(conn: sqlite3.Connection, date: str) -> str:
    """The day's notes in one line, for the brief."""
    rows = notes_for(conn, date)
    if not rows:
        return ""
    bits = []
    for r in rows:
        labels = [TAGS[t]["label"] for t in (r.get("tags") or "").split(",") if t]
        bits.append(r["note"] + (f" ({', '.join(labels)})" if labels else ""))
    return "Your note: " + " · ".join(bits)


# ---------------------------------------------------------------------------
# when to ask
# ---------------------------------------------------------------------------

def _series(conn: sqlite3.Connection, date: str, days: int) -> list[dict]:
    start = (_dt.date.fromisoformat(date) - _dt.timedelta(days=days - 1)).isoformat()
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT d.calendar_date AS d, d.resting_heart_rate AS rhr, "
        "       h.last_night_avg AS hrv, h.baseline_low AS hrv_low, "
        "       ROUND(s.sleep_time_seconds/3600.0, 2) AS sleep_h "
        "  FROM daily_summary d "
        "  LEFT JOIN hrv h ON h.calendar_date = d.calendar_date "
        "  LEFT JOIN sleep s ON s.calendar_date = d.calendar_date "
        " WHERE d.calendar_date >= ? AND d.calendar_date <= ? "
        " ORDER BY d.calendar_date", (start, date))]


def anomalies(conn: sqlite3.Connection, date: str) -> list[dict]:
    """Signals that have been off for ANOMALY_DAYS running, ending today.

    Each one is a thing the data shows and cannot explain. None of them is a
    diagnosis, and none of them changes the day's ceiling on its own.
    """
    rows = _series(conn, date, 21)
    if not rows or rows[-1]["d"] != date:
        return []
    out = []

    # Resting heart rate against its own fourteen-day mean, excluding the days
    # being judged — otherwise a sustained rise lifts the mean it is measured
    # against and hides itself.
    tail = rows[-ANOMALY_DAYS:]
    base = [r["rhr"] for r in rows[:-ANOMALY_DAYS] if r["rhr"] is not None]
    if len(tail) == ANOMALY_DAYS and len(base) >= 5:
        mean = sum(base) / len(base)
        if all(r["rhr"] is not None and r["rhr"] - mean > RHR_OVER_MEAN for r in tail):
            out.append({
                "what": "resting heart rate",
                "detail": f"{ANOMALY_DAYS} days at {round(mean + RHR_OVER_MEAN)} bpm or "
                          f"above, against a {len(base)}-day mean of {mean:.1f}",
                "since": tail[0]["d"],
            })

    if len(tail) == ANOMALY_DAYS and all(
            r["hrv"] is not None and r["hrv_low"] is not None and r["hrv"] < r["hrv_low"]
            for r in tail):
        out.append({
            "what": "overnight HRV",
            "detail": f"{ANOMALY_DAYS} nights below your own baseline floor",
            "since": tail[0]["d"],
        })

    if len(tail) == ANOMALY_DAYS and all(
            r["sleep_h"] is not None and r["sleep_h"] < 6 for r in tail):
        out.append({
            "what": "sleep",
            "detail": f"{ANOMALY_DAYS} nights under six hours",
            "since": tail[0]["d"],
        })
    return out


def should_ask(conn: sqlite3.Connection, date: str) -> str:
    """The question to put in today's brief, or "" when there is nothing to ask.

    Silent unless something has been off for three days running, and silent again
    the moment there is a note — rarely asked is what makes it worth answering.
    """
    found = anomalies(conn, date)
    if not found:
        return ""
    since = (_dt.date.fromisoformat(date)
             - _dt.timedelta(days=ASK_COOLDOWN_DAYS - 1)).isoformat()
    if notes_between(conn, since, date):
        return ""                      # already answered; do not nag
    what = ", ".join(f"{a['what']} ({a['detail']})" for a in found)
    return (f"Something the data cannot explain: {what}. "
            "Anything going on — ill, travelling, a heavy week? "
            "A line is enough, and it stops this asking again.")

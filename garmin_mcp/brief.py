"""The morning training brief, sent by the server itself.

Why this exists rather than a scheduled assistant run: the brief has to arrive
*after waking*, and a clock-based schedule cannot do that. Measured over 22 days,
a 07:30 scheduled task produced 12 emails, two of which went out at 13:33 and
21:18 — and an evening run reads a post-session readiness snapshot and would
present it as a waking value. This runs on the same 15-minute timer as the sync,
sends once the night's data has actually landed, and sends only once a day.

Entry point::

    python -m garmin_mcp.brief            # send if it is time and not yet sent
    python -m garmin_mcp.brief --dry-run  # print the brief, send nothing
    python -m garmin_mcp.brief --force    # ignore the once-a-day marker

Configuration comes from the same ``.env`` the sync uses. **The password is never
read, logged or echoed by anything here except smtplib** — it is passed straight
through.

    BRIEF_TO             where to send it
    BRIEF_SMTP_USER      the sending account
    BRIEF_SMTP_PASSWORD  an app password, set by Áron, never by a tool
    BRIEF_SMTP_HOST      default smtp.gmail.com
    BRIEF_SMTP_PORT      default 587 (STARTTLS)

Nothing here is medical advice: it is an open rule set over Garmin's own metrics.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import smtplib
import sqlite3
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from pathlib import Path

from . import goals as _goals
from . import week as _week
from .db import DB_PATH, get_connection

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent

# The ceiling ladder. Every rule lowers it; none ever raises it.
LEVELS = ("rest", "recovery", "easy", "moderate", "hard")

# Áron's own zones, from his Garmin profile (HRmax 189, LTHR 169, RHR ~41).
ZONES = {
    1: (95, 112),
    2: (113, 131),
    3: (132, 150),
    4: (151, 169),
    5: (170, 189),
}

# Only send inside a plausible morning. Outside it the night's figures are no
# longer "this morning's", and a brief that says they are would be wrong.
#
# In ÁRON's morning, not the server's: the VM runs on UTC, so a naive
# datetime.now() made this 07:00-13:00 Budapest in summer and 06:00-12:00 in
# winter — an early riser would have waited hours for a brief about a night that
# had already landed.
SEND_FROM_HOUR = 5
SEND_TO_HOUR = 11
DEFAULT_TZ = "Europe/Budapest"

SENT_MARKER = ".brief-sent"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_env(project_dir: Path | None = None) -> None:
    """Read .env the way sync.py does — setdefault, so the real environment wins."""
    env_file = (project_dir or PROJECT_DIR) / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


# One mapping, used both to READ the settings and to NAME them when they are
# missing. Spelling these twice sent the first run looking for BRIEF_PASSWORD,
# a variable nothing reads — an error message that misdirects is worse than none.
ENV_KEYS = {
    "to": "BRIEF_TO",
    "user": "BRIEF_SMTP_USER",
    "password": "BRIEF_SMTP_PASSWORD",
}


def smtp_config() -> dict:
    """The mail settings. Reports WHICH variable is missing, never its value."""
    cfg = {key: os.environ.get(env, "") for key, env in ENV_KEYS.items()}
    cfg["host"] = os.environ.get("BRIEF_SMTP_HOST", "smtp.gmail.com")
    cfg["port"] = int(os.environ.get("BRIEF_SMTP_PORT", "587"))
    cfg["missing"] = [env for key, env in ENV_KEYS.items() if not cfg[key]]
    return cfg


# --------------------------------------------------------------------------
# reading the day
# --------------------------------------------------------------------------

def read_day(conn: sqlite3.Connection, date: str) -> dict | None:
    """Everything the decision needs for one calendar day, or None if the
    night has not landed yet.

    training_readiness holds the day's MOST RECENT snapshot, not the pre-dawn
    one — it is recomputed after every session. That is exactly why this module
    refuses to run outside the morning window.
    """
    row = conn.execute(
        """
        SELECT r.calendar_date, r.score AS readiness, r.level AS readiness_level,
               r.recovery_time,
               s.sleep_time_seconds, s.sleep_score_overall AS sleep_score,
               s.deep_sleep_seconds, s.rem_sleep_seconds,
               h.last_night_avg AS hrv, h.status AS hrv_status,
               h.baseline_low AS hrv_low, h.baseline_upper AS hrv_high,
               h.weekly_avg AS hrv_weekly,
               d.resting_heart_rate AS rhr, d.body_battery_at_wake AS bb_wake,
               d.average_stress_level AS stress,
               t.status AS training_status, t.acute_load, t.chronic_load
          FROM training_readiness r
          LEFT JOIN sleep s ON s.calendar_date = r.calendar_date
          LEFT JOIN hrv h ON h.calendar_date = r.calendar_date
          LEFT JOIN daily_summary d ON d.calendar_date = r.calendar_date
          LEFT JOIN training_status t ON t.calendar_date = r.calendar_date
         WHERE r.calendar_date = ?
        """,
        (date,),
    ).fetchone()
    if row is None:
        return None
    day = dict(row)
    # Sleep and readiness are the two the brief cannot be written without.
    if day.get("sleep_time_seconds") is None or day.get("readiness") is None:
        return None
    day["sleep_h"] = day["sleep_time_seconds"] / 3600.0
    return day


def rhr_baseline(conn: sqlite3.Connection, date: str, days: int = 14) -> float | None:
    """The resting-HR mean over the days BEFORE `date`.

    Excludes `date` itself: comparing today against an average that already
    contains today flattens exactly the excursion worth noticing.
    """
    rows = conn.execute(
        """
        SELECT resting_heart_rate FROM daily_summary
         WHERE calendar_date < ? AND resting_heart_rate IS NOT NULL
         ORDER BY calendar_date DESC LIMIT ?
        """,
        (date, days),
    ).fetchall()
    vals = [r[0] for r in rows]
    return round(sum(vals) / len(vals), 1) if vals else None


def recent_sessions(conn: sqlite3.Connection, date: str, days: int = 3) -> list[dict]:
    """Sessions in the `days` days before `date`, newest first."""
    start = add_days(date, -days)
    rows = conn.execute(
        """
        SELECT substr(start_time_local, 1, 10) AS d, activity_type, activity_name,
               duration_seconds, distance_meters, average_hr, max_hr,
               training_load, aerobic_training_effect, anaerobic_training_effect
          FROM activity
         WHERE substr(start_time_local, 1, 10) >= ?
           AND substr(start_time_local, 1, 10) < ?
         ORDER BY start_time_local DESC
        """,
        (start, date),
    ).fetchall()
    return [dict(r) for r in rows]


def add_days(iso: str, n: int) -> str:
    return (_dt.date.fromisoformat(iso) + _dt.timedelta(days=n)).isoformat()


def is_hard(session: dict) -> bool:
    """A session that costs recovery, by Garmin's own training effect.

    Training load alone does not separate them: a 90-minute easy run and a
    20-minute set of intervals can carry the same load for very different costs.
    """
    anaerobic = session.get("anaerobic_training_effect") or 0
    aerobic = session.get("aerobic_training_effect") or 0
    return anaerobic >= 2.5 or aerobic >= 3.5


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def decide(day: dict, rhr_mean: float | None, sessions: list[dict]) -> dict:
    """Lower a ceiling from 'hard' and name whichever rule set it.

    Every rule can only lower it. The limiting factor reported is the rule that
    set the final level — so the email can always say WHY today cannot be more,
    which is the whole point of the thing.
    """
    level = "hard"
    limiter = None
    detail = None
    notes: list[str] = []

    def cap(to: str, why: str, says: str) -> None:
        nonlocal level, limiter, detail
        if LEVELS.index(to) < LEVELS.index(level):
            level, limiter, detail = to, why, says

    readiness = day.get("readiness")
    if readiness is not None:
        if readiness < 25:
            cap("rest", "readiness", f"Readiness {readiness:.0f} (POOR)")
        elif readiness < 50:
            cap("recovery", "readiness", f"Readiness {readiness:.0f} (LOW)")
        elif readiness < 75:
            cap("moderate", "readiness", f"Readiness {readiness:.0f} (MODERATE)")

    # Garmin's own countdown, in minutes. It is the signal that most often
    # binds on a day when everything else looks fine.
    rec = day.get("recovery_time")
    if rec:
        hours = rec / 60.0
        if hours >= 48:
            cap("recovery", "recovery time", f"{hours:.0f} h of recovery still outstanding")
        elif hours >= 12:
            cap("easy", "recovery time", f"{hours:.0f} h of recovery still outstanding")

    # Below his own lower baseline, not below some population number.
    hrv, hrv_low = day.get("hrv"), day.get("hrv_low")
    if hrv is not None and hrv_low is not None and hrv < hrv_low:
        cap("easy", "HRV", f"HRV {hrv:.0f} ms, below your baseline floor of {hrv_low:.0f}")

    rhr = day.get("rhr")
    if rhr is not None and rhr_mean is not None and rhr > rhr_mean + 3:
        cap("easy", "resting heart rate",
            f"Resting HR {rhr} bpm against a {rhr_mean} bpm 14-day mean")

    sleep_h = day.get("sleep_h")
    sleep_score = day.get("sleep_score")
    if (sleep_h is not None and sleep_h < 6) or (sleep_score is not None and sleep_score < 60):
        lowered = LEVELS[max(0, LEVELS.index(level) - 1)]
        cap(lowered, "sleep",
            f"{sleep_h:.1f} h of sleep, score {sleep_score}" if sleep_score is not None
            else f"{sleep_h:.1f} h of sleep")

    hard_days = {s["d"] for s in sessions if is_hard(s)}
    if len(hard_days) >= 2:
        cap("moderate", "recent load", f"{len(hard_days)} hard days in the last three")

    # Described, never used as a prediction: the post-2020 literature
    # (Impellizzeri et al.) refuted ACWR for injury forecasting.
    acute, chronic = day.get("acute_load"), day.get("chronic_load")
    if acute and chronic:
        ratio = acute / chronic
        notes.append(f"Acute load {acute:.0f} against chronic {chronic:.0f}, a ratio of {ratio:.2f}")

    if limiter is None:
        # Nothing bound. Say what came closest, so "hard" is not a bare assertion.
        if rhr is not None and rhr_mean is not None:
            detail = (f"nothing binding — closest is resting HR {rhr} bpm "
                      f"against a {rhr_mean} bpm mean")
        else:
            detail = "nothing binding"

    return {"level": level, "limiter": limiter, "detail": detail, "notes": notes}


def zone_text(z: int) -> str:
    lo, hi = ZONES[z]
    return f"Z{z} {lo}–{hi} bpm"


PRESCRIPTIONS = {
    "rest": ("Rest day",
             "No training. Walking is fine; nothing that raises your heart rate."),
    "recovery": ("Recovery day",
                 "20–40 minutes very easy, or nothing at all. " + zone_text(1) + "."),
    "easy": ("Easy aerobic day",
             "40–70 minutes conversational. " + zone_text(2) +
             " — if it drifts above the top of Z2, slow down rather than push through."),
    "moderate": ("Moderate day",
                 "50–80 minutes mostly in Z2 with 2×10–15 minutes of Z3 inside it. "
                 + zone_text(2) + ", lifting to " + zone_text(3) + "."),
    "hard": ("Hard day — spend it on running",
             "Intervals or a threshold run. 15 minutes warm-up in Z2, then the work in "
             + zone_text(4) + ", then easy Z2 to finish."),
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def fmt_hm(hours: float | None) -> str:
    if hours is None:
        return "—"
    total = int(round(hours * 60))
    return f"{total // 60}:{total % 60:02d}"


def week_advice(wk: dict, goal, date: str | None) -> list[str]:
    """The week's advice, from the goal layer when a goal is set.

    One function for both renderings: the plain-text and the HTML halves of the
    email used to each call the advice themselves, which is one edit away from
    the two saying different things about the same morning.
    """
    if goal is not None and date:
        return _goals.observations(wk, goal, date)
    return _week.observations(wk)


def render_text(day: dict, decision: dict, rhr_mean, sessions: list[dict],
                wk: dict | None = None, goal=None, date: str | None = None) -> str:
    headline, body = PRESCRIPTIONS[decision["level"]]
    lines = [f"TODAY — {headline}", "", body, ""]
    if decision["limiter"]:
        lines += [f"Limiting factor: {decision['limiter']}. {decision['detail']}.", ""]
    else:
        lines += [f"Limiting factor: {decision['detail']}.", ""]

    if sessions:
        lines.append("RECENT SESSIONS")
        for s in sessions[:3]:
            km = (s.get("distance_meters") or 0) / 1000.0
            mins = (s.get("duration_seconds") or 0) / 60.0
            dist = f", {km:.1f} km" if km >= 0.1 else ""
            load = s.get("training_load")
            lines.append(f"{s['d']}  {s['activity_type']}  {mins:.0f} min{dist}"
                         + (f", load {load:.0f}" if load else ""))
        lines.append("")

    if wk:
        c = wk["counts"]
        lines.append("THIS WEEK")
        lines.append(f"{wk['sessions']} sessions over {wk['days']} days, "
                     f"{wk['total_minutes']} min")
        lines.append(f"  aerobic base {c['aerobic_base']} · quality {c['aerobic_quality']}"
                     f" · strength {c['strength']} · mixed {c['mixed']}"
                     f" · restorative {c['restorative']}")
        for note in week_advice(wk, goal, date):
            lines.append(f"  - {note}")
        lines.append("")

    lines.append("LAST NIGHT")
    lines.append(f"Sleep        {fmt_hm(day.get('sleep_h'))}, score {day.get('sleep_score', '—')}")
    if day.get("hrv") is not None:
        lines.append(f"HRV          {day['hrv']:.0f} ms, {day.get('hrv_status', '')}"
                     f" — baseline {day.get('hrv_low', 0):.0f}–{day.get('hrv_high', 0):.0f}")
    lines.append(f"Resting HR   {day.get('rhr', '—')} bpm"
                 + (f", 14-day mean {rhr_mean}" if rhr_mean else ""))
    lines.append(f"Body Battery {day.get('bb_wake', '—')} on waking")
    for note in decision["notes"]:
        lines += ["", note]
    lines += ["", "———",
              "Built from your own Garmin data. Not medical advice — an open rule set "
              "over Garmin's own metrics, and it does not replace what you feel."]
    return "\n".join(lines)


def _row(label: str, value: str) -> str:
    return (f'<tr><td style="padding:7px 0;border-bottom:1px solid #efece3;width:36%;'
            f'color:#6b6a5e;">{label}</td>'
            f'<td style="padding:7px 0;border-bottom:1px solid #efece3;'
            f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{value}</td></tr>')


def render_html(day: dict, decision: dict, rhr_mean, sessions: list[dict],
                date: str, wk: dict | None = None, goal=None) -> str:
    """Table layout and inline styles only — mail clients strip <style> blocks
    and support neither flex nor grid. Palette matches the dashboard so the two
    read as one system.
    """
    headline, body = PRESCRIPTIONS[decision["level"]]
    pretty = _dt.date.fromisoformat(date).strftime("%A %-d %B")
    limiter = (f"<b style=\"color:#a8620c;\">Limiting factor &mdash; {decision['limiter']}.</b> "
               f"{decision['detail']}." if decision["limiter"]
               else f"<b style=\"color:#a8620c;\">No limiting factor.</b> {decision['detail']}.")

    rows = [
        _row("Sleep", f"<b>{fmt_hm(day.get('sleep_h'))}</b> &middot; score {day.get('sleep_score', '&mdash;')}"),
    ]
    if day.get("hrv") is not None:
        rows.append(_row("HRV", f"<b>{day['hrv']:.0f} ms</b> "
                                f"<span style=\"color:#6b6a5e;\">{day.get('hrv_status','')} &middot; "
                                f"baseline {day.get('hrv_low',0):.0f}&ndash;{day.get('hrv_high',0):.0f}</span>"))
    rows.append(_row("Resting HR", f"<b>{day.get('rhr','&mdash;')} bpm</b>"
                                   + (f" <span style=\"color:#6b6a5e;\">&middot; 14-day mean {rhr_mean}</span>"
                                      if rhr_mean else "")))
    rows.append(_row("Body Battery", f"<b>{day.get('bb_wake','&mdash;')}</b> "
                                     f"<span style=\"color:#6b6a5e;\">on waking</span>"))

    session_rows = ""
    for s in sessions[:3]:
        km = (s.get("distance_meters") or 0) / 1000.0
        mins = (s.get("duration_seconds") or 0) / 60.0
        bits = f"{mins:.0f} min"
        if km >= 0.1:
            bits += f" &middot; {km:.1f} km"
        if s.get("training_load"):
            bits += f" &middot; load {s['training_load']:.0f}"
        session_rows += _row(f"{s['d']} {s['activity_type'].replace('_',' ')}", bits)

    week_html = ""
    if wk:
        c = wk["counts"]
        chips = [("Aerobic base", c["aerobic_base"]), ("Quality", c["aerobic_quality"]),
                 ("Strength", c["strength"]), ("Mixed", c["mixed"]),
                 ("Restorative", c["restorative"])]
        chip_html = "".join(
            f'<td style="padding:0 16px 0 0;"><div style="font-size:10px;color:#6b6a5e;'
            f'text-transform:uppercase;letter-spacing:.05em;">{label}</div>'
            f'<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
            f'font-size:19px;font-weight:600;color:{"#20241f" if n else "#9a998b"};">{n}</div></td>'
            for label, n in chips)
        notes = "".join(
            f'<div style="font-size:12.5px;color:#20241f;line-height:1.5;padding-top:7px;">'
            f'&middot; {o}</div>' for o in week_advice(wk, goal, date))
        week_html = (
            '<tr><td style="padding:22px 28px 0;">'
            '<div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;'
            'color:#6b6a5e;font-weight:700;padding-bottom:8px;">This week &middot; '
            f'{wk["sessions"]} sessions, {wk["total_minutes"]} min</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{chip_html}</tr></table>'
            f'{notes}</td></tr>')

    notes_html = ""
    if decision["notes"]:
        notes_html = ('<tr><td style="padding:18px 28px 24px;">'
                      '<div style="font-size:13px;color:#20241f;line-height:1.6;background:#f0ede4;'
                      'border-radius:10px;padding:13px 15px;">' + " &middot; ".join(decision["notes"]) +
                      "</div></td></tr>")

    return f"""<div style="margin:0;padding:24px 12px;background:#f6f4ee;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e2ded1;border-radius:14px;">
<tr><td style="padding:26px 28px 18px;border-bottom:1px solid #e2ded1;">
<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#0d7d6f;font-weight:700;">FitBodAI &middot; Training brief</div>
<div style="font-size:23px;font-weight:600;color:#20241f;margin-top:7px;">{pretty}</div>
</td></tr>
<tr><td style="padding:24px 28px 4px;">
<div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#6b6a5e;font-weight:700;">Today</div>
<div style="font-size:21px;font-weight:600;color:#20241f;margin:8px 0 4px;">{headline}</div>
<div style="font-size:14.5px;color:#20241f;line-height:1.55;">{body}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:12px 0 0;"><tr>
<td style="background:#f6e6ce;border-left:3px solid #a8620c;padding:12px 14px;font-size:13px;color:#20241f;line-height:1.55;">{limiter}</td>
</tr></table>
</td></tr>
{'<tr><td style="padding:22px 28px 0;"><div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#6b6a5e;font-weight:700;padding-bottom:6px;">Recent sessions</div><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="font-size:13.5px;color:#20241f;">' + session_rows + '</table></td></tr>' if session_rows else ''}
<tr><td style="padding:20px 28px 0;">
<div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#6b6a5e;font-weight:700;padding-bottom:6px;">Last night</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="font-size:13.5px;color:#20241f;">{''.join(rows)}</table>
</td></tr>
{week_html}
{notes_html}
<tr><td style="padding:0 28px 26px;">
<div style="border-top:1px solid #e2ded1;padding-top:14px;font-size:11.5px;color:#9a998b;line-height:1.55;">
Built from your own Garmin data on your server. Not medical advice &mdash; it is an open rule set over Garmin&rsquo;s own metrics, and it does not replace what you feel. If you are unwell, rest regardless of what the numbers say.
</div>
</td></tr>
</table>
</div>"""


# --------------------------------------------------------------------------
# sending, once a day
# --------------------------------------------------------------------------

def marker_path(db_path: str | None = None) -> Path:
    """Next to the database, so the marker travels with the data it describes."""
    return Path(db_path or DB_PATH).resolve().parent / SENT_MARKER


def already_sent(date: str, path: Path) -> bool:
    try:
        return path.read_text().strip() == date
    except OSError:
        return False


def mark_sent(date: str, path: Path) -> None:
    try:
        path.write_text(date)
    except OSError as exc:
        # Better a duplicate tomorrow than a crash after the mail is away.
        log.warning("could not write the sent marker at %s: %s", path, exc)


def local_now(tz_name: str | None = None) -> _dt.datetime:
    """Now, in the athlete's timezone — never the server's.

    Falls back to the server clock only if the zone is unknown, which on a Linux
    box means the tz database is missing rather than the name being wrong.
    """
    name = tz_name or os.environ.get("BRIEF_TZ", DEFAULT_TZ)
    try:
        return _dt.datetime.now(ZoneInfo(name))
    except Exception as exc:
        log.warning("unknown timezone %r (%s) — falling back to the server clock", name, exc)
        return _dt.datetime.now()


def in_send_window(now: _dt.datetime) -> bool:
    return SEND_FROM_HOUR <= now.hour < SEND_TO_HOUR


def send_email(cfg: dict, subject: str, text: str, html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def build(conn: sqlite3.Connection, date: str) -> tuple[str, str, str] | None:
    """(subject, text, html), or None when the night has not landed yet."""
    day = read_day(conn, date)
    if day is None:
        return None
    rhr_mean = rhr_baseline(conn, date)
    sessions = recent_sessions(conn, date)
    decision = decide(day, rhr_mean, sessions)
    # The week so far, so the brief can say what is still MISSING rather than
    # only what today's readiness allows. A broken tally must not cost the brief.
    try:
        wk = _week.summarize(conn, date)
    except Exception as exc:
        log.warning("could not summarise the week: %s", exc)
        wk = None
    # The goal the week is judged against. None is a legitimate answer, and a
    # failure here must cost the brief no more than the goal line: the rest of
    # the email is about today, which does not depend on it.
    try:
        goal = _goals.driving_goal(conn, date)
    except Exception as exc:
        log.warning("could not read the goal: %s", exc)
        goal = None
    pretty = _dt.date.fromisoformat(date).strftime("%A %-d %B")
    return (
        f"Training brief — {pretty}",
        render_text(day, decision, rhr_mean, sessions, wk, goal, date),
        render_html(day, decision, rhr_mean, sessions, date, wk, goal),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print it, send nothing")
    parser.add_argument("--force", action="store_true", help="ignore the once-a-day marker and the time window")
    parser.add_argument("--date", default=None, help="the day to build (default: today)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()

    now = local_now()
    date = args.date or now.date().isoformat()
    marker = marker_path()

    if not args.force and not args.dry_run:
        if not in_send_window(now):
            log.info("outside the %02d:00-%02d:00 send window, nothing to do",
                     SEND_FROM_HOUR, SEND_TO_HOUR)
            return 0
        if already_sent(date, marker):
            log.info("already sent for %s", date)
            return 0

    conn = get_connection()
    try:
        built = build(conn, date)
    finally:
        conn.close()

    if built is None:
        # Normal before the watch has uploaded. Not an error; the next run retries.
        log.info("no sleep or readiness for %s yet — waiting", date)
        return 0

    subject, text, html = built
    if args.dry_run:
        print(subject)
        print()
        print(text)
        return 0

    cfg = smtp_config()
    if cfg["missing"]:
        log.error("mail is not configured: %s missing from .env",
                  ", ".join(cfg["missing"]))
        return 1

    send_email(cfg, subject, text, html)
    mark_sent(date, marker)
    log.info("brief sent for %s", date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

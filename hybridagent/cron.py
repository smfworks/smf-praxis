"""Cron / scheduling for Praxis — unattended, recurring autonomy.

A cron job pairs a *goal* with a *schedule* and a *delivery* target. The daemon
ticks the scheduler; when a job is due it runs the goal through the governed
agent loop (same broker, approvals, audit as any other run) and optionally
delivers the result to a messaging gateway.

Schedule grammar (kept deliberately small + stdlib-only):
* ``"30m"`` / ``"2h"`` / ``"90s"`` / ``"1d"``      — fixed interval
* ``"every 15m"``                                    — same, friendlier
* ``"hourly"`` / ``"daily"`` / ``"weekly"``          — keyword intervals
* ``"@09:00"`` / ``"daily@09:00"``                   — daily at a wall-clock time
* ``"0 9 * * *"``                                    — 5-field cron (min hour dom mon dow)

next_run is computed in local time. The scheduler is resilient: an unparseable
schedule disables the job rather than crashing the tick loop.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_KEYWORDS = {"hourly": 3600.0, "daily": 86400.0, "weekly": 604800.0}
_VALID_MODES = {"do", "ask", "research", "agent"}


@dataclass
class NextRun:
    ts: float | None
    reason: str = ""


def _parse_interval(spec: str) -> float | None:
    m = re.fullmatch(r"(?:every\s+)?(\d+(?:\.\d+)?)\s*([smhd])", spec)
    if not m:
        return None
    return float(m.group(1)) * _INTERVAL_UNITS[m.group(2)]


def _parse_daily_at(spec: str, now: datetime) -> float | None:
    """'@09:00' or 'daily@09:00' -> next occurrence of that local time."""
    m = re.fullmatch(r"(?:daily\s*)?@\s*(\d{1,2}):(\d{2})", spec)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def _parse_cron5(spec: str, now: datetime) -> float | None:
    """Minimal 5-field cron: minute hour day-of-month month day-of-week.

    Supports '*', exact numbers, comma lists, and '*/n' steps. Scans forward
    minute-by-minute up to ~366 days; returns the next matching epoch second.

    NOTE: when BOTH day-of-month and day-of-week are restricted, this uses AND
    semantics (both must match), which differs from Vixie cron's OR semantics.
    AND is the more intuitive reading and a rare combination in practice; a
    schedule needing OR should be split into two jobs. Out-of-range fields (e.g.
    minute 60, dow 9) are rejected up front so an unsatisfiable expression fails
    fast instead of scanning a full year of minutes.
    """
    fields = spec.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, mon, dow = fields

    def _valid(field: str, lo: int, hi: int) -> bool:
        """Reject a field whose literals fall outside [lo,hi] up front, so an
        unsatisfiable expression (e.g. minute '60', dow '9') fails fast instead
        of triggering a futile ~527k-iteration year-long minute scan on every
        scheduler tick."""
        if field == "*":
            return True
        if field.startswith("*/"):
            return field[2:].isdigit() and int(field[2:]) > 0
        for part in field.split(","):
            if not part.lstrip("-").isdigit():
                return False
            if not (lo <= int(part) <= hi):
                return False
        return True

    if not (_valid(minute, 0, 59) and _valid(hour, 0, 23)
            and _valid(dom, 1, 31) and _valid(mon, 1, 12)
            and _valid(dow, 0, 6)):
        return None

    def _match(field: str, value: int, lo: int, hi: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            try:
                step = int(field[2:])
            except ValueError:
                return False
            return step > 0 and (value - lo) % step == 0
        for part in field.split(","):
            try:
                if int(part) == value:
                    return True
            except ValueError:
                return False
        return False

    # Start from the next whole minute so we don't re-fire the current minute.
    t = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    for _ in range(366 * 24 * 60):
        # Python weekday: Mon=0..Sun=6; cron dow: Sun=0..Sat=6.
        cron_dow = (t.weekday() + 1) % 7
        if (_match(minute, t.minute, 0, 59) and _match(hour, t.hour, 0, 23)
                and _match(dom, t.day, 1, 31) and _match(mon, t.month, 1, 12)
                and _match(dow, cron_dow, 0, 6)):
            return t.timestamp()
        t += timedelta(minutes=1)
    return None


def compute_next_run(schedule: str, *, after: float | None = None) -> NextRun:
    """Compute the next epoch-second a schedule should fire, or None if invalid."""
    spec = (schedule or "").strip().lower()
    if not spec:
        return NextRun(None, "empty schedule")
    base = after if after is not None else time.time()
    now = datetime.fromtimestamp(base)

    if spec in _KEYWORDS:
        return NextRun(base + _KEYWORDS[spec])
    interval = _parse_interval(spec)
    if interval is not None:
        if interval < 1:
            return NextRun(None, "interval too small")
        return NextRun(base + interval)
    daily = _parse_daily_at(spec, now)
    if daily is not None:
        return NextRun(daily)
    cron = _parse_cron5(spec, now)
    if cron is not None:
        return NextRun(cron)
    return NextRun(None, f"unrecognized schedule {schedule!r}")


def normalize_mode(mode: str) -> str:
    mode = (mode or "do").strip().lower()
    return mode if mode in _VALID_MODES else "do"


class CronScheduler:
    """Store-backed scheduler. The daemon calls :meth:`due` each tick and runs
    each returned job, then calls :meth:`reschedule` to set the next fire time."""

    def __init__(self, store) -> None:
        self.store = store

    def create(self, goal: str, schedule: str, *, name: str = "",
               mode: str = "do", deliver: str = "local") -> dict:
        nr = compute_next_run(schedule)
        if nr.ts is None:
            return {"error": nr.reason}
        job_id = f"cron-{uuid.uuid4().hex[:10]}"
        self.store.add_cron_job(
            job_id, goal=goal, schedule=schedule, name=name,
            mode=normalize_mode(mode), deliver=deliver, next_run_ts=nr.ts)
        return self.store.get_cron_job(job_id)

    def list(self) -> list[dict]:
        return self.store.list_cron_jobs()

    def delete(self, job_id: str) -> bool:
        return self.store.delete_cron_job(job_id)

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        ok = self.store.set_cron_enabled(job_id, enabled)
        # Re-arm next_run when re-enabling so a paused job fires again.
        if ok and enabled:
            job = self.store.get_cron_job(job_id)
            if job and job.get("next_run_ts") is None:
                nr = compute_next_run(job["schedule"])
                self.store.set_cron_next_run(job_id, nr.ts)
        return ok

    def due(self, now: float | None = None) -> list[dict]:
        return self.store.due_cron_jobs(now)

    def reschedule(self, job_id: str, status: str, output: str = "") -> None:
        job = self.store.get_cron_job(job_id)
        if job is None:
            return
        nr = compute_next_run(job["schedule"])
        # An invalid schedule (None) stops the job from re-firing forever.
        self.store.update_cron_after_run(job_id, nr.ts, status, output)

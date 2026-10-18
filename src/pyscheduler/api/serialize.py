"""Turn runs, attempts, events, jobs and forecasts into plain JSON.

JSON only knows text, numbers, true/false, null, lists and objects, so
everything else is translated the same way everywhere:

- instants are ISO 8601 in UTC with a ``Z`` (``2026-10-19T18:30:00Z``);
- dates are ISO (``2026-10-19``) and times ``HH:MM`` (``HH:MM:SS`` if
  they have seconds);
- durations are seconds, in fields whose names end in ``_seconds``;
- enums are their values (criticality is its lower-case name);
- ``None`` is ``null``.

A run's ``duration_seconds`` is only present when its latest attempt is
known (the run detail endpoint), and is ``null`` until that attempt ends.
"""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from pynet.messaging import JsonValue

from pyscheduler.config import Config, JobAlerts, JobDef, Problem
from pyscheduler.config.redaction import redact_text, redactor
from pyscheduler.engine.model import Attempt, Event, Run
from pyscheduler.schedule.forecast import ForecastEntry
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
    Schedule,
)

__all__ = [
    "attempt_json",
    "date_text",
    "definition_json",
    "event_json",
    "forecast_json",
    "instant",
    "job_json",
    "problem_json",
    "redact",
    "run_json",
    "schedule_text",
]

type JsonObject = dict[str, JsonValue]

HIDDEN = "***"
"""What an environment variable's value is shown as (values may be secrets)."""

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR


def instant(value: datetime | None) -> str | None:
    """Return an aware instant as ISO 8601 UTC with ``Z``, or ``None``."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def date_text(value: date | None) -> str | None:
    """Return a date as ISO ``YYYY-MM-DD``, or ``None``."""
    return None if value is None else value.isoformat()


def _seconds(value: timedelta | None) -> float | None:
    """Return a duration in seconds, or ``None``."""
    return None if value is None else value.total_seconds()


def _clock(value: time) -> str:
    """Return a local time as ``HH:MM`` (``HH:MM:SS`` with seconds)."""
    return value.strftime("%H:%M:%S" if value.second else "%H:%M")


def duration_text(value: timedelta) -> str:
    """Return a duration the way configs write it, such as ``1h30m`` or ``45s``."""
    total = int(value.total_seconds())
    parts: list[str] = []
    for size, unit in ((_DAY, "d"), (_HOUR, "h"), (_MINUTE, "m"), (1, "s")):
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


def _filters(schedule: CronSchedule | AtSchedule | EverySchedule | BusinessDaySchedule) -> str:
    """Return the weekday and month filters of a schedule as text (or ``""``)."""
    out = ""
    if schedule.days is not None:
        out += " on " + ",".join(_WEEKDAYS[d] for d in sorted(schedule.days))
    if schedule.months is not None:
        out += " in months " + ",".join(str(m) for m in sorted(schedule.months))
    return out


def schedule_text(schedule: Schedule) -> str:
    """Return a short description of one schedule, such as ``cron 30 18 * * *``."""
    if isinstance(schedule, CronSchedule):
        return f"cron {schedule.cron.text.strip()}{_filters(schedule)}"
    if isinstance(schedule, AtSchedule):
        times = ", ".join(_clock(t) for t in schedule.times)
        return f"at {times}{_filters(schedule)}"
    if isinstance(schedule, EverySchedule):
        window = f"{_clock(schedule.start)}-{_clock(schedule.end)}"
        return f"every {duration_text(schedule.step)} between {window}{_filters(schedule)}"
    if isinstance(schedule, BusinessDaySchedule):
        times = ", ".join(_clock(t) for t in schedule.times)
        return f"business day {schedule.n} at {times}{_filters(schedule)}"
    return repr(schedule)


def schedules_text(job: JobDef) -> list[JsonValue]:
    """Return every schedule of ``job`` as text, with ``manual`` last if it is manual."""
    texts: list[JsonValue] = [schedule_text(s) for s in job.schedules]
    if job.manual:
        texts.append("manual")
    return texts


def run_json(run: Run, latest: Attempt | None = None, *, with_attempt: bool = False) -> JsonObject:
    """Return a run as JSON.

    With its ``latest`` attempt (or ``with_attempt``, for a run that may
    have none), add that attempt's ``started_at``, ``ended_at`` and
    ``duration_seconds`` (``null`` when unknown or still running).
    """
    out: JsonObject = {
        "run_id": run.run_id,
        "job": run.job,
        "run_date": date_text(run.run_date),
        "scheduled_at": instant(run.scheduled_at),
        "state": run.state.value,
        "reason": run.reason,
        "attempt": run.attempt,
        "rerun": run.rerun,
        "trigger": run.trigger.value,
        "config_version": run.config_version,
        "created_at": instant(run.created_at),
        "updated_at": instant(run.updated_at),
        "held_from": None if run.held_from is None else run.held_from.value,
    }
    if latest is not None or with_attempt:
        out["started_at"] = None if latest is None else instant(latest.started_at)
        out["ended_at"] = None if latest is None else instant(latest.ended_at)
        out["duration_seconds"] = None if latest is None else _attempt_seconds(latest)
    return out


def _attempt_seconds(attempt: Attempt) -> float | None:
    """Return how long an ended attempt took, or ``None`` while it runs."""
    if attempt.ended_at is None:
        return None
    return (attempt.ended_at - attempt.started_at).total_seconds()


def attempt_json(attempt: Attempt) -> JsonObject:
    """Return an attempt as JSON, with ``duration_seconds`` (``null`` while running)."""
    return {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt,
        "started_at": instant(attempt.started_at),
        "ended_at": instant(attempt.ended_at),
        "duration_seconds": _attempt_seconds(attempt),
        "exit_code": attempt.exit_code,
        "signal": attempt.signal,
        "outcome": None if attempt.outcome is None else attempt.outcome.value,
        "pid": attempt.pid,
        "stdout_path": attempt.stdout_path,
        "stderr_path": attempt.stderr_path,
        "status_path": attempt.status_path,
        "reason": attempt.reason,
    }


def event_json(event: Event) -> JsonObject:
    """Return an event as JSON; its ``detail`` text is parsed back into an object."""
    try:
        detail: JsonValue = json.loads(event.detail) if event.detail else {}
    except ValueError:
        detail = event.detail
    return {
        "at": instant(event.at),
        "actor": event.actor,
        "run_id": event.run_id,
        "action": event.action,
        "detail": detail,
    }


def problem_json(problem: Problem, expansions: Mapping[str, str] | None = None) -> JsonObject:
    """Return a config problem as JSON, hiding ``expansions`` values in its message.

    The loader already hides the values of the config it loaded; passing
    the running config's ``expansions`` hides those as well.
    """
    message = problem.message if expansions is None else redact_text(problem.message, expansions)
    return {
        "file": problem.file,
        "line": problem.line,
        "message": message,
        "job": problem.job,
        "severity": problem.severity,
    }


def forecast_json(entry: ForecastEntry) -> JsonObject:
    """Return one forecast entry as JSON, with its expected end when the duration is known."""
    end = (
        None if entry.expected_duration is None else entry.expected_start + entry.expected_duration
    )
    return {
        "job": entry.job,
        "run_date": date_text(entry.run_date),
        "expected_start": instant(entry.expected_start),
        "expected_duration_seconds": _seconds(entry.expected_duration),
        "expected_end": instant(end),
        "via": entry.via,
    }


def redact(value: JsonValue, expansions: Mapping[str, str]) -> JsonValue:
    """Replace every expanded ``${NAME}`` value in the strings of ``value`` with ``${NAME}``.

    Walks lists and objects (keys too), using ``config.redaction``: longer
    values first, the brace-doubled form of a value too, and values
    shorter than ``MIN_REDACTED`` left alone.
    """
    hide = redactor(expansions)

    def walk(item: JsonValue) -> JsonValue:
        """Redact one value, recursing into lists and objects."""
        if isinstance(item, str):
            return hide(item)
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            return {str(walk(key)): walk(child) for key, child in item.items()}
        return item

    return walk(value)


def _redacted(config: Config, out: JsonObject) -> JsonObject:
    """Return ``out`` with the config's expanded ``${NAME}`` values hidden."""
    return cast("JsonObject", redact(out, config.expansions))  # an object stays an object


def job_json(config: Config, job: JobDef, next_firing: datetime | None) -> JsonObject:
    """Return a job's summary: who owns it, how it is scheduled, and when it fires next.

    Expanded ``${NAME}`` values are hidden again (see ``redact``).
    """
    return _redacted(
        config,
        {
            "name": job.name,
            "description": job.description,
            "owner": job.owner,
            "tags": list(job.tags),
            "criticality": job.criticality.name.lower(),
            "enabled": job.enabled,
            "calendar": config.effective_calendars.get(job.name, job.calendar),
            "schedules": schedules_text(job),
            "depends_on": [dep.job for dep in job.depends_on],
            "next_firing": instant(next_firing),
        },
    )


def _must_start_by(job: JobDef) -> JsonValue:
    """Return ``must_start_by`` as a local time text or a number of seconds."""
    value = job.must_start_by
    if value is None:
        return None
    if isinstance(value, time):
        return _clock(value)
    return value.total_seconds()


def _alerts(alerts: JobAlerts) -> JsonObject:
    """Return a job's alert settings as sorted kind names and the notifier list."""
    on: list[JsonValue] = [*sorted(k.value for k in alerts.on)]
    off: list[JsonValue] = [*sorted(k.value for k in alerts.off)]
    notify: list[JsonValue] = list(alerts.notify)
    return {"on": on, "off": off, "notify": notify}


def definition_json(config: Config, job: JobDef, *, show_alerts: bool) -> JsonObject:
    """Return the whole job definition as JSON, with secrets hidden.

    Durations are seconds (``timeout_seconds``, ``retries.delay_seconds``,
    ...). Environment variable values are shown as ``***``; every other
    string has the config's expanded ``${NAME}`` values put back as
    ``${NAME}`` (see ``redact``). ``alerts`` (webhooks, addresses) is
    ``***`` unless ``show_alerts`` (the caller is an operator).
    """
    command: JsonValue = job.command if isinstance(job.command, str) else list(job.command)
    retries = job.retries
    return _redacted(
        config,
        {
            "name": job.name,
            "command": command,
            "cwd": job.cwd,
            "env": dict.fromkeys(sorted(job.env), HIDDEN),
            "schedules": schedules_text(job),
            "manual": job.manual,
            "calendar": job.calendar,
            "timezone": job.timezone.key,
            "run_date": job.run_date_rule.value,
            "depends_on": [
                {"job": d.job, "on": d.on.value, "run_date_offset": d.run_date_offset}
                for d in job.depends_on
            ],
            "retries": {
                "count": retries.count,
                "delay_seconds": retries.delay.total_seconds(),
                "backoff": retries.backoff,
                "max_delay_seconds": retries.max_delay.total_seconds(),
            },
            "timeout_seconds": _seconds(job.timeout),
            "criticality": job.criticality.name.lower(),
            "pools": dict(job.pools),
            "overlap": job.overlap.value,
            "catch_up": job.catch_up.value,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "min_runtime_seconds": _seconds(job.min_runtime),
            "max_runtime_seconds": _seconds(job.max_runtime),
            "must_start_by": _must_start_by(job),
            "alerts": _alerts(job.alerts) if show_alerts else HIDDEN,
            "agent": job.agent,
            "agent_tags": list(job.agent_tags),
            "tags": list(job.tags),
            "description": job.description,
            "owner": job.owner,
            "enabled": job.enabled,
            "source": {"file": job.source.file, "line": job.source.line},
        },
    )

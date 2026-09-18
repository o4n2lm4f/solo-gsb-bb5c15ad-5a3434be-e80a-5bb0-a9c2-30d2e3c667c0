"""VALARM expansion for VEVENT instances.

Each :class:`~app.expander.Instance` produced by the recurrence expander
carries the raw VALARM components of the event that defines it:

* a master event's VALARMs apply to **every** expanded instance of the
  recurring series (master alarms are never re-attached per occurrence in
  the ICS text — one definition applies to the whole series);
* a ``RECURRENCE-ID`` override carries **its own** VALARMs and replaces the
  master's alarms for that instance (an override with no VALARM has no
  reminders).

Only ``ACTION:DISPLAY`` alarms are supported. ``TRIGGER`` may be absolute
(``VALUE=DATE-TIME``) or a positive/negative duration relative to DTSTART
(the default) or DTEND (``RELATED=END``). ``REPEAT``/``DURATION`` (which
must appear together per RFC 5545) fan the alarm out into repeated
reminders spaced by DURATION. Only reminders whose trigger instant falls
inside the half-open query window ``[window_start, window_end)`` are
returned.

Anything invalid or unsupported raises :class:`AlarmError`, which the HTTP
layer maps to 422.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import (
    Instance,
    _parse_calendar,
    _to_utc,
    expand_parsed_calendar,
)

UTC = dt.timezone.utc

# Upper bound on reminders one alarm may fan out to (1 initial trigger plus
# REPEAT repeats), keeping an abusive REPEAT from flooding a response.
MAX_REMINDERS_PER_ALARM = 1_000


class AlarmError(Exception):
    """Invalid or unsupported VALARM input; surfaced to the client as 422."""


@dataclass(frozen=True)
class Reminder:
    """One concrete alarm trigger bound to a specific event instance."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    summary: str | None
    trigger: dt.datetime  # aware UTC
    description: str


@dataclass(frozen=True)
class _AlarmSpec:
    """A validated VALARM definition, independent of a concrete instance."""

    trigger: dt.datetime | dt.timedelta  # absolute datetime, or relative delta
    related_end: bool  # a relative trigger anchors to DTEND, not DTSTART
    description: str
    repeat: int  # additional repetitions after the first trigger
    spacing: dt.timedelta  # time between repeated triggers


def expand_alarms(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[Reminder]:
    """Expand every VALARM of every VEVENT instance in the UTC window.

    Instances come from :func:`app.expander.expand_calendar`; an instance
    whose event has no VALARMs contributes nothing. All VALARMs in the
    calendar are validated eagerly (like the expander validates every
    event), so an unsupported alarm yields 422 even if its trigger would
    fall outside the window.
    """
    calendar = _parse_calendar(ics_text)
    instances: list[Instance] = expand_parsed_calendar(
        calendar, window_start, window_end
    )

    specs: dict[int, _AlarmSpec] = {}
    for alarm in calendar.walk("VALARM"):
        specs[id(alarm)] = _parse_alarm(alarm)

    reminders: list[Reminder] = []
    for instance in instances:
        for alarm in instance.alarms:
            spec = specs[id(alarm)]
            reminders.extend(
                _materialize(spec, instance, window_start, window_end)
            )

    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    reminders.sort(
        key=lambda r: (
            r.trigger,
            r.uid,
            r.recurrence_id or no_rid,
            r.description,
        )
    )
    return reminders


# ---------------------------------------------------------------------------
# VALARM parsing
# ---------------------------------------------------------------------------


def _parse_alarm(alarm) -> _AlarmSpec:
    action = alarm.get("ACTION")
    if action is None:
        raise AlarmError("VALARM is missing the required ACTION property")
    action = str(action).upper()
    if action != "DISPLAY":
        raise AlarmError(
            f"unsupported VALARM ACTION {action!r}; only DISPLAY is supported"
        )

    description = alarm.get("DESCRIPTION")
    if description is None:
        raise AlarmError("DISPLAY VALARM is missing the required DESCRIPTION")

    trigger_prop = alarm.get("TRIGGER")
    if trigger_prop is None:
        raise AlarmError("VALARM is missing the required TRIGGER property")
    params = trigger_prop.params or {}

    value_type = str(params.get("VALUE", "")).upper()
    raw = trigger_prop.dt
    related = str(params.get("RELATED", "")).upper()

    if isinstance(raw, dt.timedelta):
        if value_type not in ("", "DURATION"):
            raise AlarmError(
                f"TRIGGER VALUE={value_type} is incompatible with a duration value"
            )
        if related not in ("", "START", "END"):
            raise AlarmError(f"unsupported TRIGGER RELATED={related}")
        trigger: dt.datetime | dt.timedelta = raw
        related_end = related == "END"
    elif isinstance(raw, dt.datetime):
        if value_type not in ("", "DATE-TIME"):
            raise AlarmError(
                f"TRIGGER VALUE={value_type} is incompatible with a DATE-TIME value"
            )
        if related:
            raise AlarmError(
                "TRIGGER RELATED is only valid for duration-valued triggers"
            )
        trigger = _absolute_trigger(raw, params)
        related_end = False
    else:
        raise AlarmError(
            "unsupported TRIGGER value type: only DURATION and DATE-TIME are supported"
        )

    repeat, spacing = _parse_repeat(alarm)
    return _AlarmSpec(
        trigger=trigger,
        related_end=related_end,
        description=str(description),
        repeat=repeat,
        spacing=spacing,
    )


def _absolute_trigger(value: dt.datetime, params) -> dt.datetime:
    """Normalize an absolute TRIGGER value to an aware UTC datetime."""
    if value.tzinfo is None:
        tzid = params.get("TZID")
        if tzid:
            tzname = str(tzid)
            try:
                tz = ZoneInfo(tzname)
            except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
                raise AlarmError(f"unknown timezone: {tzname!r}") from exc
            value = value.replace(tzinfo=tz)
        else:
            # Floating time, like the expander's convention, is assumed UTC.
            value = value.replace(tzinfo=UTC)
    return _to_utc(value)


def _parse_repeat(alarm) -> tuple[int, dt.timedelta]:
    """Validate the REPEAT/DURATION pair; both or neither must be present."""
    repeat_prop = alarm.get("REPEAT")
    duration_prop = alarm.get("DURATION")
    if repeat_prop is None and duration_prop is None:
        return 0, dt.timedelta(0)
    if repeat_prop is None:
        raise AlarmError("VALARM DURATION requires REPEAT (they must appear together)")
    if duration_prop is None:
        raise AlarmError("VALARM REPEAT requires DURATION (they must appear together)")
    try:
        repeat = int(repeat_prop)
    except (TypeError, ValueError) as exc:
        raise AlarmError(f"invalid VALARM REPEAT value: {repeat_prop!r}") from exc
    if repeat < 0:
        raise AlarmError("VALARM REPEAT must not be negative")
    spacing = duration_prop.dt
    if not isinstance(spacing, dt.timedelta):
        raise AlarmError("VALARM DURATION must be a duration value")
    if spacing <= dt.timedelta(0):
        raise AlarmError("VALARM DURATION must be a positive duration")
    if repeat + 1 > MAX_REMINDERS_PER_ALARM:
        raise AlarmError(
            f"VALARM fans out to more than {MAX_REMINDERS_PER_ALARM} reminders"
        )
    return repeat, spacing


# ---------------------------------------------------------------------------
# Per-instance materialization
# ---------------------------------------------------------------------------


def _materialize(
    spec: _AlarmSpec,
    instance: Instance,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> list[Reminder]:
    if isinstance(spec.trigger, dt.datetime):
        first = spec.trigger
    else:
        anchor = instance.end if spec.related_end else instance.start
        first = anchor + spec.trigger

    reminders = []
    current = first
    for _ in range(spec.repeat + 1):
        if window_start <= current < window_end:
            reminders.append(
                Reminder(
                    uid=instance.uid,
                    recurrence_id=instance.recurrence_id,
                    summary=instance.summary,
                    trigger=current,
                    description=spec.description,
                )
            )
        current += spec.spacing
    return reminders

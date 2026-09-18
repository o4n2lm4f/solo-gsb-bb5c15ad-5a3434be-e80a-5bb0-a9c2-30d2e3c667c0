"""Structural validation of iCalendar (RFC 5545) VEVENT data.

Unlike the expansion endpoints, validation never raises for bad calendar
input: unparsable text and malformed events are reported as structured
issues (see the ``*_CODE`` constants) and the HTTP response stays ``200``.

Issues are examined in the order the ``VEVENT`` components appear in the
source text; one event may accumulate several issues. Cross-event checks
(multiple master events per UID, overrides without a master) run after the
per-event checks. The final issue list is sorted by ``event_index`` (nulls
first) and then by stable ``code``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from icalendar import Calendar

UTC = dt.timezone.utc

# ---------------------------------------------------------------------------
# Stable issue codes (part of the HTTP contract)
# ---------------------------------------------------------------------------

PARSE_ERROR_CODE = "parse_error"
MISSING_UID_CODE = "missing_uid"
MISSING_DTSTART_CODE = "missing_dtstart"
DTEND_AND_DURATION_CODE = "dtend_and_duration"
END_BEFORE_START_CODE = "end_before_start"
DUPLICATE_MASTER_CODE = "duplicate_master_event"
ORPHAN_OVERRIDE_CODE = "orphan_override"


@dataclass(frozen=True)
class Issue:
    """One diagnostic. ``event_index`` is the 0-based position of the
    offending VEVENT in source order, or None when no event can be located;
    ``uid`` is None for events without a usable UID."""

    code: str
    message: str
    event_index: int | None = None
    uid: str | None = None


@dataclass
class ValidationReport:
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def error_count(self) -> int:
        return len(self.issues)


def validate_ics(ics_text: str) -> ValidationReport:
    """Validate raw iCalendar text and return every problem found.

    A parsing failure short-circuits to a single ``parse_error`` issue;
    nothing else can be located without a parsed component tree.
    """
    try:
        root = Calendar.from_ical(ics_text)
        walk = getattr(root, "walk", None)
        if walk is None:
            raise TypeError("parsed object contains no iCalendar components")
        events = list(walk("VEVENT"))
    except Exception as exc:
        return ValidationReport(
            issues=[
                Issue(
                    code=PARSE_ERROR_CODE,
                    message=f"iCalendar text could not be parsed: {exc}",
                )
            ]
        )

    issues: list[Issue] = []

    # Per-event checks, in source order. Each check is independent so that a
    # single event can accumulate more than one issue.
    for index, event in enumerate(events):
        uid = _event_uid(event)
        if uid is None:
            issues.append(
                Issue(
                    code=MISSING_UID_CODE,
                    message="VEVENT is missing the required UID property",
                    event_index=index,
                )
            )

        start = _event_start(event)
        if start is None:
            prop = event.get("DTSTART")
            if prop is None:
                message = "VEVENT is missing the required DTSTART property"
            else:
                message = (
                    "VEVENT has a DTSTART that is not a valid DATE or "
                    "DATE-TIME value"
                )
            issues.append(
                Issue(
                    code=MISSING_DTSTART_CODE,
                    message=message,
                    event_index=index,
                    uid=uid,
                )
            )

        # RFC 5545: DTEND and DURATION on the same event are forbidden.
        if "DTEND" in event and "DURATION" in event:
            issues.append(
                Issue(
                    code=DTEND_AND_DURATION_CODE,
                    message="VEVENT must not declare both DTEND and DURATION; "
                    "the properties are mutually exclusive",
                    event_index=index,
                    uid=uid,
                )
            )

        # Ordering only applies to an explicitly declared end. An event with
        # just DTSTART has no declared end and is left alone.
        if start is not None:
            end = _declared_end(event, start)
            if end is not None and end <= start:
                issues.append(
                    Issue(
                        code=END_BEFORE_START_CODE,
                        message=(
                            f"VEVENT end {_format_dt(end)} is not later than "
                            f"its start {_format_dt(start)}"
                        ),
                        event_index=index,
                        uid=uid,
                    )
                )

    # Cross-event checks keyed on UID + presence of RECURRENCE-ID.
    master_indices: dict[str, list[int]] = {}
    overrides: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        uid = _event_uid(event)
        if uid is None:
            # Events without a usable UID cannot participate in grouping.
            continue
        if "RECURRENCE-ID" in event:
            overrides.append((index, uid))
        else:
            master_indices.setdefault(uid, []).append(index)

    for uid, indices in master_indices.items():
        if len(indices) > 1:
            for index in indices:
                issues.append(
                    Issue(
                        code=DUPLICATE_MASTER_CODE,
                        message=(
                            f"UID {uid!r} has {len(indices)} VEVENTs without "
                            "RECURRENCE-ID; a recurring series may have only "
                            "one master event"
                        ),
                        event_index=index,
                        uid=uid,
                    )
                )

    for index, uid in overrides:
        if uid not in master_indices:
            issues.append(
                Issue(
                    code=ORPHAN_OVERRIDE_CODE,
                    message=(
                        "VEVENT has a RECURRENCE-ID override but no master "
                        f"event without RECURRENCE-ID shares UID {uid!r}"
                    ),
                    event_index=index,
                    uid=uid,
                )
            )

    # Null event_index sorts before any integer; code is the secondary key.
    issues.sort(
        key=lambda issue: (
            issue.event_index is not None,
            issue.event_index if issue.event_index is not None else 0,
            issue.code,
        )
    )
    return ValidationReport(issues=issues)


# ---------------------------------------------------------------------------
# Property extraction (defensive: a broken property must never crash a check)
# ---------------------------------------------------------------------------


def _event_uid(event) -> str | None:
    prop = event.get("UID")
    if prop is None:
        return None
    try:
        return str(prop)
    except Exception:
        return None


def _event_start(event) -> dt.datetime | None:
    """Start as an aware UTC datetime, or None when absent/unusable."""
    prop = event.get("DTSTART")
    if prop is None:
        return None
    try:
        value = prop.dt
    except Exception:
        return None
    return _coerce_datetime(value)


def _declared_end(event, start: dt.datetime) -> dt.datetime | None:
    """The end explicitly given by DTEND or DURATION, or None when neither
    is declared (or the declared value is unusable)."""
    if "DTEND" in event:
        try:
            value = event["DTEND"].dt
        except Exception:
            return None
        return _coerce_datetime(value)
    if "DURATION" in event:
        try:
            duration = event["DURATION"].dt
        except Exception:
            return None
        if isinstance(duration, dt.timedelta):
            return start + duration
        return None
    return None


def _coerce_datetime(value) -> dt.datetime | None:
    """Normalize DATE/DATE-TIME values to aware UTC; floating = UTC."""
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time.min)
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_dt(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

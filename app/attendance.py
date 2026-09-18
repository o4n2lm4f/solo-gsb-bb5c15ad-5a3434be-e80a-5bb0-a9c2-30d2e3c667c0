"""ATTENDEE participation-status expansion for VEVENT instances.

Each :class:`~app.expander.Instance` produced by the recurrence expander
carries the raw ATTENDEE properties in effect for it:

* recurring instances **inherit** the master event's ATTENDEE list;
* a ``RECURRENCE-ID`` override that declares **any** ATTENDEE replaces the
  whole list for that instance, while an override without ATTENDEE keeps
  inheriting the master's (mirroring SUMMARY/DURATION inheritance);
* a cancelled occurrence (master ``STATUS:CANCELLED`` or a cancelled
  override) produces no instance and therefore no attendance.

Addresses are normalized per the request contract: ``mailto:`` addresses
lose their scheme prefix and are lowercased (``mailto:Alice@Example.com``
→ ``alice@example.com``); every other URI is kept verbatim. Missing
``PARTSTAT`` defaults to ``NEEDS-ACTION`` and missing ``ROLE`` to
``REQ-PARTICIPANT``. The same address appearing twice within one effective
list is accepted only when its parameters agree; conflicting PARTSTAT or
ROLE raises :class:`AttendanceError`, which the HTTP layer maps to 422.

Attendees are stably sorted by normalized address.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import Instance, _parse_calendar, _props, expand_parsed_calendar

UTC = dt.timezone.utc

DEFAULT_PARTSTAT = "NEEDS-ACTION"
DEFAULT_ROLE = "REQ-PARTICIPANT"
_MAILTO_PREFIX = "mailto:"


class AttendanceError(Exception):
    """Invalid ATTENDEE input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class Attendee:
    """One normalized attendee within an event instance."""

    address: str
    partstat: str  # defaults to NEEDS-ACTION
    role: str  # defaults to REQ-PARTICIPANT


@dataclass(frozen=True)
class InstanceAttendance:
    """The attendee responses of one concrete event instance."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    attendees: list[Attendee]


def attendance_for_calendar(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[InstanceAttendance]:
    """Return the attendee list of every VEVENT instance in the UTC window.

    Instances come from :func:`app.expander.expand_calendar` and keep its
    (start, uid, recurrence_id) order. Every VEVENT's own ATTENDEE list is
    validated eagerly (like alarms), which also covers every effective
    per-instance list: an effective list is always either a master's own
    list or an override's own list — inheritance never merges two lists.
    """
    calendar = _parse_calendar(ics_text)

    parsed_by_key: dict[tuple[int, ...], list[Attendee]] = {}
    for event in calendar.walk("VEVENT"):
        props = _props(event, "ATTENDEE")
        parsed_by_key[tuple(id(prop) for prop in props)] = _parse_attendees(props)

    instances: list[Instance] = expand_parsed_calendar(
        calendar, window_start, window_end
    )

    results: list[InstanceAttendance] = []
    for instance in instances:
        key = tuple(id(prop) for prop in instance.attendees)
        attendees = parsed_by_key.get(key)
        if attendees is None:  # defensive: an empty list shared by no event
            attendees = _parse_attendees(instance.attendees)
        results.append(
            InstanceAttendance(
                uid=instance.uid,
                recurrence_id=instance.recurrence_id,
                start=instance.start,
                end=instance.end,
                attendees=attendees,
            )
        )
    return results


def partstat_counts(attendees: list[Attendee]) -> dict[str, int]:
    """Count attendees per PARTSTAT, in first-seen address order."""
    counts: dict[str, int] = {}
    for attendee in attendees:
        counts[attendee.partstat] = counts.get(attendee.partstat, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# ATTENDEE parsing
# ---------------------------------------------------------------------------


def _parse_attendees(props) -> list[Attendee]:
    """Normalize and de-duplicate one event's own ATTENDEE property list."""
    attendees: list[Attendee] = []
    seen: dict[str, tuple[str, str]] = {}
    for prop in props:
        address = _normalize_address(str(prop))
        partstat = str(prop.params.get("PARTSTAT", DEFAULT_PARTSTAT)).upper()
        role = str(prop.params.get("ROLE", DEFAULT_ROLE)).upper()
        params = (partstat, role)
        previous = seen.get(address)
        if previous is not None and previous != params:
            raise AttendanceError(
                f"duplicate ATTENDEE {address!r} has conflicting parameters "
                f"(PARTSTAT/ROLE {previous[0]}/{previous[1]} vs "
                f"{partstat}/{role})"
            )
        if previous is None:
            seen[address] = params
            attendees.append(Attendee(address=address, partstat=partstat, role=role))
    attendees.sort(key=lambda a: a.address)
    return attendees


def _normalize_address(raw: str) -> str:
    """Strip/lowercase ``mailto:`` addresses; preserve every other URI."""
    if raw[: len(_MAILTO_PREFIX)].lower() == _MAILTO_PREFIX:
        return raw[len(_MAILTO_PREFIX) :].lower()
    return raw

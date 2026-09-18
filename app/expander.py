"""Core iCalendar recurrence expansion logic.

Expands VEVENT recurrence sets (DTSTART + RRULE + RDATE - EXDATE) into
concrete instances, applies per-instance overrides keyed by
UID + RECURRENCE-ID (reschedule or STATUS:CANCELLED), and normalizes
everything to UTC. Timezones are interpreted as IANA names via zoneinfo,
so DST transitions are honored automatically.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar

UTC = dt.timezone.utc

# Safety bound: a single UID group may not expand to more candidate
# occurrences than this inside the (duration-padded) query window.
MAX_OCCURRENCES_PER_UID = 10_000


class ExpansionError(Exception):
    """Invalid calendar input; surfaced to the client as HTTP 422."""


@dataclass
class Instance:
    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    summary: str | None
    status: str
    # True when the effective DTSTART is DATE-valued (an all-day event).
    # DATE values carry no timezone: the expander keeps their authored
    # calendar date as a floating midnight (normalized to UTC), so callers
    # rendering local agendas must recover that date instead of projecting
    # it through a display timezone.
    all_day: bool = False
    # Raw icalendar VALARM components carried by the event that produced
    # this instance: a master's alarms apply to every recurring instance,
    # while a RECURRENCE-ID override uses only its own (possibly empty).
    alarms: list = field(default_factory=list)
    # Raw icalendar ATTENDEE properties in effect for this instance. A
    # recurring instance inherits the master's attendees; a RECURRENCE-ID
    # override that declares any ATTENDEE replaces the whole list (an
    # override without ATTENDEE keeps inheriting the master's).
    attendees: list = field(default_factory=list)
    # Effective TRANSP value ("OPAQUE" blocks time, "TRANSPARENT" does
    # not). A recurring instance inherits the master event's TRANSP; a
    # RECURRENCE-ID override that declares TRANSP explicitly replaces the
    # inherited value for its instance.
    transp: str = "OPAQUE"


def expand_calendar(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[Instance]:
    """Expand all VEVENTs in ``ics_text`` and return instances overlapping
    the UTC window [window_start, window_end), sorted by (start, uid, rid)."""
    calendar = _parse_calendar(ics_text)
    return expand_parsed_calendar(calendar, window_start, window_end)


def expand_parsed_calendar(
    calendar: Calendar, window_start: dt.datetime, window_end: dt.datetime
) -> list[Instance]:
    """Like :func:`expand_calendar` but for an already parsed calendar."""
    groups: dict[str, list] = {}
    for component in calendar.walk("VEVENT"):
        uid = component.get("UID")
        if uid is None:
            raise ExpansionError("VEVENT is missing the required UID property")
        groups.setdefault(str(uid), []).append(component)

    instances: list[Instance] = []
    for uid, events in groups.items():
        instances.extend(_expand_group(uid, events, window_start, window_end))

    instances = [
        i for i in instances if _overlaps(i.start, i.end, window_start, window_end)
    ]
    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    instances.sort(key=lambda i: (i.start, i.uid, i.recurrence_id or no_rid))
    return instances


# ---------------------------------------------------------------------------
# UID groups: master + overrides
# ---------------------------------------------------------------------------

def _expand_group(
    uid: str, events: list, window_start: dt.datetime, window_end: dt.datetime
) -> list[Instance]:
    for event in events:
        _validate_tzids(event)

    masters = [e for e in events if "RECURRENCE-ID" not in e]
    overrides = [e for e in events if "RECURRENCE-ID" in e]

    instances: dict[dt.datetime, Instance] = {}
    master_summary: str | None = None
    master_duration: dt.timedelta | None = None
    master_attendees: list = []
    master_transp = "OPAQUE"

    if masters:
        master = masters[0]
        master_summary = _text(master, "SUMMARY")
        master_transp = _transp(master)
        m_start, m_end, m_all_day = _event_times(master)
        master_duration = m_end - m_start
        master_alarms = [c for c in master.subcomponents if c.name == "VALARM"]
        master_attendees = _props(master, "ATTENDEE")
        if _status(master) != "CANCELLED":
            occurrences, recurring = _expand_master(
                master, m_start, master_duration, window_start, window_end
            )
            for occ in occurrences:
                start = _to_utc(occ)
                instances[start] = Instance(
                    uid=uid,
                    recurrence_id=start if recurring else None,
                    start=start,
                    end=start + master_duration,
                    summary=master_summary,
                    status=_status(master),
                    all_day=m_all_day,
                    alarms=master_alarms,
                    attendees=master_attendees,
                    transp=master_transp,
                )

    for override in overrides:
        rid = _to_utc(_recurrence_id(override, masters[0] if masters else None))
        # An override replaces the original occurrence: the original time
        # slot must never survive next to (or instead of) the override.
        instances.pop(rid, None)
        status = _status(override)
        if status == "CANCELLED":
            continue
        start, end, all_day = _event_times(override, default_duration=master_duration)
        summary = _text(override, "SUMMARY")
        override_attendees = _props(override, "ATTENDEE")
        instances[rid] = Instance(
            uid=uid,
            recurrence_id=rid,
            start=_to_utc(start),
            end=_to_utc(end),
            summary=summary if summary is not None else master_summary,
            status=status,
            all_day=all_day,
            alarms=[c for c in override.subcomponents if c.name == "VALARM"],
            # An override declaring any ATTENDEE replaces the master's list
            # wholesale; one without keeps inheriting the master's.
            attendees=override_attendees if override_attendees else master_attendees,
            # Same rule for TRANSP: an explicit declaration replaces the
            # inherited value, an absent one keeps the master's.
            transp=_transp(override) if "TRANSP" in override else master_transp,
        )

    return list(instances.values())


def _expand_master(
    event,
    start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple[list[dt.datetime], bool]:
    """Expand DTSTART/RRULE/RDATE/EXDATE. Returns (occurrences, recurring).

    Occurrence starts are generated in the window padded backwards by the
    event duration, so events starting before the window but overlapping
    it are still produced. Final window filtering happens in the caller.
    """
    rrule_props = _props(event, "RRULE")
    rdate_props = _props(event, "RDATE")
    exdate_props = _props(event, "EXDATE")
    recurring = bool(rrule_props or rdate_props)
    if not recurring:
        return [start], False

    rset = rruleset()
    for prop in rrule_props:
        rule_text = prop.to_ical().decode()
        try:
            rset.rrule(rrulestr(rule_text, dtstart=start))
        except ExpansionError:
            raise
        except Exception as exc:
            raise ExpansionError(f"invalid RRULE {rule_text!r}: {exc}") from exc
    for prop in rdate_props:
        for value in _datetimes(prop, start):
            rset.rdate(value)
    # Per RFC 5545 the DTSTART itself always belongs to the recurrence set
    # (an rruleset with only RDATEs would otherwise drop it). Duplicates
    # collapse later via the per-UID dict keyed on occurrence start.
    rset.rdate(start)
    for prop in exdate_props:
        for value in _datetimes(prop, start):
            rset.exdate(value)

    if start.tzinfo is None:  # floating time: interpreted as UTC
        after = (window_start - duration).replace(tzinfo=None)
        before = window_end.replace(tzinfo=None)
    else:
        after = (window_start - duration).astimezone(start.tzinfo)
        before = window_end.astimezone(start.tzinfo)

    try:
        occurrences: list[dt.datetime] = []
        for occ in rset.between(after, before, inc=True):
            occurrences.append(occ)
            if len(occurrences) > MAX_OCCURRENCES_PER_UID:
                raise ExpansionError(
                    f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
                    "occurrences within the query window"
                )
    except ExpansionError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc
    return occurrences, True


# ---------------------------------------------------------------------------
# Property helpers
# ---------------------------------------------------------------------------

def _parse_calendar(ics_text: str) -> Calendar:
    try:
        return Calendar.from_ical(ics_text)
    except Exception as exc:
        raise ExpansionError(f"invalid iCalendar input: {exc}") from exc


def _props(event, name: str) -> list:
    """All properties named ``name`` (icalendar keeps repeated properties)."""
    out = []
    for key, value in event.property_items():
        if key == name:
            out.extend(value if isinstance(value, list) else [value])
    return out


def _validate_tzids(event) -> None:
    """Every TZID must be a known IANA timezone, otherwise 422."""
    for _, value in event.property_items():
        for prop in (value if isinstance(value, list) else [value]):
            params = getattr(prop, "params", None)
            if params and "TZID" in params:
                tzid = str(params["TZID"])
                try:
                    ZoneInfo(tzid)
                except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
                    raise ExpansionError(f"unknown timezone: {tzid!r}") from exc


def _event_times(
    event, default_duration: dt.timedelta | None = None
) -> tuple[dt.datetime, dt.datetime, bool]:
    start, start_is_date = _start_time(event)
    if "DTEND" in event:
        value = event["DTEND"].dt
        if isinstance(value, dt.datetime):
            end = value
        elif isinstance(value, dt.date):
            end = dt.datetime.combine(value, dt.time.min, tzinfo=start.tzinfo)
        else:
            raise ExpansionError("unsupported DTEND value type")
    elif "DURATION" in event:
        end = start + event["DURATION"].dt
    elif default_duration is not None:
        # Override without DTEND/DURATION: keep the master's duration.
        end = start + default_duration
    elif start_is_date:
        # RFC 5545: a DATE-valued DTSTART without DTEND lasts the whole day.
        end = start + dt.timedelta(days=1)
    else:
        end = start
    return start, end, start_is_date


def _start_time(event) -> tuple[dt.datetime, bool]:
    prop = event.get("DTSTART")
    if prop is None:
        raise ExpansionError(
            f"VEVENT (UID {event.get('UID', '?')}) is missing DTSTART"
        )
    value = prop.dt
    if isinstance(value, dt.datetime):
        return value, False
    if isinstance(value, dt.date):
        tzid = prop.params.get("TZID")
        tz = ZoneInfo(str(tzid)) if tzid else None
        return dt.datetime.combine(value, dt.time.min, tzinfo=tz), True
    raise ExpansionError("unsupported DTSTART value type")


def _recurrence_id(event, master) -> dt.datetime:
    prop = event["RECURRENCE-ID"]
    rng = prop.params.get("RANGE")
    if rng is not None and str(rng).upper() not in ("THISANDPRIOR",):
        raise ExpansionError(f"RECURRENCE-ID RANGE={rng} is not supported")
    value = prop.dt
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        tz = None
        if master is not None:
            tz = _event_times(master)[0].tzinfo
        return dt.datetime.combine(value, dt.time.min, tzinfo=tz)
    raise ExpansionError("unsupported RECURRENCE-ID value type")


def _datetimes(prop, ref_start: dt.datetime) -> list[dt.datetime]:
    """Normalize an RDATE/EXDATE property to a list of datetimes."""
    out = []
    for item in prop.dts:
        value = item.dt
        if isinstance(value, dt.datetime):
            out.append(value)
        elif isinstance(value, dt.date):
            out.append(
                dt.datetime.combine(value, dt.time.min, tzinfo=ref_start.tzinfo)
            )
        else:
            raise ExpansionError(
                f"unsupported {prop.name} value type: {type(value).__name__}"
            )
    return out


def _status(event) -> str:
    return str(event.get("STATUS", "CONFIRMED")).upper()


def _transp(event) -> str:
    return str(event.get("TRANSP", "OPAQUE")).upper()


def _text(event, name: str) -> str | None:
    value = event.get(name)
    return str(value) if value is not None else None


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # floating time: interpreted as UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _overlaps(
    start: dt.datetime, end: dt.datetime, window_start: dt.datetime, window_end: dt.datetime
) -> bool:
    if end > start:
        return start < window_end and end > window_start
    # Zero-duration event: in the window iff its instant is.
    return window_start <= start < window_end

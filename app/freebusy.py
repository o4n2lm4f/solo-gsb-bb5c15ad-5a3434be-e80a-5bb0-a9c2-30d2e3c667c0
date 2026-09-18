"""VFREEBUSY export for named calendars.

Each calendar (``calendar_id`` + ICS text + IANA timezone) is expanded with
the same recurrence engine as ``POST /expand`` and reduced to its busy
periods inside the UTC query window:

* ``STATUS:CANCELLED`` instances never occupy time;
* ``TRANSP:TRANSPARENT`` instances do not block time either. A recurring
  instance inherits the master event's ``TRANSP``; a ``RECURRENCE-ID``
  override that declares ``TRANSP`` explicitly replaces the inherited value
  for its instance (an override without ``TRANSP`` keeps inheriting);
* a ``DATE`` all-day instance occupies its authored calendar dates as
  *local* full days in the calendar's own timezone, converted to UTC day
  by day with ``zoneinfo`` — so a spring-forward day occupies 23 UTC hours
  and a fall-back day 25;
* every busy interval is clipped to the half-open window
  ``[window_start, window_end)`` and overlapping or touching intervals are
  merged (back-to-back meetings leave no free instant between them).

The merged periods are rendered as one ``VFREEBUSY`` component per calendar
inside a complete ``VCALENDAR`` text, with ``FREEBUSY`` lines sorted by
start.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import ExpansionError, Instance, expand_calendar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)

PRODID = "-//freebusy-export//EN"

# TRANSP values defined by RFC 5545; anything else is rejected with 422.
TRANSP_VALUES = ("OPAQUE", "TRANSPARENT")


class FreeBusyError(Exception):
    """Invalid free/busy input; surfaced to the client as HTTP 422."""


def busy_periods(
    ics_text: str,
    tzname: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Merged, window-clipped busy intervals of one calendar, in UTC."""
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise FreeBusyError(f"unknown timezone: {tzname!r}") from exc

    # The expander filters instances by their UTC-normalized times, but an
    # all-day event's *local* day span can reach up to ~14h outside that
    # floating-midnight span (the largest UTC offset). Expand against a
    # padded window so such boundary events are not missed; everything is
    # clipped back to the true window below.
    pad = dt.timedelta(days=2)
    try:
        instances = expand_calendar(
            ics_text, window_start - pad, window_end + pad
        )
    except ExpansionError as exc:
        raise FreeBusyError(str(exc)) from exc

    clipped: list[tuple[dt.datetime, dt.datetime]] = []
    for inst in instances:
        if inst.status == "CANCELLED":
            continue
        if inst.transp not in TRANSP_VALUES:
            raise FreeBusyError(
                f"unsupported TRANSP value: {inst.transp!r} "
                "(expected OPAQUE or TRANSPARENT)"
            )
        if inst.transp == "TRANSPARENT":
            continue
        if inst.all_day:
            start, end = _all_day_span_utc(inst, tz)
        else:
            start, end = inst.start, inst.end
        start = max(start, window_start)
        end = min(end, window_end)
        if end > start:
            clipped.append((start, end))
    return _merge_intervals(clipped)


def render_vfreebusy(
    uid: str,
    periods: list[tuple[dt.datetime, dt.datetime]],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> str:
    """Render one calendar's busy periods as a complete VCALENDAR text
    holding a single VFREEBUSY component.

    ``periods`` must already be sorted and merged; the ``FREEBUSY`` lines
    follow that order, so the output is stable for a stable input.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VFREEBUSY",
        f"UID:{_escape_text(uid)}",
        f"DTSTAMP:{_stamp(dt.datetime.now(UTC))}",
        f"DTSTART:{_stamp(window_start)}",
        f"DTEND:{_stamp(window_end)}",
    ]
    for start, end in periods:
        lines.append(f"FREEBUSY:{_stamp(start)}/{_stamp(end)}")
    lines.append("END:VFREEBUSY")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------


def _all_day_span_utc(
    inst: Instance, tz: ZoneInfo
) -> tuple[dt.datetime, dt.datetime]:
    """UTC span of an all-day instance's authored dates as local days.

    The expander stores DATE values as floating midnights normalized to
    UTC, so ``start.date()``/``end.date()`` recover the authored calendar
    dates. Converting the local midnights of the first and last+1 date
    keeps the span correct across DST transitions (a spring-forward day
    occupies 23 UTC hours, a fall-back day 25).
    """
    first_date = inst.start.astimezone(UTC).date()
    last_date = inst.end.astimezone(UTC).date() - DAY
    start = dt.datetime.combine(first_date, dt.time.min, tzinfo=tz).astimezone(UTC)
    end = dt.datetime.combine(last_date + DAY, dt.time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def _merge_intervals(
    intervals: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Sort and merge overlapping or touching half-open intervals.

    Half-open intervals [start, end) carry no free instant between
    ``[a, b)`` and ``[b, c)``, so adjacent intervals merge as well.
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[dt.datetime, dt.datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    """RFC 5545 TEXT escaping for property values."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )

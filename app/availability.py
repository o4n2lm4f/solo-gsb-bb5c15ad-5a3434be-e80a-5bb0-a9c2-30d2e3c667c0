"""Common free-time computation.

Given multiple named calendars, a UTC query window, an IANA timezone and
per-weekday local working periods, build the working-period timeline in UTC
(day by day, so DST transitions are handled by zoneinfo), clip every
non-CANCELLED, positive-duration busy instance onto it, merge all busy
intervals (adjacent included), and return the complement intervals that are
at least ``minimum_minutes`` long for *every* calendar at once.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

# Monday first (dt.date.weekday()); the request uses the same indices.
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class AvailabilityError(Exception):
    """Invalid availability input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class LocalRange:
    start: dt.time
    end: dt.time


def build_working_periods(
    window_start: dt.datetime,
    window_end: dt.datetime,
    tzname: str,
    ranges_by_weekday: dict[int, list[LocalRange]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Materialize the working periods as UTC intervals covering the window.

    Ranges are generated for each local date individually and localized to
    UTC, so a fixed local time (e.g. 09:00) shifts correctly at DST
    transitions. The result is clipped to ``[window_start, window_end)`` and
    merged with touching intervals collapsed.
    """
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise AvailabilityError(f"unknown timezone: {tzname!r}") from exc

    first_date = window_start.astimezone(tz).date()
    last_date = window_end.astimezone(tz).date()

    periods: list[tuple[dt.datetime, dt.datetime]] = []
    day = dt.timedelta(days=1)
    date = first_date
    while date <= last_date:
        for rng in ranges_by_weekday.get(date.weekday(), []):
            start = dt.datetime.combine(date, rng.start, tzinfo=tz).astimezone(UTC)
            end = dt.datetime.combine(date, rng.end, tzinfo=tz).astimezone(UTC)
            start = max(start, window_start)
            end = min(end, window_end)
            if end > start:
                periods.append((start, end))
        date += day

    return _merge_intervals(periods)


def busy_intervals(
    calendars: dict[str, str],
    window_start: dt.datetime,
    window_end: dt.datetime,
    working: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Expand every calendar and clip its busy instances onto working time.

    STATUS:CANCELLED instances and non-positive-duration instances are
    ignored. The intervals returned are merged, with touching intervals
    collapsed (two back-to-back meetings carry no free instant between
    them).
    """
    clipped: list[tuple[dt.datetime, dt.datetime]] = []
    for ics in calendars.values():
        try:
            instances = expand_calendar(ics, window_start, window_end)
        except ExpansionError as exc:
            raise AvailabilityError(str(exc)) from exc
        for inst in instances:
            if inst.status == "CANCELLED" or inst.end <= inst.start:
                continue
            clipped.extend(_clip_to_intervals(inst.start, inst.end, working))
    return _merge_intervals(clipped)


def common_free_slots(
    busy: list[tuple[dt.datetime, dt.datetime]],
    working: list[tuple[dt.datetime, dt.datetime]],
    minimum: dt.timedelta,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Complement of merged busy intervals within merged working periods,
    keeping only intervals of at least ``minimum`` length."""
    slots: list[tuple[dt.datetime, dt.datetime]] = []
    for w_start, w_end in working:
        cursor = w_start
        for b_start, b_end in busy:
            if b_end <= cursor:
                continue
            if b_start >= w_end:
                break
            if b_start > cursor:
                slots.append((cursor, b_start))
            if b_end > cursor:
                cursor = b_end
        if cursor < w_end:
            slots.append((cursor, w_end))
    return [(s, e) for s, e in slots if e - s >= minimum]


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------

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


def _clip_to_intervals(
    start: dt.datetime,
    end: dt.datetime,
    intervals: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Intersect one half-open [start, end) interval with a sorted,
    non-overlapping list of half-open intervals."""
    out: list[tuple[dt.datetime, dt.datetime]] = []
    for i_start, i_end in intervals:
        if i_end <= start:
            continue
        if i_start >= end:
            break
        c_start = start if start > i_start else i_start
        c_end = end if end < i_end else i_end
        if c_end > c_start:
            out.append((c_start, c_end))
    return out

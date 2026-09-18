"""Compress a set of equal-length UTC instances into a compact iCalendar
recurrence description.

The instances are covered by several candidate descriptions and the
shortest complete ICS (in UTF-8 bytes) wins:

* one candidate per ``FREQ=DAILY``/``FREQ=WEEKLY`` with ``INTERVAL`` 1..4.
  The earliest start becomes ``DTSTART`` and the latest start ``UNTIL``;
  grid points absent from the input are written as ``EXDATE`` and input
  starts the grid does not generate as ``RDATE``;
* a fallback carrying every instance after the first purely as ``RDATE``
  (``DTSTART`` + ``RDATE`` only).

Every candidate is verified by feeding it back through the same expansion
engine used by ``POST /expand``: a candidate is only eligible when it
reproduces the exact input set of starts and ends.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from icalendar import Calendar, Event

from .expander import expand_calendar

UTC = dt.timezone.utc

FREQUENCIES = ("DAILY", "WEEKLY")
INTERVALS = (1, 2, 3, 4)
FALLBACK_STRATEGY = "RDATE"

PRODID = "-//compress-schedule//EN"


class CompressionError(Exception):
    """Invalid instance set; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class CompressedSchedule:
    strategy: str
    exception_count: int
    ics: str  # complete VCALENDAR text


@dataclass(frozen=True)
class _Candidate:
    strategy: str
    exception_count: int
    ics: bytes
    is_rrule: bool
    freq_rank: int  # 0 = DAILY, 1 = WEEKLY, 2 = RDATE fallback
    interval: int

    def sort_key(self) -> tuple:
        # Fewest bytes first; ties: fewer exceptions, then an RRULE plan,
        # then DAILY over WEEKLY, then the smaller INTERVAL.
        return (
            len(self.ics),
            self.exception_count,
            0 if self.is_rrule else 1,
            self.freq_rank,
            self.interval,
        )


def compress_instances(
    uid: str, instances: list[tuple[dt.datetime, dt.datetime]]
) -> CompressedSchedule:
    """Build the smallest recurrence description for ``instances``."""
    if not isinstance(uid, str) or not uid:
        raise CompressionError("uid must be a non-empty string")
    if not instances:
        raise CompressionError(
            "instances must be a non-empty list of {start, end} UTC datetimes"
        )

    normalized = sorted(
        ((_to_utc(start), _to_utc(end)) for start, end in instances),
        key=lambda pair: pair[0],
    )
    _validate(normalized)

    starts = [start for start, _ in normalized]
    first, last = starts[0], starts[-1]
    duration = normalized[0][1] - normalized[0][0]
    actual = set(starts)

    candidates: list[_Candidate] = []

    for rank, freq in enumerate(FREQUENCIES):
        for interval in INTERVALS:
            step = dt.timedelta(**{"weeks" if freq == "WEEKLY" else "days": interval})
            grid = _grid(first, last, step)
            grid_set = set(grid)
            exdates = sorted(grid_set - actual)
            rdates = sorted(actual - grid_set)
            strategy = f"RRULE:FREQ={freq};INTERVAL={interval}"
            ics = _build_ics(
                uid, first, duration, last, freq, interval, exdates, rdates
            )
            candidate = _Candidate(
                strategy=strategy,
                exception_count=len(exdates) + len(rdates),
                ics=ics,
                is_rrule=True,
                freq_rank=rank,
                interval=interval,
            )
            if _round_trips(ics, normalized, first, last, duration):
                candidates.append(candidate)

    # Fallback: DTSTART + one RDATE per remaining instance, no RRULE.
    fallback_rdates = starts[1:]
    fallback_ics = _build_ics(
        uid, first, duration, last, None, None, [], fallback_rdates
    )
    fallback = _Candidate(
        strategy=FALLBACK_STRATEGY,
        exception_count=len(fallback_rdates),
        ics=fallback_ics,
        is_rrule=False,
        freq_rank=len(FREQUENCIES),
        interval=INTERVALS[-1] + 1,
    )
    if _round_trips(fallback_ics, normalized, first, last, duration):
        candidates.append(fallback)

    if not candidates:
        # The fallback always round-trips, so this is defensive only.
        raise CompressionError("no candidate representation could be verified")

    best = min(candidates, key=_Candidate.sort_key)
    return CompressedSchedule(
        strategy=best.strategy,
        exception_count=best.exception_count,
        ics=best.ics.decode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Validation / construction helpers
# ---------------------------------------------------------------------------


def _validate(ordered_input: list[tuple[dt.datetime, dt.datetime]]) -> None:
    seen: set[dt.datetime] = set()
    duration: dt.timedelta | None = None
    for index, (start, end) in enumerate(ordered_input):
        if end <= start:
            raise CompressionError(
                f"instance {index} end {_stamp(end)} must be later than its "
                f"start {_stamp(start)}"
            )
        if start in seen:
            raise CompressionError(f"duplicate instance start: {_stamp(start)}")
        seen.add(start)
        instance_duration = end - start
        if duration is None:
            duration = instance_duration
        elif instance_duration != duration:
            raise CompressionError(
                "all instances must have equal duration: instance 0 lasts "
                f"{duration} but instance {index} lasts {instance_duration}"
            )

    for (start, end), (next_start, _) in zip(ordered_input, ordered_input[1:]):
        if start < next_start < end:
            raise CompressionError(
                f"instances overlap: {_stamp(start)}..{_stamp(end)} overlaps "
                f"an instance starting {_stamp(next_start)}"
            )


def _grid(first: dt.datetime, last: dt.datetime, step: dt.timedelta):
    points: list[dt.datetime] = []
    current = first
    while current <= last:
        points.append(current)
        current += step
    return points


def _build_ics(
    uid: str,
    first: dt.datetime,
    duration: dt.timedelta,
    last: dt.datetime,
    freq: str | None,
    interval: int | None,
    exdates: list[dt.datetime],
    rdates: list[dt.datetime],
) -> bytes:
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", PRODID)

    event = Event()
    event.add("uid", uid)
    event.add("dtstart", first)
    event.add("dtend", first + duration)
    if freq is not None:
        event.add(
            "rrule",
            {"FREQ": freq, "INTERVAL": interval, "UNTIL": last},
        )
    if exdates:
        event.add("exdate", exdates)
    if rdates:
        event.add("rdate", rdates)
    calendar.add_component(event)
    return calendar.to_ical()


def _round_trips(
    ics: bytes,
    expected: list[tuple[dt.datetime, dt.datetime]],
    first: dt.datetime,
    last: dt.datetime,
    duration: dt.timedelta,
) -> bool:
    """Expand the candidate and require the exact same (start, end) set."""
    try:
        expanded = expand_calendar(
            ics.decode("utf-8"), first, last + duration
        )
    except Exception:
        return False
    got = sorted((instance.start, instance.end) for instance in expanded)
    return got == sorted(expected)


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # naive input is assumed to be UTC
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

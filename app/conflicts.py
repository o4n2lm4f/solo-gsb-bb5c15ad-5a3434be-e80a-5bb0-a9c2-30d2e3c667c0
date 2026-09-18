"""Concurrency conflict detection across named calendars.

Each named calendar is expanded with the same recurrence engine used by
``/expand`` and ``/availability``. Only instances that are not
``STATUS:CANCELLED`` and have a positive duration participate; every
participating interval is clipped to the UTC query window
``[window_start, window_end)``.

A sweep line visits all interval start/end instants in chronological order.
Intervals are half-open ``[start, end)``, so at one and the same instant an
ending instance is removed *before* a starting instance is added: two
back-to-back meetings share no instant and never count as concurrent.

Each maximal segment whose active set size reaches ``minimum_concurrency``
(which must be at least 2) is returned, carrying the count and the
participating instances, sorted by ``calendar``, then ``UID``, then
``RECURRENCE-ID`` (a ``null`` ``RECURRENCE-ID`` sorts first). Two adjacent
segments are merged only when their participating instances are exactly the
same; the result is sorted by ``start``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

# Sorts before any real RECURRENCE-ID, so a non-recurring instance
# (recurrence_id is None) comes first for the same calendar and UID.
_NO_RECURRENCE_ID = dt.datetime.min.replace(tzinfo=UTC)

# Sweep events are (instant, kind, participant): ends are processed before
# starts at the same instant, implementing half-open [start, end) semantics.
_EVENT_END = 0
_EVENT_START = 1


class ConflictsError(Exception):
    """Invalid conflicts input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class Participant:
    """One instance participating in a conflict segment."""

    calendar: str
    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    start: dt.datetime  # aware UTC, clipped to the query window
    end: dt.datetime  # aware UTC, clipped to the query window
    summary: str | None
    status: str


@dataclass(frozen=True)
class ConflictSegment:
    """One non-empty UTC interval with at least ``minimum_concurrency`` active."""

    start: dt.datetime
    end: dt.datetime
    count: int
    participants: tuple[Participant, ...]


def find_conflicts(
    calendars: dict[str, str],
    window_start: dt.datetime,
    window_end: dt.datetime,
    minimum_concurrency: int,
) -> list[ConflictSegment]:
    """Scan the named calendars and return the high-concurrency segments."""
    intervals: list[tuple[dt.datetime, dt.datetime, Participant]] = []
    for calendar_name, ics in calendars.items():
        try:
            instances = expand_calendar(ics, window_start, window_end)
        except ExpansionError as exc:
            raise ConflictsError(str(exc)) from exc
        for inst in instances:
            if inst.status == "CANCELLED" or inst.end <= inst.start:
                continue
            # Clip the instance to the half-open query window; an instance
            # merely touching a boundary contributes no positive span here.
            start = inst.start if inst.start > window_start else window_start
            end = inst.end if inst.end < window_end else window_end
            if end <= start:
                continue
            intervals.append(
                (
                    start,
                    end,
                    Participant(
                        calendar=calendar_name,
                        uid=inst.uid,
                        recurrence_id=inst.recurrence_id,
                        start=start,
                        end=end,
                        summary=inst.summary,
                        status=inst.status,
                    ),
                )
            )

    # Sweep events: (instant, kind, participant). The kind orders ends before
    # starts at one instant, implementing half-open [start, end) semantics.
    events: list[tuple[dt.datetime, int, Participant]] = []
    for start, end, participant in intervals:
        events.append((end, _EVENT_END, participant))
        events.append((start, _EVENT_START, participant))
    # Clipping guarantees every event lies inside [window_start, window_end].
    events.sort(key=lambda e: (e[0], e[1]))

    Key = tuple[str, str, dt.datetime | None]
    raw: list[tuple[dt.datetime, dt.datetime, dict[Key, Participant]]] = []
    active: dict[Key, Participant] = {}
    cursor = window_start
    for instant, kind, participant in events:
        # The segment before this instant keeps the state prior to applying
        # the events AT the instant; at window_end this closes the window.
        if instant > cursor and len(active) >= minimum_concurrency:
            raw.append((cursor, instant, dict(active)))
        key = participant_key(participant)
        if kind == _EVENT_END:
            active.pop(key, None)
        else:
            active[key] = participant
        cursor = instant

    # Merge adjacent segments only while their participant sets are identical.
    merged: list[ConflictSegment] = []
    for start, end, active_set in raw:
        participants = tuple(
            sorted(active_set.values(), key=_participant_sort_key)
        )
        if merged and start == merged[-1].end:
            prev = merged[-1]
            if {participant_key(p) for p in prev.participants} == active_set.keys():
                merged[-1] = ConflictSegment(
                    start=prev.start,
                    end=end,
                    count=prev.count,
                    participants=prev.participants,
                )
                continue
        merged.append(
            ConflictSegment(
                start=start,
                end=end,
                count=len(participants),
                participants=participants,
            )
        )
    return merged


def participant_key(participant: Participant) -> tuple[str, str, dt.datetime | None]:
    """Stable identity of an instance inside a named calendar."""
    return participant.calendar, participant.uid, participant.recurrence_id


def _participant_sort_key(participant: Participant) -> tuple[str, str, dt.datetime]:
    return (
        participant.calendar,
        participant.uid,
        participant.recurrence_id or _NO_RECURRENCE_ID,
    )

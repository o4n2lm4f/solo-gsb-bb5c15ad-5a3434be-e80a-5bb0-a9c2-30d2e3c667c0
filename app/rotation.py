"""Fair cross-timezone rotation scheduling.

Given an organizer timezone, a list of organizer-local dates each carrying
candidate local start times, a meeting duration, and any number of
participants (IANA timezone + busy calendar + preferred local periods),
choose exactly one candidate per date so that the joint plan is fair.

Candidates are constructed on the organizer's local dates with ``zoneinfo``
(a fixed local wall-clock time shifts correctly across DST transitions) and
converted to UTC. A candidate is feasible when the meeting interval
``[start, start + duration)`` overlaps no participant's busy time; busy
time is produced by the same recurrence expansion engine as ``/expand``
(instances that are not ``STATUS:CANCELLED`` and have a positive
duration).

Every meeting costs a participant the minutes of its interval that fall
outside the participant's preferred local periods (materialized per local
date in the participant's own timezone, so DST is honored there too);
the cost accumulates over all dates. The joint plan minimizes, in order:

1. the largest per-participant cumulative deviation (fairness),
2. the total deviation summed over all participants,
3. candidate input order (earlier dates, then earlier candidates within
   their date, win remaining ties).

The joint search space (the product of the feasible candidate counts of
all dates) is bounded by ``MAX_COMBINATIONS``; a larger space, or a date
without any feasible candidate, is rejected with :class:`RotationError`.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import LocalRange
from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
MICROSECOND = dt.timedelta(microseconds=1)

# Hard bound on the joint search space: the product of the feasible
# candidate counts of all dates may not exceed this many combinations.
MAX_COMBINATIONS = 50_000


class RotationError(Exception):
    """Invalid rotation input or infeasible plan; surfaced as HTTP 422."""


@dataclass(frozen=True)
class ParticipantSpec:
    """One participant: identity, IANA timezone, busy calendar, preferences."""

    name: str
    timezone: str
    busy_ics: str
    preferred: tuple[LocalRange, ...]


@dataclass(frozen=True)
class Candidate:
    """One candidate meeting slot on an organizer-local date."""

    date: dt.date  # organizer-local date the candidate was authored on
    index: int  # 0-based position in the date's candidate list (input order)
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC


@dataclass(frozen=True)
class ChosenMeeting:
    """The candidate selected for one date, with per-participant costs."""

    date: dt.date
    candidate_index: int
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    local_start: dt.datetime  # organizer-local, offset-bearing
    local_end: dt.datetime  # organizer-local, offset-bearing
    # Deviation of this meeting per participant (request order), in
    # microseconds: the meeting time outside the preferred local periods.
    deviations: tuple[int, ...]


@dataclass(frozen=True)
class RotationPlan:
    """The winning joint plan and its objective values (microseconds)."""

    meetings: tuple[ChosenMeeting, ...]
    cumulative: tuple[int, ...]  # per participant (request order)
    max_cumulative: int
    total: int


def plan_fair_rotation(
    organizer_timezone: str,
    dates: list[tuple[dt.date, tuple[dt.time, ...]]],
    duration: dt.timedelta,
    participants: list[ParticipantSpec],
) -> RotationPlan:
    """Choose one candidate per date, jointly optimal for all participants."""
    organizer_tz = _load_timezone(organizer_timezone)

    # Candidates are built per organizer-local date and localized with
    # zoneinfo, so a fixed wall-clock time shifts correctly across DST.
    per_date: list[list[Candidate]] = []
    for date, times in dates:
        candidates = []
        for index, local_time in enumerate(times):
            start = dt.datetime.combine(date, local_time, tzinfo=organizer_tz)
            start_utc = start.astimezone(UTC)
            candidates.append(
                Candidate(
                    date=date,
                    index=index,
                    start=start_utc,
                    end=start_utc + duration,
                )
            )
        per_date.append(candidates)

    flat = [candidate for candidates in per_date for candidate in candidates]
    window_start = min(candidate.start for candidate in flat)
    window_end = max(candidate.end for candidate in flat)

    names: set[str] = set()
    participant_zones: list[ZoneInfo] = []
    busy_per_participant: list[list[tuple[dt.datetime, dt.datetime]]] = []
    for participant in participants:
        if participant.name in names:
            raise RotationError(
                f"duplicate participant name: {participant.name!r}"
            )
        names.add(participant.name)
        participant_zones.append(_load_timezone(participant.timezone))
        busy_per_participant.append(
            _busy_intervals(participant.busy_ics, window_start, window_end)
        )

    # Feasibility: the meeting interval must avoid every participant's
    # merged busy intervals (half-open overlap).
    feasible: list[list[Candidate]] = []
    for (date, _), candidates in zip(dates, per_date):
        surviving = [
            candidate
            for candidate in candidates
            if _is_feasible(candidate, busy_per_participant)
        ]
        if not surviving:
            raise RotationError(
                f"no feasible candidate on organizer date {date.isoformat()}"
            )
        feasible.append(surviving)

    combinations = 1
    for surviving in feasible:
        combinations *= len(surviving)
    if combinations > MAX_COMBINATIONS:
        raise RotationError(
            f"joint search space has {combinations} combinations, "
            f"exceeding the limit of {MAX_COMBINATIONS}"
        )

    # deviation[p][d][k]: cost of the k-th feasible candidate of date d for
    # participant p, in microseconds.
    deviation: list[list[list[int]]] = [
        [
            [
                _deviation_micros(
                    candidate.start,
                    candidate.end,
                    participant_zones[p],
                    participants[p].preferred,
                )
                for candidate in surviving
            ]
            for surviving in feasible
        ]
        for p in range(len(participants))
    ]

    count = len(participants)
    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_combo: tuple[int, ...] = ()
    best_cumulative: tuple[int, ...] = (0,) * count
    for combo in itertools.product(*(range(len(s)) for s in feasible)):
        cumulative = [0] * count
        for date_index, choice in enumerate(combo):
            for p in range(count):
                cumulative[p] += deviation[p][date_index][choice]
        # Remaining ties fall to candidate input order: the combo tuple
        # compares lexicographically by date, then candidate position.
        key = (max(cumulative) if cumulative else 0, sum(cumulative), combo)
        if best_key is None or key < best_key:
            best_key = key
            best_combo = combo
            best_cumulative = tuple(cumulative)

    meetings = []
    for date_index, choice in enumerate(best_combo):
        candidate = feasible[date_index][choice]
        meetings.append(
            ChosenMeeting(
                date=candidate.date,
                candidate_index=candidate.index,
                start=candidate.start,
                end=candidate.end,
                local_start=candidate.start.astimezone(organizer_tz),
                local_end=candidate.end.astimezone(organizer_tz),
                deviations=tuple(
                    deviation[p][date_index][choice] for p in range(count)
                ),
            )
        )

    assert best_key is not None  # dates is non-empty, so a plan exists
    return RotationPlan(
        meetings=tuple(meetings),
        cumulative=best_cumulative,
        max_cumulative=best_key[0],
        total=best_key[1],
    )


def minutes_from_micros(micros: int) -> int | float:
    """Render a microsecond cost as minutes, keeping integers exact."""
    if micros % 60_000_000 == 0:
        return micros // 60_000_000
    return micros / 60_000_000


# ---------------------------------------------------------------------------
# Participant busy time and preferences
# ---------------------------------------------------------------------------


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise RotationError(f"unknown timezone: {name!r}") from exc


def _busy_intervals(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Merged busy intervals of one participant's calendar, in UTC.

    The calendar is expanded with the same recurrence engine as ``/expand``
    over the span covering every candidate; ``STATUS:CANCELLED`` instances
    and non-positive-duration instances never block time. A blank calendar
    text means the participant has no busy time at all.
    """
    if not ics_text.strip():
        return []
    try:
        instances = expand_calendar(ics_text, window_start, window_end)
    except ExpansionError as exc:
        raise RotationError(str(exc)) from exc
    intervals = [
        (inst.start, inst.end)
        for inst in instances
        if inst.status != "CANCELLED" and inst.end > inst.start
    ]
    return _merge_intervals(intervals)


def _is_feasible(
    candidate: Candidate,
    busy_per_participant: list[list[tuple[dt.datetime, dt.datetime]]],
) -> bool:
    """True when the meeting overlaps no participant's busy intervals."""
    for busy in busy_per_participant:
        for b_start, b_end in busy:
            if b_start >= candidate.end:
                break  # merged intervals are sorted; none later can overlap
            if b_end > candidate.start:
                return False
    return True


def _deviation_micros(
    start: dt.datetime,
    end: dt.datetime,
    tz: ZoneInfo,
    preferred: tuple[LocalRange, ...],
) -> int:
    """Microseconds of ``[start, end)`` outside the preferred local periods.

    The preferred ranges are materialized per local date in the
    participant's timezone (DST-correct) and merged; the deviation is the
    meeting time they do not cover. A participant without preferred
    periods deviates by the full meeting length.
    """
    if not preferred:
        return (end - start) // MICROSECOND
    first_date = start.astimezone(tz).date()
    last_date = (end - MICROSECOND).astimezone(tz).date()
    periods: list[tuple[dt.datetime, dt.datetime]] = []
    date = first_date
    while date <= last_date:
        for rng in preferred:
            p_start = dt.datetime.combine(date, rng.start, tzinfo=tz).astimezone(UTC)
            p_end = dt.datetime.combine(date, rng.end, tzinfo=tz).astimezone(UTC)
            if p_end > p_start:
                periods.append((p_start, p_end))
        date += DAY
    covered = dt.timedelta(0)
    for p_start, p_end in _merge_intervals(periods):
        overlap_start = max(start, p_start)
        overlap_end = min(end, p_end)
        if overlap_end > overlap_start:
            covered += overlap_end - overlap_start
    return ((end - start) - covered) // MICROSECOND


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

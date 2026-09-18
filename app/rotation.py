"""Fair cross-timezone rotating-meeting scheduling.

Given an organizer IANA timezone, several organizer-local dates, candidate
wall-clock times (interpreted in the organizer's timezone), a meeting
duration and a roster of participants (each with an IANA timezone, an ICS
busy calendar and preferred local periods), choose *one* candidate per
date jointly so the rotation is as fair as possible:

1. minimize the largest cumulative deviation-from-preference any single
   participant accumulates over all meetings,
2. then minimize the sum of every participant's cumulative deviation,
3. then minimize the tuple of chosen candidate input positions (so a
   request-identical instance always yields the same schedule).

Busy calendars are expanded with the same recurrence engine as the rest
of the service: a candidate is feasible for a date only when it overlaps
no busy interval of *any* participant. Busy intervals reuse the
``/freebusy-export`` semantics (CANCELLED and TRANSPARENT instances do not
block, DATE all-day events occupy local full days in the participant's own
timezone, and intervals are merged with touching ones collapsed).

A candidate's deviation for one participant is the number of minutes of
the meeting that fall outside that participant's preferred local periods,
measured on the participant's own local clock. All local wall-clock
construction goes through ``zoneinfo`` date by date, so DST transitions
(including 23/25-hour days) are handled correctly in every timezone.

The complete Cartesian product of feasible candidates is enumerated; when
it exceeds ``MAX_COMBINATIONS`` (or any date has no feasible candidate) a
:class:`RotationError` is raised, which the HTTP layer turns into 422.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from itertools import product
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import LocalRange
from .freebusy import FreeBusyError, busy_periods

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)

# Safety bound on the number of candidate schedules enumerated.
MAX_COMBINATIONS = 50_000


class RotationError(Exception):
    """Invalid fair-rotation input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class Participant:
    """One rotation participant's scheduling inputs."""

    participant_id: str
    tzname: str
    ics: str
    preferences: tuple[LocalRange, ...]


@dataclass(frozen=True)
class Candidate:
    """One feasible organizer-local start on one of the dates."""

    # Position of this candidate in the request's candidate_times list.
    input_index: int
    start: dt.datetime  # aware UTC


@dataclass(frozen=True)
class ChosenMeeting:
    """The winning candidate for one date."""

    date: dt.date
    candidate_index: int
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    # Minutes outside each participant's preferred periods for this meeting.
    deviations: dict[str, int]
    # Cumulative deviation per participant after this meeting, in date order.
    cumulative_costs: dict[str, int]


@dataclass(frozen=True)
class RotationPlan:
    dates: tuple[dt.date, ...]
    meetings: tuple[ChosenMeeting, ...]
    participant_ids: tuple[str, ...]
    cumulative_costs: dict[str, int]
    max_deviation: int
    total_deviation: int
    combinations: int


def plan_fair_rotation(
    organizer_tzname: str,
    dates: list[dt.date],
    candidate_times: list[dt.time],
    duration: dt.timedelta,
    participants: list[Participant],
) -> RotationPlan:
    """Select one feasible candidate per date minimizing the fair-rotation
    objective; raise :class:`RotationError` on infeasibility or overflow."""
    organizer_tz = _zone(organizer_tzname, "organizer timezone")
    participant_tzs = {
        p.participant_id: _zone(p.tzname, f"timezone of participant {p.participant_id!r}")
        for p in participants
    }

    # Build candidates on each organizer-local date (wall-clock -> UTC via
    # zoneinfo, so the offset follows the DST rule in effect that day).
    raw_by_date: list[list[tuple[int, dt.datetime, dt.datetime]]] = []
    for date in dates:
        starts: dict[dt.datetime, tuple[int, dt.datetime, dt.datetime]] = {}
        for index, local_time in enumerate(candidate_times):
            start = dt.datetime.combine(
                date, local_time, tzinfo=organizer_tz
            ).astimezone(UTC)
            # Duplicate UTC instants (only possible on pathological DST
            # input) collapse to their first input position.
            starts.setdefault(start, (index, start, start + duration))
        raw_by_date.append(list(starts.values()))

    if raw_by_date:
        window_start = min(item[1] for item in raw_by_date[0])
        window_end = max(
            item[2] for by_date in raw_by_date for item in by_date
        )
    else:  # an empty date list is rejected by the request model
        window_start = window_end = dt.datetime.now(tz=UTC)

    # Each participant's merged busy intervals over the whole rotation span.
    busy_by_participant: dict[str, list[tuple[dt.datetime, dt.datetime]]] = {}
    for participant in participants:
        try:
            busy_by_participant[participant.participant_id] = busy_periods(
                participant.ics, participant.tzname, window_start, window_end
            )
        except FreeBusyError as exc:
            raise RotationError(
                f"participant {participant.participant_id!r}: {exc}"
            ) from exc

    # Keep only candidates free for every participant on each date.
    feasible: list[list[Candidate]] = []
    for date, raw in zip(dates, raw_by_date):
        free = [
            Candidate(input_index=index, start=start)
            for index, start, end in raw
            if not _any_overlap(start, end, busy_by_participant, participants)
        ]
        if not free:
            raise RotationError(
                f"no feasible candidate on organizer-local date "
                f"{date.isoformat()} (every candidate conflicts with a "
                "participant calendar)"
            )
        feasible.append(free)

    combinations = 1
    for options in feasible:
        combinations *= len(options)
    if combinations > MAX_COMBINATIONS:
        raise RotationError(
            f"candidate space has {combinations} combinations, exceeding "
            f"the supported maximum of {MAX_COMBINATIONS}"
        )

    # Enumerate the joint schedules and keep the lexicographically smallest
    # objective (max cumulative deviation, total, candidate input tuple).
    best: tuple[tuple[int, ...], tuple[Candidate, ...]] | None = None
    for picks in product(*feasible):
        score = _score(list(picks), duration, participants, participant_tzs)
        if best is None or score < best[0]:
            best = (score, picks)

    assert best is not None
    score, chosen = best

    meetings, totals = _build_meetings(
        list(dates), list(chosen), duration, participants, participant_tzs
    )
    return RotationPlan(
        dates=tuple(dates),
        meetings=tuple(meetings),
        participant_ids=tuple(p.participant_id for p in participants),
        cumulative_costs=dict(totals),
        max_deviation=score[0],
        total_deviation=score[1],
        combinations=combinations,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(
    picks: list[Candidate],
    duration: dt.timedelta,
    participants: list[Participant],
    participant_tzs: dict[str, ZoneInfo],
) -> tuple[int, ...]:
    """Objective tuple: per-participant cumulative deviations aggregated as
    (maximum, total), followed by the chosen candidate input positions."""
    cumulative: dict[str, int] = {p.participant_id: 0 for p in participants}
    for candidate in picks:
        end = candidate.start + duration
        for participant in participants:
            cumulative[participant.participant_id] += _deviation(
                candidate.start,
                end,
                participant,
                participant_tzs[participant.participant_id],
            )
    values = list(cumulative.values())
    return (max(values, default=0), sum(values), *(c.input_index for c in picks))


def _build_meetings(
    dates: list[dt.date],
    picks: list[Candidate],
    duration: dt.timedelta,
    participants: list[Participant],
    participant_tzs: dict[str, ZoneInfo],
) -> tuple[list[ChosenMeeting], dict[str, int]]:
    """Materialize the winning schedule with per-meeting and cumulative cost."""
    cumulative: dict[str, int] = {p.participant_id: 0 for p in participants}
    meetings: list[ChosenMeeting] = []
    for date, candidate in zip(dates, picks):
        end = candidate.start + duration
        deviations: dict[str, int] = {}
        for participant in participants:
            minutes = _deviation(
                candidate.start,
                end,
                participant,
                participant_tzs[participant.participant_id],
            )
            cumulative[participant.participant_id] += minutes
            deviations[participant.participant_id] = minutes
        meetings.append(
            ChosenMeeting(
                date=date,
                candidate_index=candidate.input_index,
                start=candidate.start,
                end=end,
                deviations=dict(deviations),
                cumulative_costs=dict(cumulative),
            )
        )
    return meetings, cumulative


def _deviation(
    start: dt.datetime,
    end: dt.datetime,
    participant: Participant,
    tz: ZoneInfo,
) -> int:
    """Minutes of ``[start, end)`` outside the participant's preferred local
    periods on the local dates the meeting occupies.

    Both the meeting and the preferred periods are projected onto the
    participant's own local clock; the preferred intervals are localized
    back to UTC per occupied local date, so their offsets follow DST. With
    no preferred periods the whole meeting counts as deviation.
    """
    if not participant.preferences:
        return int((end - start).total_seconds() // 60)

    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    preferred_utc: list[tuple[dt.datetime, dt.datetime]] = []
    day = local_start.date()
    while day <= local_end.date():
        for rng in participant.preferences:
            p_start = dt.datetime.combine(day, rng.start, tzinfo=tz).astimezone(UTC)
            p_end = dt.datetime.combine(day, rng.end, tzinfo=tz).astimezone(UTC)
            if p_end > p_start:  # skip inverted/zero periods (validated upstream)
                preferred_utc.append((p_start, p_end))
        day += DAY
    preferred_utc.sort()

    inside = dt.timedelta()
    for p_start, p_end in preferred_utc:
        o_start = start if start > p_start else p_start
        o_end = end if end < p_end else p_end
        if o_end > o_start:
            inside += o_end - o_start
    deviation = (end - start) - inside
    return int(deviation.total_seconds() // 60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _any_overlap(
    start: dt.datetime,
    end: dt.datetime,
    busy_by_participant: dict[str, list[tuple[dt.datetime, dt.datetime]]],
    participants: list[Participant],
) -> bool:
    """True when the half-open candidate overlaps any busy interval."""
    for participant in participants:
        for b_start, b_end in busy_by_participant[participant.participant_id]:
            if b_end <= start:
                continue
            if b_start >= end:
                break  # busy lists are sorted and merged
            return True
    return False


def _zone(tzname: str, what: str) -> ZoneInfo:
    try:
        return ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise RotationError(f"unknown {what}: {tzname!r}") from exc

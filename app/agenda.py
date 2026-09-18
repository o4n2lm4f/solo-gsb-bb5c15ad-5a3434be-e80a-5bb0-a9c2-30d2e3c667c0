"""Local-day agenda rendering.

Reuses the recurrence expansion engine (:mod:`app.expander`) and projects
its UTC instances onto an IANA display timezone.

Timed instances are split into one display fragment per local date they
touch: an instance crossing local midnight appears under every date it
spans, with ``continues_before``/``continues_after`` marking the fragments
before the first / after the last.

All-day instances (DATE-valued ``DTSTART``) carry no timezone: their
authored calendar dates are returned day by day exactly as written and are
never projected through the display timezone (a DATE midnight must not be
shifted to the previous/next local day by a UTC offset).

Fragments are ordered by local date ascending; within one date all-day
entries come first, then timed fragments by their local start, then
``uid``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)


class AgendaError(Exception):
    """Invalid agenda input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class AgendaFragment:
    uid: str
    summary: str | None
    date: dt.date
    start: dt.datetime  # aware, in the display timezone
    end: dt.datetime  # aware, in the display timezone
    all_day: bool
    continues_before: bool
    continues_after: bool


def build_agenda(
    ics_text: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    tzname: str,
) -> list[AgendaFragment]:
    """Expand ``ics_text`` over the UTC window and render local-day fragments."""
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise AgendaError(f"unknown timezone: {tzname!r}") from exc

    try:
        instances = expand_calendar(ics_text, window_start, window_end)
    except ExpansionError as exc:
        raise AgendaError(str(exc)) from exc

    fragments: list[AgendaFragment] = []
    for inst in instances:
        if inst.status == "CANCELLED":
            continue
        if inst.all_day:
            fragments.extend(_all_day_fragments(inst, tz))
        else:
            fragments.extend(_timed_fragments(inst, tz))

    fragments.sort(
        key=lambda f: (
            f.date,
            0 if f.all_day else 1,  # all-day entries sort first within a date
            f.start,
            f.uid,
        )
    )
    return fragments


def _local_midnight(date: dt.date, tz: ZoneInfo) -> dt.datetime:
    """Local midnight for ``date``; combine() resolves fold/DST ambiguities."""
    return dt.datetime.combine(date, dt.time.min, tzinfo=tz)


def _all_day_fragments(inst, tz: ZoneInfo) -> list[AgendaFragment]:
    """One fragment per authored DATE (never projected through the display tz).

    The expander turns DATE values into floating midnights normalized to
    UTC, so ``start.date()`` recovers the exact calendar dates the event
    author wrote; rendering those dates as local midnights keeps them on
    the same days in every display timezone.
    """
    first_date = inst.start.astimezone(UTC).date()
    last_date = inst.end.astimezone(UTC).date() - DAY
    fragments: list[AgendaFragment] = []
    date = first_date
    while date <= last_date:
        day_start = _local_midnight(date, tz)
        fragments.append(
            AgendaFragment(
                uid=inst.uid,
                summary=inst.summary,
                date=date,
                start=day_start,
                end=_local_midnight(date + DAY, tz),
                all_day=True,
                continues_before=date > first_date,
                continues_after=date < last_date,
            )
        )
        date += DAY
    return fragments


def _timed_fragments(inst, tz: ZoneInfo) -> list[AgendaFragment]:
    """Split a timed instance at local-midnight boundaries of ``tz``."""
    local_start = inst.start.astimezone(tz)
    local_end = inst.end.astimezone(tz)

    if local_end <= local_start:
        # Zero/negative-duration instance: a single instant/empty fragment.
        return [
            AgendaFragment(
                uid=inst.uid,
                summary=inst.summary,
                date=local_start.date(),
                start=local_start,
                end=local_end,
                all_day=False,
                continues_before=False,
                continues_after=False,
            )
        ]

    date = local_start.date()
    last_date = local_end.date()
    fragments: list[AgendaFragment] = []
    while True:
        day_start = _local_midnight(date, tz)
        day_end = _local_midnight(date + DAY, tz)
        frag_start = max(local_start, day_start)
        frag_end = min(local_end, day_end)
        if frag_end > frag_start:
            fragments.append(
                AgendaFragment(
                    uid=inst.uid,
                    summary=inst.summary,
                    date=date,
                    start=frag_start,
                    end=frag_end,
                    all_day=False,
                    continues_before=local_start < day_start,
                    continues_after=local_end > day_end,
                )
            )
        if date >= last_date:
            break
        date += DAY
    return fragments


"""Difference between two versions of an iCalendar over one UTC window.

Both calendars are expanded with the same recurrence expander
(:func:`app.expander.expand_calendar`) over the *same* UTC query window,
and the resulting instances are compared by their stable identity:

* a recurring instance is identified by ``UID + RECURRENCE-ID``;
* a non-recurring event is identified by ``UID`` alone
  (its ``RECURRENCE-ID`` is ``None``).

Instances present only in the new calendar are ``added``; instances present
only in the old calendar are ``removed``. An instance present on both sides
whose ``start``, ``end``, ``summary`` or ``status`` differs is ``modified``
and reported with both its ``before`` and ``after`` values; identical
instances are not reported at all.

Note that the expander suppresses instances cancelled via
``STATUS:CANCELLED`` overrides, so cancelling an occurrence shows up here as
``removed``, exactly as it disappears from an ``/expand`` response.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import Instance, expand_calendar

UTC = dt.timezone.utc

# Sorts before any real RECURRENCE-ID, so a non-recurring instance
# (recurrence_id is None) comes first for the same UID.
_NO_RECURRENCE_ID = dt.datetime.min.replace(tzinfo=UTC)

# Fields that make an instance present on both sides "modified".
_COMPARED_FIELDS = ("start", "end", "summary", "status")


@dataclass(frozen=True)
class CalendarDiff:
    """Instances added, removed and modified between two calendar versions."""

    added: list[Instance]
    removed: list[Instance]
    # (before, after) pairs sharing one identity.
    modified: list[tuple[Instance, Instance]]


def instance_key(instance: Instance) -> tuple[str, dt.datetime | None]:
    """Stable identity of an expanded instance."""
    return instance.uid, instance.recurrence_id


def _sort_key(key: tuple[str, dt.datetime | None]) -> tuple[str, dt.datetime]:
    uid, recurrence_id = key
    return uid, recurrence_id or _NO_RECURRENCE_ID


def diff_calendars(
    old_ics: str,
    new_ics: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> CalendarDiff:
    """Expand both calendars over one UTC window and diff the instances.

    ``ExpansionError`` from either calendar propagates to the caller, which
    maps it to HTTP 422.
    """
    old_by_id = {
        instance_key(i): i
        for i in expand_calendar(old_ics, window_start, window_end)
    }
    new_by_id = {
        instance_key(i): i
        for i in expand_calendar(new_ics, window_start, window_end)
    }

    old_keys = old_by_id.keys()
    new_keys = new_by_id.keys()

    added = [new_by_id[key] for key in sorted(new_keys - old_keys, key=_sort_key)]
    removed = [old_by_id[key] for key in sorted(old_keys - new_keys, key=_sort_key)]

    modified: list[tuple[Instance, Instance]] = []
    for key in sorted(old_keys & new_keys, key=_sort_key):
        before = old_by_id[key]
        after = new_by_id[key]
        if any(getattr(before, field) != getattr(after, field)
               for field in _COMPARED_FIELDS):
            modified.append((before, after))

    return CalendarDiff(added=added, removed=removed, modified=modified)

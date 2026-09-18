"""Per-occurrence provenance chains for one VEVENT series (one ``UID``).

While :mod:`app.expander` only returns the instances that survive every
recurrence transformation, this module explains *how* each candidate
occurrence of one series reached its final shape inside a UTC window:

1. **generation** — the authored ``DTSTART`` / ``RRULE`` / ``RDATE``
   properties that produce the occurrence's scheduled slot;
2. **exclusion** — every ``EXDATE`` matching that slot;
3. **override** — the ``RECURRENCE-ID`` component that *reschedules* the
   occurrence (possibly keeping its start, e.g. a summary-only edit),
   *cancels* it, or an orphan override that names a slot the series never
   generates;
4. **result** — the effective ``DTSTART``/``DTEND``/``STATUS`` after every
   transformation. ``final_start``/``final_end`` are ``null`` when the
   occurrence does not survive; ``final_status`` still reports ``CANCELLED``
   for a cancellation and is ``null`` only for an unresurrected ``EXDATE``.

The expansion itself is reused verbatim from :mod:`app.expander`
(:func:`~app.expander._expand_master` yields the exact surviving
occurrences the public engine produces); this module only rebuilds the
extra bookkeeping around it. Occurrences excluded by ``EXDATE`` are
re-added to the candidate set (from the authored ``EXDATE`` values inside
the same duration-padded frame) so their exclusion can be reported.

Candidates are returned when **either** their scheduled slot **or** their
final (post-override) slot overlaps the window — the same "moved in from
outside / moved out" judgment the expander applies per occurrence. The
list is stably sorted by the effective instance time (final start, else
scheduled start), then ``RECURRENCE-ID``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from dateutil.rrule import rruleset, rrulestr

from .expander import (
    MAX_OCCURRENCES_PER_UID,
    UTC,
    ExpansionError,
    _datetimes,
    _event_times,
    _expand_master,
    _overlaps,
    _parse_calendar,
    _props,
    _recurrence_id,
    _status,
    _to_utc,
    _validate_tzids,
)

# Generation kinds, also used as the stable listing order.
GEN_RRULE = "RRULE"
GEN_RDATE = "RDATE"
GEN_DTSTART = "DTSTART"

# Override actions.
OVR_RESCHEDULED = "RESCHEDULED"  # non-cancelled override, new slot differs
OVR_MODIFIED = "MODIFIED"  # non-cancelled override keeping the same slot
OVR_CANCELLED = "CANCELLED"  # STATUS:CANCELLED override


class ProvenanceError(Exception):
    """The provenance request cannot be answered; HTTP layer maps this to 422."""


@dataclass(frozen=True)
class GenerationStep:
    """One authored property that generates an occurrence slot."""

    kind: str  # RRULE | RDATE | DTSTART
    # The unfolded RRULE text (e.g. 'FREQ=DAILY;COUNT=3'); None otherwise.
    rrule: str | None = None


@dataclass(frozen=True)
class OverrideRecord:
    """The RECURRENCE-ID component applied to an occurrence slot."""

    action: str  # RESCHEDULED | MODIFIED | CANCELLED
    recurrence_id: dt.datetime  # aware UTC
    start: dt.datetime | None  # aware UTC; None when cancelled
    end: dt.datetime | None  # aware UTC; None when cancelled
    status: str


@dataclass(frozen=True)
class OccurrenceProvenance:
    """The provenance chain of one candidate occurrence."""

    recurrence_id: dt.datetime  # the scheduled slot's start, aware UTC
    generated_by: tuple[GenerationStep, ...]
    excluded_by: tuple[dt.datetime, ...]  # matching EXDATE instants, aware UTC
    override: OverrideRecord | None
    scheduled_start: dt.datetime | None  # None only for master-less orphans
    scheduled_end: dt.datetime | None
    # Effective DTSTART/DTEND after EXDATE/override processing; None when
    # the occurrence does not survive (EXDATE-excluded, CANCELLED master or
    # CANCELLED override). final_status stays CANCELLED for a cancellation
    # and is None only for an EXDATE exclusion without resurrecting override.
    final_start: dt.datetime | None
    final_end: dt.datetime | None
    final_status: str | None


@dataclass(frozen=True)
class SeriesProvenance:
    """Provenance chains for every candidate occurrence of one UID."""

    uid: str
    window_start: dt.datetime
    window_end: dt.datetime
    occurrences: tuple[OccurrenceProvenance, ...]


def provenance_for_uid(
    ics_text: str,
    uid: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> SeriesProvenance:
    """Parse ``ics_text`` and build provenance chains for the series ``uid``.

    ``ExpansionError`` (bad calendar) and :class:`ProvenanceError` (unknown
    UID) propagate to the HTTP layer, which maps both to 422.
    """
    calendar = _parse_calendar(ics_text)

    groups: dict[str, list] = {}
    for component in calendar.walk("VEVENT"):
        event_uid = component.get("UID")
        if event_uid is None:
            raise ExpansionError("VEVENT is missing the required UID property")
        groups.setdefault(str(event_uid), []).append(component)

    events = groups.get(uid)
    if events is None:
        raise ProvenanceError(f"no VEVENT with UID {uid!r}")

    return _provenance_for_group(uid, events, window_start, window_end)


# ---------------------------------------------------------------------------
# One UID group: master + overrides
# ---------------------------------------------------------------------------


def _provenance_for_group(
    uid: str,
    events: list,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> SeriesProvenance:
    for event in events:
        _validate_tzids(event)

    masters = [e for e in events if "RECURRENCE-ID" not in e]
    overrides = [e for e in events if "RECURRENCE-ID" in e]
    master = masters[0] if masters else None

    candidates: dict[dt.datetime, OccurrenceProvenance] = {}

    # Master-derived information, shared with orphan overrides below.
    duration: dt.timedelta | None = None
    master_status = "CONFIRMED"
    rule_frames: list[tuple[GenerationStep, rruleset]] = []
    frame_hits: list[tuple[GenerationStep, set[dt.datetime]]] = []
    rdate_values: set[dt.datetime] = set()
    exdate_values: set[dt.datetime] = set()
    after = before = None
    m_start = None
    dtstart_utc = None
    recurring = False

    if master is not None:
        m_start, m_end, _ = _event_times(master)
        duration = m_end - m_start
        master_status = _status(master)

        rrule_props = _props(master, "RRULE")
        rdate_props = _props(master, "RDATE")
        exdate_props = _props(master, "EXDATE")
        recurring = bool(rrule_props or rdate_props)

        for prop in rdate_props:
            rdate_values.update(_to_utc(value) for value in _datetimes(prop, m_start))
        for prop in exdate_props:
            exdate_values.update(_to_utc(value) for value in _datetimes(prop, m_start))

        if recurring:
            rule_frames = _build_rule_frames(rrule_props, m_start)
            after, before = _frame_bounds(m_start, duration, window_start, window_end)
            for step, rset in rule_frames:
                hits = _hits_in_frame(rset, after, before)
                frame_hits.append((step, hits))

            # Surviving occurrences come straight from the public engine,
            # so the scheduled candidate set cannot drift from /expand.
            survivors, _ = _expand_master(
                master, m_start, duration, window_start, window_end
            )
            generated_starts = {_to_utc(occ) for occ in survivors}

            # Re-add slots removed solely by EXDATE so their exclusion can
            # be reported; a stray EXDATE naming no generated slot is not a
            # candidate (it has no generation source).
            for exd in sorted(exdate_values):
                if _in_frame(exd, m_start, after, before) and _sources(
                    exd, frame_hits, rdate_values, _to_utc(m_start)
                ):
                    generated_starts.add(exd)
                    if len(generated_starts) > MAX_OCCURRENCES_PER_UID:
                        raise ExpansionError(
                            f"recurrence expands to more than "
                            f"{MAX_OCCURRENCES_PER_UID} occurrences within "
                            "the query window"
                        )
        else:
            # A non-recurring master ignores RDATE/EXDATE exactly like the
            # expander; its single occurrence is DTSTART itself.
            generated_starts = {_to_utc(m_start)}

        dtstart_utc = _to_utc(m_start)
        for when in generated_starts:
            sources = _sources_for(
                when,
                m_start,
                frame_hits,
                rule_frames,
                after,
                before,
                rdate_values,
                dtstart_utc,
                recurring,
            )
            scheduled_start = when
            scheduled_end = when + duration
            excluded = tuple(
                sorted(exd for exd in exdate_values if exd == when)
            ) if recurring else ()
            if excluded:
                # EXDATE removes the slot; a later override may still
                # resurrect it (handled in the override pass below).
                final_start = final_end = final_status = None
            else:
                # A STATUS:CANCELLED master suppresses the whole series;
                # the effective STATUS is still reported (with no times),
                # unless an override resurrects this specific slot.
                final_start = None if master_status == "CANCELLED" else scheduled_start
                final_end = None if master_status == "CANCELLED" else scheduled_end
                final_status = master_status
            candidates[when] = OccurrenceProvenance(
                recurrence_id=when,
                generated_by=tuple(sources),
                excluded_by=excluded,
                override=None,
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
                final_start=final_start,
                final_end=final_end,
                final_status=final_status,
            )

    # Overrides replace the occurrence at their RECURRENCE-ID, in source
    # order: the last component for one RID wins (same dict semantics as
    # the expander). A RID without a generated slot becomes an orphan
    # candidate when its own (or scheduled) time intersects the window.
    for override in overrides:
        rid = _to_utc(_recurrence_id(override, master))
        record = _override_record(override, rid, duration)
        candidate = candidates.get(rid)
        if candidate is None:
            scheduled_start = scheduled_end = None
            sources = ()
            excluded: tuple[dt.datetime, ...] = ()
            if master is not None and duration is not None:
                scheduled_start, scheduled_end = rid, rid + duration
                sources = tuple(
                    _sources_for(
                        rid,
                        m_start,
                        frame_hits,
                        rule_frames,
                        after,
                        before,
                        rdate_values,
                        dtstart_utc,
                        recurring,
                    )
                )
                excluded = tuple(
                    sorted(exd for exd in exdate_values if exd == rid)
                )
            candidate = OccurrenceProvenance(
                recurrence_id=rid,
                generated_by=sources,
                excluded_by=excluded,
                override=None,
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
                final_start=None,
                final_end=None,
                final_status=None,
            )

        if record.action == OVR_CANCELLED:
            # A CANCELLED override removes the instance: no effective times,
            # but the effective STATUS is still CANCELLED.
            final_start = final_end = None
            final_status = record.status
        else:
            final_start, final_end = record.start, record.end
            final_status = record.status
        candidates[rid] = OccurrenceProvenance(
            recurrence_id=candidate.recurrence_id,
            generated_by=candidate.generated_by,
            excluded_by=candidate.excluded_by,
            override=record,
            scheduled_start=candidate.scheduled_start,
            scheduled_end=candidate.scheduled_end,
            final_start=final_start,
            final_end=final_end,
            final_status=final_status,
        )

    occurrences = [
        candidate
        for candidate in candidates.values()
        if _candidate_in_window(candidate, window_start, window_end)
    ]
    occurrences.sort(key=lambda c: (_sort_start(c), c.recurrence_id))

    return SeriesProvenance(
        uid=uid,
        window_start=window_start,
        window_end=window_end,
        occurrences=tuple(occurrences),
    )


# ---------------------------------------------------------------------------
# RRULE / RDATE frames and generation attribution
# ---------------------------------------------------------------------------


def _build_rule_frames(rrule_props, start) -> list[tuple[GenerationStep, rruleset]]:
    """One (generation step, rruleset-without-EXDATE) per RRULE property."""
    frames: list[tuple[GenerationStep, rruleset]] = []
    for prop in rrule_props:
        rule_text = prop.to_ical().decode()
        rset = rruleset()
        try:
            rset.rrule(rrulestr(rule_text, dtstart=start))
        except ExpansionError:
            raise
        except Exception as exc:
            raise ExpansionError(f"invalid RRULE {rule_text!r}: {exc}") from exc
        frames.append((GenerationStep(kind=GEN_RRULE, rrule=rule_text), rset))
    return frames


def _frame_bounds(
    start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple:
    """Duration-padded query bounds expressed in the master's local frame."""
    if start.tzinfo is None:  # floating time: interpreted as UTC
        return (window_start - duration).replace(tzinfo=None), window_end.replace(
            tzinfo=None
        )
    return (window_start - duration).astimezone(start.tzinfo), window_end.astimezone(
        start.tzinfo
    )


def _hits_in_frame(rset, after, before) -> set[dt.datetime]:
    """UTC occurrence instants one RRULE generates within the padded frame."""
    hits: set[dt.datetime] = set()
    try:
        for occ in rset.between(after, before, inc=True):
            hits.add(_to_utc(occ))
            if len(hits) > MAX_OCCURRENCES_PER_UID:
                raise ExpansionError(
                    f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
                    "occurrences within the query window"
                )
    except ExpansionError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc
    return hits


def _in_frame(when_utc, master_start, after, before) -> bool:
    """True when UTC instant ``when_utc`` lies within the padded local frame."""
    if master_start.tzinfo is None:
        local = when_utc.replace(tzinfo=None)
    else:
        local = when_utc.astimezone(master_start.tzinfo)
    return after <= local <= before


def _sources(
    when_utc: dt.datetime,
    frame_hits: list[tuple[GenerationStep, set[dt.datetime]]],
    rdate_values: set[dt.datetime],
    dtstart_utc: dt.datetime,
) -> list[GenerationStep]:
    """Generation sources for an instant known to lie inside the frame.

    Every authored property that generates the slot is reported, in
    canonical order: RRULE properties (source order), then RDATE, then
    DTSTART alone. An RDATE duplicating an RRULE hit therefore lists both,
    while the DTSTART seed of an RRULE series is covered by the RRULE and
    is not listed separately.
    """
    steps: list[GenerationStep] = []
    for step, hits in frame_hits:
        if when_utc in hits:
            steps.append(step)
    if when_utc in rdate_values:
        steps.append(GenerationStep(kind=GEN_RDATE))
    if not steps and when_utc == dtstart_utc:
        steps.append(GenerationStep(kind=GEN_DTSTART))
    return steps


def _sources_for(
    when_utc,
    master_start,
    frame_hits,
    rule_frames,
    after,
    before,
    rdate_values,
    dtstart_utc,
    recurring,
) -> list[GenerationStep]:
    """Generation sources for any instant, with a point lookup outside frame."""
    if recurring and not _in_frame(when_utc, master_start, after, before):
        # Orphan override for a slot the padded window never reaches: ask
        # each RRULE directly for that one instant instead of the frame.
        steps = [
            step
            for step, rset in rule_frames
            if _rule_contains(rset, _localize(when_utc, master_start))
        ]
        if when_utc in rdate_values:
            steps.append(GenerationStep(kind=GEN_RDATE))
        if not steps and when_utc == dtstart_utc:
            steps.append(GenerationStep(kind=GEN_DTSTART))
        return steps
    return _sources(when_utc, frame_hits, rdate_values, dtstart_utc)


def _localize(when_utc: dt.datetime, master_start: dt.datetime):
    """Project a UTC instant into the RRULE's local frame."""
    if master_start.tzinfo is None:  # floating: naive UTC
        return when_utc.replace(tzinfo=None)
    return when_utc.astimezone(master_start.tzinfo)


def _rule_contains(rset, local) -> bool:
    """True when an rruleset generates exactly ``local`` (point membership)."""
    try:
        return bool(rset.between(local, local, inc=True))
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc


# ---------------------------------------------------------------------------
# Overrides / filtering / ordering
# ---------------------------------------------------------------------------


def _override_record(
    override, rid: dt.datetime, master_duration: dt.timedelta | None
) -> OverrideRecord:
    """Build the provenance record for one RECURRENCE-ID component."""
    status = _status(override)
    if status == "CANCELLED":
        return OverrideRecord(
            action=OVR_CANCELLED,
            recurrence_id=rid,
            start=None,
            end=None,
            status=status,
        )
    start, end, _ = _event_times(override, default_duration=master_duration)
    start = _to_utc(start)
    end = _to_utc(end)
    action = OVR_RESCHEDULED if start != rid else OVR_MODIFIED
    return OverrideRecord(
        action=action, recurrence_id=rid, start=start, end=end, status=status
    )


def _candidate_in_window(
    candidate: OccurrenceProvenance,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> bool:
    """Included when the scheduled or the final slot overlaps the window."""
    if candidate.scheduled_start is not None and _overlaps(
        candidate.scheduled_start,
        candidate.scheduled_end,
        window_start,
        window_end,
    ):
        return True
    if candidate.final_start is not None and _overlaps(
        candidate.final_start, candidate.final_end, window_start, window_end
    ):
        return True
    return False


def _sort_start(candidate: OccurrenceProvenance) -> dt.datetime:
    """Effective instance time: rescheduled occurrences sort at new time."""
    if candidate.final_start is not None:
        return candidate.final_start
    if candidate.scheduled_start is not None:
        return candidate.scheduled_start
    return candidate.recurrence_id

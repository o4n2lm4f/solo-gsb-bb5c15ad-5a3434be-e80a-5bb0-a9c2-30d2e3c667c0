"""Key tests for the /conflicts endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

UTC = dt.timezone.utc

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//conflict-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def event_utc(uid: str, start: str, end: str, *extra: str) -> str:
    return event(
        f"UID:{uid}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        *extra,
    )


def conflicts(calendars=None, *, minimum_concurrency=2,
              window_start="2026-01-05T00:00:00Z",
              window_end="2026-01-06T00:00:00Z"):
    return client.post(
        "/conflicts",
        json={
            "calendars": calendars or {},
            "window_start": window_start,
            "window_end": window_end,
            "minimum_concurrency": minimum_concurrency,
        },
    )


def segments(resp):
    return [
        (s["start"], s["end"], s["count"])
        for s in resp.json()["segments"]
    ]


def participant_ids(segment) -> list[tuple[str, str, str | None]]:
    return [
        (p["calendar"], p["uid"], p["recurrence_id"])
        for p in segment["participants"]
    ]


# ---------------------------------------------------------------------------
# Basic overlap
# ---------------------------------------------------------------------------

def test_simple_two_calendar_overlap():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T103000Z", "20260105T113000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.status_code == 200
    body = resp.json()
    assert body["minimum_concurrency"] == 2
    assert segments(resp) == [("2026-01-05T10:30:00Z", "2026-01-05T11:00:00Z", 2)]
    seg = body["segments"][0]
    assert participant_ids(seg) == [
        ("alice", "a", None),
        ("bob", "b", None),
    ]


def test_no_overlap_returns_no_segments():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T120000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.status_code == 200
    assert resp.json()["segments"] == []
    assert resp.json()["count"] == 0


def test_empty_calendars_returns_empty():
    resp = conflicts({})
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


def test_segments_below_threshold_dropped():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T130000Z"))
    bob = cal(
        event_utc("b1", "20260105T110000Z", "20260105T113000Z"),
        event_utc("b2", "20260105T120000Z", "20260105T123000Z"),
    )
    carol = cal(event_utc("c", "20260105T111500Z", "20260105T121500Z"))
    resp = conflicts(
        {"alice": alice, "bob": bob, "carol": carol},
        minimum_concurrency=3,
    )
    # Three-way only while all of alice, carol and b1 overlap; the 11:30-12:00
    # stretch holds just alice+carol and is dropped.
    assert segments(resp) == [
        ("2026-01-05T11:15:00Z", "2026-01-05T11:30:00Z", 3),
        ("2026-01-05T12:00:00Z", "2026-01-05T12:15:00Z", 3),
    ]


# ---------------------------------------------------------------------------
# Half-open semantics: ends are processed before starts at one instant
# ---------------------------------------------------------------------------

def test_back_to_back_meetings_are_not_concurrent():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T120000Z"))
    carol = cal(event_utc("c", "20260105T090000Z", "20260105T120000Z"))
    resp = conflicts({"alice": alice, "bob": bob, "carol": carol})
    # alice+carol then bob+carol; at 11:00 alice ends strictly before bob
    # starts, so no instant falsely carries all three, and the two pieces are
    # kept separate because their participants differ.
    assert segments(resp) == [
        ("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", 2),
        ("2026-01-05T11:00:00Z", "2026-01-05T12:00:00Z", 2),
    ]
    body = resp.json()
    assert participant_ids(body["segments"][0]) == [
        ("alice", "a", None),
        ("carol", "c", None),
    ]
    assert participant_ids(body["segments"][1]) == [
        ("bob", "b", None),
        ("carol", "c", None),
    ]


def test_event_touching_window_end_is_excluded_at_boundary():
    # The conflict ends exactly at window_end; [start, end) keeps it.
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T103000Z", "20260105T110000Z"))
    resp = conflicts(
        {"alice": alice, "bob": bob},
        window_start="2026-01-05T10:00:00Z",
        window_end="2026-01-05T11:00:00Z",
    )
    assert segments(resp) == [
        ("2026-01-05T10:30:00Z", "2026-01-05T11:00:00Z", 2)
    ]


# ---------------------------------------------------------------------------
# Window clipping
# ---------------------------------------------------------------------------

def test_overflowing_instances_are_clipped_to_window():
    alice = cal(event_utc("a", "20260104T220000Z", "20260105T020000Z"))
    bob = cal(event_utc("b", "20260104T230000Z", "20260105T030000Z"))
    resp = conflicts(
        {"alice": alice, "bob": bob},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-06T00:00:00Z",
    )
    seg = resp.json()["segments"][0]
    assert seg["start"] == "2026-01-05T00:00:00Z"
    assert seg["end"] == "2026-01-05T02:00:00Z"
    alice_p = next(p for p in seg["participants"] if p["calendar"] == "alice")
    bob_p = next(p for p in seg["participants"] if p["calendar"] == "bob")
    assert alice_p["start"] == "2026-01-05T00:00:00Z"
    assert alice_p["end"] == "2026-01-05T02:00:00Z"
    assert bob_p["end"] == "2026-01-05T03:00:00Z"


def test_instance_only_touching_window_boundary_does_not_count():
    # An instance ending exactly at window_start has zero clipped duration.
    alice = cal(event_utc("a", "20260104T230000Z", "20260105T000000Z"))
    bob = cal(event_utc("b", "20260105T000000Z", "20260105T010000Z"))
    resp = conflicts({"alice": alice, "bob": bob})
    assert resp.json()["segments"] == []


# ---------------------------------------------------------------------------
# Filtering: CANCELLED and zero duration never participate
# ---------------------------------------------------------------------------

def test_cancelled_and_zero_duration_instances_are_ignored():
    ics = cal(
        event_utc("a", "20260105T100000Z", "20260105T110000Z", "STATUS:CANCELLED"),
        event_utc("z", "20260105T103000Z", "20260105T103000Z"),
    )
    bob = cal(event_utc("b", "20260105T100000Z", "20260105T120000Z"))
    resp = conflicts({"alice": ics, "bob": bob})
    assert resp.json()["segments"] == []


def test_cancelled_recurrence_override_does_not_participate():
    ics = cal(
        event(
            "UID:r",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:r",
            "RECURRENCE-ID:20260105T100000Z",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "STATUS:CANCELLED",
        ),
    )
    bob = cal(event_utc("b", "20260105T100000Z", "20260105T110000Z"))
    resp = conflicts(
        {"alice": ics, "bob": bob},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-07T00:00:00Z",
    )
    # Only the Jan 6 occurrence overlaps Bob's Jan 5 event-free window ... Bob
    # is busy only on Jan 5, when alice's occurrence is cancelled.
    assert resp.json()["segments"] == []


# ---------------------------------------------------------------------------
# Recurring instances and recurrence-ids
# ---------------------------------------------------------------------------

def test_recurring_instances_participate_with_recurrence_id():
    ics = cal(event(
        "UID:r",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T110000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "SUMMARY:Daily",
    ))
    bob = cal(event_utc("b", "20260105T103000Z", "20260105T113000Z", "SUMMARY:Oneoff"))
    resp = conflicts(
        {"alice": ics, "bob": bob},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-07T00:00:00Z",
    )
    body = resp.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    recurring = next(
        p for p in seg["participants"] if p["uid"] == "r"
    )
    assert recurring["recurrence_id"] == "2026-01-05T10:00:00Z"
    assert recurring["summary"] == "Daily"
    assert recurring["status"] == "CONFIRMED"
    oneoff = next(p for p in seg["participants"] if p["uid"] == "b")
    assert oneoff["recurrence_id"] is None
    assert oneoff["summary"] == "Oneoff"


# ---------------------------------------------------------------------------
# Sorting: segments by start, participants by calendar/UID/RECURRENCE-ID
# ---------------------------------------------------------------------------

def test_segments_sorted_by_start():
    alice = cal(
        event_utc("a1", "20260105T100000Z", "20260105T110000Z"),
        event_utc("a2", "20260105T140000Z", "20260105T150000Z"),
    )
    bob = cal(
        event_utc("b1", "20260105T103000Z", "20260105T113000Z"),
        event_utc("b2", "20260105T143000Z", "20260105T153000Z"),
    )
    resp = conflicts({"alice": alice, "bob": bob})
    starts = [s[0] for s in segments(resp)]
    assert starts == sorted(starts)


def test_participants_sorted_by_calendar_then_uid_then_recurrence_id():
    zeta = cal(event_utc("zeta", "20260105T100000Z", "20260105T120000Z"))
    alpha = cal(
        event(
            "UID:series",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T110000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event_utc("aaa", "20260105T100000Z", "20260105T120000Z"),
    )
    resp = conflicts(
        {"zeta": zeta, "alpha": alpha},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-07T00:00:00Z",
    )
    # Jan 5 segment: all four participants; null RECURRENCE-ID sorts first.
    seg = next(
        s for s in resp.json()["segments"]
        if s["start"] == "2026-01-05T10:00:00Z"
    )
    assert [(p["calendar"], p["uid"]) for p in seg["participants"]] == [
        ("alpha", "aaa"),
        ("alpha", "series"),
        ("zeta", "zeta"),
    ]
    series = next(p for p in seg["participants"] if p["uid"] == "series")
    assert series["recurrence_id"] == "2026-01-05T09:00:00Z"


def test_null_recurrence_id_sorts_first():
    # Null RECURRENCE-ID sorts first for the same calendar and UID.
    from app.conflicts import Participant, _participant_sort_key

    s = dt.datetime(2026, 1, 5, tzinfo=UTC)
    e = dt.datetime(2026, 1, 6, tzinfo=UTC)
    null = Participant("cal", "u", None, s, e, None, "CONFIRMED")
    rid = Participant("cal", "u", s, s, e, None, "CONFIRMED")
    assert sorted([rid, null], key=_participant_sort_key) == [null, rid]


# ---------------------------------------------------------------------------
# Merging: adjacent segments with identical participants become one
# ---------------------------------------------------------------------------

def test_below_threshold_gap_keeps_neighboring_segments_separate():
    # threshold 3: 10:00-11:00 {a,b,d}, 11:00-12:00 {a,d} dropped,
    # 12:00-13:00 {a,c,d}. The outer pieces differ, so nothing merges.
    base = cal(
        event_utc("a", "20260105T100000Z", "20260105T130000Z"),
        event_utc("b", "20260105T100000Z", "20260105T110000Z"),
        event_utc("c", "20260105T120000Z", "20260105T130000Z"),
    )
    other = cal(event_utc("d", "20260105T100000Z", "20260105T130000Z"))
    resp = conflicts({"base": base, "other": other}, minimum_concurrency=3)
    assert segments(resp) == [
        ("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", 3),
        ("2026-01-05T12:00:00Z", "2026-01-05T13:00:00Z", 3),
    ]
    first, second = resp.json()["segments"]
    assert participant_ids(first) != participant_ids(second)


def test_engine_merges_adjacent_segments_with_identical_participants():
    # A boundary that splits two qualifying segments without changing the
    # active set cannot arise from ordinary non-cancelled, positive-duration
    # intervals, so exercise the merge rule directly against the engine with
    # a fake expander that emits two identical-identity back-to-back pieces.
    import app.conflicts as conflicts_mod
    from app.expander import Instance

    s = dt.datetime(2026, 1, 5, 10, tzinfo=UTC)
    m = dt.datetime(2026, 1, 5, 11, tzinfo=UTC)
    e = dt.datetime(2026, 1, 5, 12, tzinfo=UTC)

    def fake_expand(ics_text, window_start, window_end):
        return [
            Instance(
                uid="u", recurrence_id=None, start=s, end=m,
                summary=None, status="CONFIRMED", alarms=[],
            ),
            Instance(
                uid="u", recurrence_id=None, start=m, end=e,
                summary=None, status="CONFIRMED", alarms=[],
            ),
        ]

    original = conflicts_mod.expand_calendar
    conflicts_mod.expand_calendar = fake_expand
    try:
        result = conflicts_mod.find_conflicts(
            {"cal": "ignored"},
            dt.datetime(2026, 1, 5, tzinfo=UTC),
            dt.datetime(2026, 1, 6, tzinfo=UTC),
            1,
        )
    finally:
        conflicts_mod.expand_calendar = original

    assert [(seg.start, seg.end, seg.count) for seg in result] == [(s, e, 1)]
    assert len(result[0].participants) == 1


def test_identical_participant_segments_separated_by_gap_do_not_merge():
    # {a,b} overlap 10:00-11:00 and again 12:00-13:00 with a free gap:
    # two separate segments despite identical participants.
    alice = cal(
        event_utc("a1", "20260105T100000Z", "20260105T110000Z"),
        event_utc("a2", "20260105T120000Z", "20260105T130000Z"),
    )
    bob = cal(
        event_utc("b1", "20260105T100000Z", "20260105T110000Z"),
        event_utc("b2", "20260105T120000Z", "20260105T130000Z"),
    )
    resp = conflicts({"alice": alice, "bob": bob})
    assert segments(resp) == [
        ("2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", 2),
        ("2026-01-05T12:00:00Z", "2026-01-05T13:00:00Z", 2),
    ]


# ---------------------------------------------------------------------------
# Naive window handling
# ---------------------------------------------------------------------------

def test_naive_window_timestamps_treated_as_utc():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T103000Z", "20260105T113000Z"))
    resp = client.post(
        "/conflicts",
        json={
            "calendars": {"alice": alice, "bob": bob},
            "window_start": "2026-01-05T00:00:00",
            "window_end": "2026-01-06T00:00:00",
            "minimum_concurrency": 2,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["window_start"] == "2026-01-05T00:00:00Z"


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def test_inverted_window_returns_422():
    resp = conflicts(
        {},
        window_start="2026-01-06T00:00:00Z",
        window_end="2026-01-05T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_zero_width_window_returns_422():
    resp = conflicts(
        {},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-05T00:00:00Z",
    )
    assert resp.status_code == 422


def test_minimum_concurrency_below_two_returns_422():
    for value in (0, 1, -3):
        resp = conflicts({}, minimum_concurrency=value)
        assert resp.status_code == 422, value


def test_missing_minimum_concurrency_returns_422():
    resp = client.post(
        "/conflicts",
        json={
            "calendars": {},
            "window_start": "2026-01-05T00:00:00Z",
            "window_end": "2026-01-06T00:00:00Z",
        },
    )
    assert resp.status_code == 422


def test_invalid_ics_returns_422():
    resp = conflicts({"alice": "not an iCalendar document"})
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_invalid_rrule_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T110000Z",
        "RRULE:FREQ=BOGUS",
    ))
    resp = conflicts({"alice": ics})
    assert resp.status_code == 422


def test_unknown_tzid_inside_calendar_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T100000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T110000",
    ))
    resp = conflicts({"alice": ics})
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]

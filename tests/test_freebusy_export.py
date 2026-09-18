"""Key tests for the /freebusy-export endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//freebusy-api//EN\r\n"
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


def freebusy(calendars=None, *, window_start="2026-03-01T00:00:00Z",
             window_end="2026-04-01T00:00:00Z"):
    return client.post(
        "/freebusy-export",
        json={
            "calendars": calendars if calendars is not None else [],
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def one_calendar(ics: str, *, timezone="UTC", calendar_id="cal",
                 window_start="2026-03-01T00:00:00Z",
                 window_end="2026-04-01T00:00:00Z"):
    resp = freebusy(
        [{"calendar_id": calendar_id, "ics": ics, "timezone": timezone}],
        window_start=window_start,
        window_end=window_end,
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["count"] == 1
    return body["calendars"][0]


def freebusy_lines(vfreebusy: str) -> list[str]:
    """The FREEBUSY period values, in file order."""
    return [
        line.split(":", 1)[1]
        for line in vfreebusy.split("\r\n")
        if line.startswith("FREEBUSY:")
    ]


def has_busy_periods(vfreebusy: str) -> bool:
    return any(line.startswith("FREEBUSY:") for line in vfreebusy.split("\r\n"))


def normalized(vfreebusy: str) -> str:
    """The VFREEBUSY text with the volatile DTSTAMP line blanked out."""
    return re.sub(r"DTSTAMP:\d{8}T\d{6}Z", "DTSTAMP:<now>", vfreebusy)


# ---------------------------------------------------------------------------
# Response shape and VFREEBUSY text
# ---------------------------------------------------------------------------

def test_single_event_produces_canonical_vfreebusy_text():
    ics = cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z"))
    entry = one_calendar(ics)
    assert entry["calendar_id"] == "cal"
    assert normalized(entry["vfreebusy"]) == (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//freebusy-export//EN\r\n"
        "BEGIN:VFREEBUSY\r\n"
        "UID:cal\r\n"
        "DTSTAMP:<now>\r\n"
        "DTSTART:20260301T000000Z\r\n"
        "DTEND:20260401T000000Z\r\n"
        "FREEBUSY:20260309T100000Z/20260309T110000Z\r\n"
        "END:VFREEBUSY\r\n"
        "END:VCALENDAR\r\n"
    )


def test_dtstamp_is_a_utc_timestamp():
    entry = one_calendar(cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z")))
    stamp = re.search(r"^DTSTAMP:([^\r]+)\r$", entry["vfreebusy"], re.MULTILINE)
    assert stamp is not None
    assert re.fullmatch(r"\d{8}T\d{6}Z", stamp.group(1))


def test_window_is_echoed_in_utc():
    resp = freebusy(
        [], window_start="2026-03-01T02:00:00+02:00", window_end="2026-04-01T00:00:00Z"
    )
    assert resp.status_code == 200
    assert resp.json()["window_start"] == "2026-03-01T00:00:00Z"
    assert resp.json()["window_end"] == "2026-04-01T00:00:00Z"


def test_empty_calendar_list_is_allowed():
    resp = freebusy([])
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["calendars"] == []


def test_calendar_without_busy_events_has_no_freebusy_lines():
    entry = one_calendar(cal())
    assert not has_busy_periods(entry["vfreebusy"])


def test_calendars_are_sorted_by_calendar_id():
    ics = cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z"))
    resp = freebusy([
        {"calendar_id": "zoe", "ics": ics, "timezone": "UTC"},
        {"calendar_id": "amy", "ics": ics, "timezone": "UTC"},
    ])
    assert resp.status_code == 200
    assert [c["calendar_id"] for c in resp.json()["calendars"]] == ["amy", "zoe"]
    for entry in resp.json()["calendars"]:
        assert f"UID:{entry['calendar_id']}" in entry["vfreebusy"]


def test_calendar_id_is_escaped_in_the_uid_line():
    entry = one_calendar(
        cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z")),
        calendar_id="team;a,b",
    )
    assert "UID:team\\;a\\,b" in entry["vfreebusy"]


# ---------------------------------------------------------------------------
# Clipping and merging
# ---------------------------------------------------------------------------

def test_busy_intervals_are_clipped_to_the_window():
    ics = cal(
        event_utc("before", "20260228T220000Z", "20260301T020000Z"),
        event_utc("after", "20260331T230000Z", "20260401T020000Z"),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260301T000000Z/20260301T020000Z",
        "20260331T230000Z/20260401T000000Z",
    ]


def test_overlapping_and_adjacent_intervals_merge():
    ics = cal(
        event_utc("a", "20260309T090000Z", "20260309T100000Z"),
        event_utc("b", "20260309T093000Z", "20260309T103000Z"),
        # Adjacent: touches the previous interval at 10:30.
        event_utc("c", "20260309T103000Z", "20260309T110000Z"),
        # Disjoint: stays a separate FREEBUSY line.
        event_utc("d", "20260309T120000Z", "20260309T130000Z"),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260309T090000Z/20260309T110000Z",
        "20260309T120000Z/20260309T130000Z",
    ]


def test_freebusy_lines_are_sorted_by_start():
    ics = cal(
        event_utc("late", "20260320T100000Z", "20260320T110000Z"),
        event_utc("early", "20260302T100000Z", "20260302T110000Z"),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260302T100000Z/20260302T110000Z",
        "20260320T100000Z/20260320T110000Z",
    ]


def test_zero_duration_event_occupies_no_busy_time():
    ics = cal(event("UID:instant", "DTSTART:20260309T100000Z"))
    entry = one_calendar(ics)
    assert not has_busy_periods(entry["vfreebusy"])


def test_recurring_series_expands_each_occurrence():
    ics = cal(event_utc(
        "weekly", "20260302T090000Z", "20260302T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260302T090000Z/20260302T100000Z",
        "20260309T090000Z/20260309T100000Z",
        "20260316T090000Z/20260316T100000Z",
    ]


# ---------------------------------------------------------------------------
# STATUS:CANCELLED
# ---------------------------------------------------------------------------

def test_cancelled_master_suppresses_the_whole_series():
    ics = cal(event_utc(
        "weekly", "20260302T090000Z", "20260302T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "STATUS:CANCELLED",
    ))
    entry = one_calendar(ics)
    assert not has_busy_periods(entry["vfreebusy"])


def test_cancelled_override_removes_only_its_occurrence():
    ics = cal(
        event_utc(
            "weekly", "20260302T090000Z", "20260302T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
        ),
        event(
            "UID:weekly",
            "RECURRENCE-ID:20260309T090000Z",
            "DTSTART:20260309T090000Z",
            "DTEND:20260309T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260302T090000Z/20260302T100000Z",
    ]


# ---------------------------------------------------------------------------
# TRANSP inheritance
# ---------------------------------------------------------------------------

def test_transparent_event_does_not_block_time():
    ics = cal(event_utc(
        "e1", "20260309T100000Z", "20260309T110000Z", "TRANSP:TRANSPARENT",
    ))
    entry = one_calendar(ics)
    assert not has_busy_periods(entry["vfreebusy"])


def test_recurring_instances_inherit_master_transp():
    ics = cal(event_utc(
        "weekly", "20260302T090000Z", "20260302T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=2",
        "TRANSP:TRANSPARENT",
    ))
    entry = one_calendar(ics)
    assert not has_busy_periods(entry["vfreebusy"])


def test_override_with_explicit_transp_replaces_the_inherited_value():
    # Master is TRANSPARENT; the March 9 override declares OPAQUE, so only
    # that occurrence blocks time.
    ics = cal(
        event_utc(
            "weekly", "20260302T090000Z", "20260302T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
            "TRANSP:TRANSPARENT",
        ),
        event(
            "UID:weekly",
            "RECURRENCE-ID:20260309T090000Z",
            "DTSTART:20260309T090000Z",
            "DTEND:20260309T100000Z",
            "TRANSP:OPAQUE",
        ),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260309T090000Z/20260309T100000Z",
    ]


def test_override_with_explicit_transparent_frees_an_opaque_series():
    ics = cal(
        event_utc(
            "weekly", "20260302T090000Z", "20260302T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
        ),
        event(
            "UID:weekly",
            "RECURRENCE-ID:20260309T090000Z",
            "DTSTART:20260309T090000Z",
            "DTEND:20260309T100000Z",
            "TRANSP:TRANSPARENT",
        ),
    )
    entry = one_calendar(ics)
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260302T090000Z/20260302T100000Z",
    ]


def test_override_without_transp_keeps_inheriting_the_master():
    # The override moves the occurrence but declares no TRANSP, so the
    # master's TRANSPARENT still applies and nothing is busy.
    ics = cal(
        event_utc(
            "weekly", "20260302T090000Z", "20260302T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
            "TRANSP:TRANSPARENT",
        ),
        event(
            "UID:weekly",
            "RECURRENCE-ID:20260309T090000Z",
            "DTSTART:20260310T090000Z",
            "DTEND:20260310T100000Z",
        ),
    )
    entry = one_calendar(ics)
    assert not has_busy_periods(entry["vfreebusy"])


# ---------------------------------------------------------------------------
# All-day DATE events and the calendar timezone
# ---------------------------------------------------------------------------

def test_all_day_event_uses_calendar_timezone_local_days():
    # 2026-03-08 is the US spring-forward day: the local day runs
    # 05:00Z to 04:00Z the next day (23 hours), not a fixed 24.
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260308",
        "DTEND;VALUE=DATE:20260309",
    ))
    entry = one_calendar(ics, timezone="America/New_York")
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260308T050000Z/20260309T040000Z",
    ]


def test_all_day_event_on_fall_back_day_spans_25_hours():
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20261101",
        "DTEND;VALUE=DATE:20261102",
    ))
    entry = one_calendar(
        ics,
        timezone="America/New_York",
        window_start="2026-10-01T00:00:00Z",
        window_end="2026-12-01T00:00:00Z",
    )
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20261101T040000Z/20261102T050000Z",
    ]


def test_multi_day_all_day_event_spans_local_midnights():
    # Two authored dates (Mar 7-8) around the spring forward: 47 UTC hours.
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260307",
        "DTEND;VALUE=DATE:20260309",
    ))
    entry = one_calendar(ics, timezone="America/New_York")
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260307T050000Z/20260309T040000Z",
    ]


def test_all_day_event_without_dtend_lasts_one_local_day():
    ics = cal(event("UID:allday", "DTSTART;VALUE=DATE:20260310"))
    entry = one_calendar(ics, timezone="Asia/Tokyo")
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260309T150000Z/20260310T150000Z",
    ]


def test_recurring_all_day_series_expands_per_local_day():
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260307",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    entry = one_calendar(ics, timezone="America/New_York")
    # Three consecutive local days merge into one 71-hour interval (the
    # middle day is the 23-hour spring-forward day).
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260307T050000Z/20260310T040000Z",
    ]


def test_all_day_event_is_clipped_to_the_window():
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260308",
        "DTEND;VALUE=DATE:20260309",
    ))
    entry = one_calendar(
        ics,
        timezone="America/New_York",
        window_start="2026-03-08T12:00:00Z",
        window_end="2026-04-01T00:00:00Z",
    )
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260308T120000Z/20260309T040000Z",
    ]


def test_all_day_local_span_reaching_into_the_window_is_not_missed():
    # In Tokyo the Mar 8 local day is 2026-03-07T15:00Z..2026-03-08T15:00Z,
    # while its floating-midnight UTC span starts at 2026-03-08T00:00Z. A
    # window touching only the local span must still see the event.
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260308",
        "DTEND;VALUE=DATE:20260309",
    ))
    entry = one_calendar(
        ics,
        timezone="Asia/Tokyo",
        window_start="2026-03-07T16:00:00Z",
        window_end="2026-03-07T17:00:00Z",
    )
    assert freebusy_lines(entry["vfreebusy"]) == [
        "20260307T160000Z/20260307T170000Z",
    ]


def test_transparent_all_day_event_does_not_block_time():
    ics = cal(event(
        "UID:allday",
        "DTSTART;VALUE=DATE:20260308",
        "TRANSP:TRANSPARENT",
    ))
    entry = one_calendar(ics, timezone="America/New_York")
    assert not has_busy_periods(entry["vfreebusy"])


# ---------------------------------------------------------------------------
# 422 handling
# ---------------------------------------------------------------------------

def test_window_start_must_be_before_window_end():
    resp = freebusy([], window_start="2026-04-01T00:00:00Z",
                    window_end="2026-03-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start must be before window_end" in resp.json()["detail"]


def test_unknown_timezone_is_422():
    resp = freebusy([{
        "calendar_id": "cal",
        "ics": cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z")),
        "timezone": "Mars/Olympus",
    }])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_unparsable_ics_is_422():
    resp = freebusy([{
        "calendar_id": "cal",
        "ics": "this is not icalendar",
        "timezone": "UTC",
    }])
    assert resp.status_code == 422


def test_invalid_rrule_is_422():
    ics = cal(event_utc(
        "e1", "20260309T100000Z", "20260309T110000Z", "RRULE:FREQ=BOGUS",
    ))
    resp = freebusy([{"calendar_id": "cal", "ics": ics, "timezone": "UTC"}])
    assert resp.status_code == 422


def test_unknown_tzid_is_422():
    ics = cal(event(
        "UID:e1",
        "DTSTART;TZID=Mars/Olympus:20260309T100000",
        "DTEND;TZID=Mars/Olympus:20260309T110000",
    ))
    resp = freebusy([{"calendar_id": "cal", "ics": ics, "timezone": "UTC"}])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_duplicate_calendar_id_is_422():
    ics = cal(event_utc("e1", "20260309T100000Z", "20260309T110000Z"))
    resp = freebusy([
        {"calendar_id": "cal", "ics": ics, "timezone": "UTC"},
        {"calendar_id": "cal", "ics": ics, "timezone": "UTC"},
    ])
    assert resp.status_code == 422
    assert "duplicate calendar_id" in resp.json()["detail"]


def test_unknown_transp_value_is_422():
    ics = cal(event_utc(
        "e1", "20260309T100000Z", "20260309T110000Z", "TRANSP:BOGUS",
    ))
    resp = freebusy([{"calendar_id": "cal", "ics": ics, "timezone": "UTC"}])
    assert resp.status_code == 422
    assert "TRANSP" in resp.json()["detail"]


def test_missing_required_fields_are_422():
    resp = client.post("/freebusy-export", json={"calendars": []})
    assert resp.status_code == 422
    resp = freebusy([{"calendar_id": "cal", "timezone": "UTC"}])
    assert resp.status_code == 422
    resp = freebusy([{"calendar_id": "", "ics": cal(), "timezone": "UTC"}])
    assert resp.status_code == 422

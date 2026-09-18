"""Key tests for the /expand endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//expand-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def expand(ics: str, window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z"):
    return client.post(
        "/expand",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def starts(response) -> list[str]:
    return [i["start"] for i in response.json()["instances"]]


# ---------------------------------------------------------------------------
# Basic expansion: DTSTART / RRULE / RDATE / EXDATE
# ---------------------------------------------------------------------------

def test_daily_rrule_expands_within_window():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "SUMMARY:standup",
    ))
    resp = expand(ics)
    assert resp.status_code == 200
    assert starts(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    inst = resp.json()["instances"][0]
    assert inst["uid"] == "evt-1"
    assert inst["end"] == "2026-01-05T10:00:00Z"
    assert inst["summary"] == "standup"
    assert inst["recurrence_id"] == inst["start"]


def test_window_bounds_are_respected():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=10",
    ))
    resp = expand(ics, "2026-01-07T00:00:00Z", "2026-01-10T00:00:00Z")
    assert starts(resp) == [
        "2026-01-07T09:00:00Z",
        "2026-01-08T09:00:00Z",
        "2026-01-09T09:00:00Z",
    ]


def test_exdate_removes_occurrence():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000Z",
    ))
    resp = expand(ics)
    assert starts(resp) == ["2026-01-05T09:00:00Z", "2026-01-07T09:00:00Z"]


def test_rdate_adds_occurrence_and_dtstart_is_kept():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RDATE:20260120T090000Z",
        "RDATE:20260121T090000Z,20260122T090000Z",
    ))
    resp = expand(ics)
    assert starts(resp) == [
        "2026-01-05T09:00:00Z",  # DTSTART itself belongs to the recurrence set
        "2026-01-20T09:00:00Z",
        "2026-01-21T09:00:00Z",
        "2026-01-22T09:00:00Z",
    ]


def test_event_overlapping_window_start_is_included():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260109T230000Z",
        "DTEND:20260110T010000Z",
    ))
    resp = expand(ics, "2026-01-10T00:00:00Z", "2026-01-20T00:00:00Z")
    assert starts(resp) == ["2026-01-09T23:00:00Z"]


def test_recurring_event_overlapping_window_start_is_included():
    # 3-hour event starting before the window but ending inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260104T220000Z",
        "DTEND:20260105T010000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ))
    resp = expand(ics, "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z")
    assert starts(resp) == ["2026-01-04T22:00:00Z", "2026-01-05T22:00:00Z"]


def test_floating_time_is_interpreted_as_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000",  # no Z, no TZID
        "DURATION:PT1H",
    ))
    resp = expand(ics)
    assert resp.status_code == 200
    assert starts(resp) == ["2026-01-05T09:00:00Z"]
    assert resp.json()["instances"][0]["end"] == "2026-01-05T10:00:00Z"


# ---------------------------------------------------------------------------
# Overrides: UID + RECURRENCE-ID merge, reschedule, cancel
# ---------------------------------------------------------------------------

def weekly_with_override(*override_lines: str) -> str:
    master = event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",  # Mondays 09:00 UTC
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",  # Jan 5, 12, 19
        "SUMMARY:weekly sync",
    )
    override = event("UID:evt-1", *override_lines)
    return cal(master, override)


def test_override_reschedules_instance_and_drops_original():
    ics = weekly_with_override(
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260113T100000Z",
        "DTEND:20260113T110000Z",
        "SUMMARY:weekly sync (moved)",
    )
    resp = expand(ics)
    assert resp.status_code == 200
    # Original Jan 12 09:00 slot is gone; the instance lives at its new time.
    assert starts(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-13T10:00:00Z",
        "2026-01-19T09:00:00Z",
    ]
    moved = resp.json()["instances"][1]
    assert moved["recurrence_id"] == "2026-01-12T09:00:00Z"
    assert moved["end"] == "2026-01-13T11:00:00Z"
    assert moved["summary"] == "weekly sync (moved)"


def test_override_cancelled_removes_instance():
    ics = weekly_with_override(
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260112T090000Z",
        "DTEND:20260112T100000Z",
        "STATUS:CANCELLED",
    )
    resp = expand(ics)
    assert starts(resp) == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_override_moves_instance_into_window_from_outside():
    # March occurrence (outside the January window) moved into the window.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=10",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    resp = expand(ics, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert resp.status_code == 200
    assert starts(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
        "2026-01-15T12:00:00Z",  # moved instance, judged by its actual time
        "2026-01-19T09:00:00Z",
        "2026-01-26T09:00:00Z",
    ]


def test_override_moves_instance_out_of_window():
    # January occurrence moved to March: neither old nor new slot appears.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260310T090000Z",
            "DTEND:20260310T100000Z",
        ),
    )
    resp = expand(ics)
    assert starts(resp) == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_override_with_tzid_recurrence_id_matches():
    # RECURRENCE-ID given in local time must match the UTC occurrence.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=Europe/Berlin:20260106T100000",  # = 09:00Z
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
        ),
    )
    resp = expand(ics)
    assert starts(resp) == ["2026-01-05T09:00:00Z", "2026-01-07T15:00:00Z"]


# ---------------------------------------------------------------------------
# Timezones and DST
# ---------------------------------------------------------------------------

def test_tzid_dst_transition_shifts_utc_times():
    # America/New_York: EST (UTC-5) until 2026-03-08, then EDT (UTC-4).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260301T090000",
        "DTEND;TZID=America/New_York:20260301T100000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    resp = expand(ics, "2026-02-25T00:00:00Z", "2026-04-01T00:00:00Z")
    assert resp.status_code == 200
    assert starts(resp) == [
        "2026-03-01T14:00:00Z",  # EST
        "2026-03-08T13:00:00Z",  # EDT (DST began 02:00 local)
        "2026-03-15T13:00:00Z",  # EDT
    ]
    assert resp.json()["instances"][0]["end"] == "2026-03-01T15:00:00Z"


def test_unknown_tzid_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp = expand(ics)
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]
    assert "Mars/Olympus_Mons" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_instances_sorted_by_utc_start_then_uid():
    ics = cal(
        event("UID:b-event", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:a-event", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:c-event", "DTSTART:20260104T090000Z", "DTEND:20260104T100000Z"),
    )
    resp = expand(ics)
    got = [(i["start"], i["uid"]) for i in resp.json()["instances"]]
    assert got == [
        ("2026-01-04T09:00:00Z", "c-event"),
        ("2026-01-05T09:00:00Z", "a-event"),
        ("2026-01-05T09:00:00Z", "b-event"),
    ]


# ---------------------------------------------------------------------------
# Error handling: 422 with clear messages
# ---------------------------------------------------------------------------

def test_invalid_rrule_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "RRULE:FREQ=BOGUS;COUNT=2",
    ))
    resp = expand(ics)
    assert resp.status_code == 422
    assert "invalid RRULE" in resp.json()["detail"]


def test_invalid_window_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = expand(ics, "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_unparseable_ics_returns_422():
    resp = expand("this is not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_missing_dtstart_returns_422():
    ics = cal(event("UID:evt-1", "RRULE:FREQ=DAILY;COUNT=2"))
    resp = expand(ics)
    assert resp.status_code == 422
    assert "DTSTART" in resp.json()["detail"]


def test_thisandfuture_range_returns_422():
    ics = weekly_with_override(
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260112T090000Z",
        "DTSTART:20260112T100000Z",
    )
    resp = expand(ics)
    assert resp.status_code == 422
    assert "THISANDFUTURE" in resp.json()["detail"]


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

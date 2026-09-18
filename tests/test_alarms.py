"""Key tests for the /alarms endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//alarms-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def alarm(*lines: str) -> str:
    return "BEGIN:VALARM\r\n" + "".join(line + "\r\n" for line in lines) + "END:VALARM\r\n"


def alarms(ics: str, window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z"):
    return client.post(
        "/alarms",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def rows(response):
    return [
        (a["trigger"], a["uid"], a["recurrence_id"], a["description"])
        for a in response.json()["alarms"]
    ]


DISPLAY = ("ACTION:DISPLAY", "DESCRIPTION:Reminder")


# ---------------------------------------------------------------------------
# Basic triggers
# ---------------------------------------------------------------------------

def test_relative_trigger_before_dtstart():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:standup",
        alarm("TRIGGER:-PT15M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    item = resp.json()["alarms"][0]
    assert item == {
        "uid": "evt-1",
        "recurrence_id": None,
        "summary": "standup",
        "trigger": "2026-01-05T08:45:00Z",
        "description": "Reminder",
    }


def test_relative_trigger_anchored_to_dtend_positive_delta():
    # +10 minutes after the end.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;RELATED=END:PT10M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T10:10:00Z", "evt-1", None, "Reminder")]


def test_relative_trigger_before_dtend():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;RELATED=END:-PT1H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T09:00:00Z", "evt-1", None, "Reminder")]


def test_absolute_trigger_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;VALUE=DATE-TIME:20260105T073000Z", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T07:30:00Z", "evt-1", None, "Reminder")]


def test_absolute_trigger_with_tzid_is_converted_to_utc():
    # 08:00 America/New_York on 2026-01-05 is EST (UTC-5) = 13:00Z.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260105T090000",
        "DTEND;TZID=America/New_York:20260105T100000",
        alarm(
            "TRIGGER;VALUE=DATE-TIME;TZID=America/New_York:20260105T080000",
            *DISPLAY,
        ),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T13:00:00Z", "evt-1", None, "Reminder")]


def test_floating_absolute_trigger_is_assumed_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;VALUE=DATE-TIME:20260105T080000", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T08:00:00Z", "evt-1", None, "Reminder")]


# ---------------------------------------------------------------------------
# REPEAT / DURATION
# ---------------------------------------------------------------------------

def test_repeat_generates_spaced_reminders():
    # Initial trigger at -15m, then two more every 5 minutes: 08:45, 08:50,
    # 08:55 before a 09:00 start.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT15M", "REPEAT:2", "DURATION:PT5M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:45:00Z",
        "2026-01-05T08:50:00Z",
        "2026-01-05T08:55:00Z",
    ]


def test_repeat_zero_still_fires_once():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT15M", "REPEAT:0", "DURATION:PT5M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:45:00Z",
    ]


def test_repeat_works_with_absolute_trigger():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm(
            "TRIGGER;VALUE=DATE-TIME:20260105T080000Z",
            "REPEAT:2",
            "DURATION:PT10M",
            *DISPLAY,
        ),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",
        "2026-01-05T08:10:00Z",
        "2026-01-05T08:20:00Z",
    ]


# ---------------------------------------------------------------------------
# Recurring events and overrides
# ---------------------------------------------------------------------------

def weekly_master(extra: str = "") -> str:
    return event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",  # Mondays Jan 5/12/19
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "SUMMARY:weekly sync",
        alarm("TRIGGER:-PT1H", *DISPLAY),
        extra,
    )


def test_master_alarm_applies_to_every_expanded_instance():
    ics = cal(weekly_master())
    resp = alarms(ics)
    assert rows(resp) == [
        ("2026-01-05T08:00:00Z", "evt-1", "2026-01-05T09:00:00Z", "Reminder"),
        ("2026-01-12T08:00:00Z", "evt-1", "2026-01-12T09:00:00Z", "Reminder"),
        ("2026-01-19T08:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
    ]


def test_override_uses_its_own_alarm_not_the_masters():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "SUMMARY:weekly sync (moved)",
            alarm("TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:Override ping"),
        ),
    )
    resp = alarms(ics)
    assert rows(resp) == [
        ("2026-01-05T08:00:00Z", "evt-1", "2026-01-05T09:00:00Z", "Reminder"),
        ("2026-01-12T10:55:00Z", "evt-1", "2026-01-12T09:00:00Z", "Override ping"),
        ("2026-01-19T08:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
    ]


def test_override_without_valarm_alarm_nobody():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
        ),
    )
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_cancelled_override_produces_no_reminders():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_exdated_occurrence_has_no_reminder():
    ics = cal(weekly_master("EXDATE:20260112T090000Z"))
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_non_recurring_event_alarm_has_null_recurrence_id():
    ics = cal(event(
        "UID:oneoff",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.json()["alarms"][0]["recurrence_id"] is None


# ---------------------------------------------------------------------------
# Window filtering (half-open)
# ---------------------------------------------------------------------------

def test_only_triggers_inside_window_are_returned():
    # Event starts inside the window; reminders straddle both edges.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm(
            "TRIGGER;VALUE=DATE-TIME:20260105T070000Z",
            "REPEAT:4",
            "DURATION:PT1H",
            *DISPLAY,
        ),  # 07:00, 08:00, 09:00, 10:00, 11:00
    ))
    resp = alarms(
        ics,
        window_start="2026-01-05T08:00:00Z",
        window_end="2026-01-05T11:00:00Z",
    )
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",  # == window_start: included
        "2026-01-05T09:00:00Z",
        "2026-01-05T10:00:00Z",
        # 11:00 == window_end: excluded
    ]


def test_event_with_alarms_all_outside_window_returns_nothing():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", *DISPLAY),
    ))
    resp = alarms(
        ics,
        window_start="2026-03-01T00:00:00Z",
        window_end="2026-04-01T00:00:00Z",
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-04-01T00:00:00Z",
        "count": 0,
        "alarms": [],
    }


def test_calendar_without_alarms_returns_empty_list():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:no alarm here",
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["alarms"] == []


def test_multiple_alarms_on_one_instance_all_returned():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", "ACTION:DISPLAY", "DESCRIPTION:one hour before"),
        alarm("TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:five minutes before"),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",
        "2026-01-05T08:55:00Z",
    ]


# ---------------------------------------------------------------------------
# Sorting: trigger time, then UID, then RECURRENCE-ID
# ---------------------------------------------------------------------------

def test_sorted_by_trigger_then_uid():
    ics = cal(
        event(
            "UID:b-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:a-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:c-event",
            "DTSTART:20260104T090000Z",
            "DTEND:20260104T100000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
    )
    resp = alarms(ics)
    got = [(a["trigger"], a["uid"]) for a in resp.json()["alarms"]]
    assert got == [
        ("2026-01-04T08:00:00Z", "c-event"),
        ("2026-01-05T09:00:00Z", "a-event"),
        ("2026-01-05T09:00:00Z", "b-event"),
    ]


def test_same_trigger_sorted_by_recurrence_id():
    # Two recurring series with an identical trigger instant; the UID then
    # RECURRENCE-ID tiebreak keeps the order stable.
    ics = cal(
        event(
            "UID:z-series",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:a-series",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
            alarm("TRIGGER:-PT2H", *DISPLAY),
        ),
    )
    resp = alarms(ics)
    got = [(a["trigger"], a["uid"], a["recurrence_id"]) for a in resp.json()["alarms"]]
    assert got == [
        ("2026-01-05T08:00:00Z", "a-series", "2026-01-05T10:00:00Z"),
        ("2026-01-05T08:00:00Z", "z-series", "2026-01-05T09:00:00Z"),
        ("2026-01-06T08:00:00Z", "a-series", "2026-01-06T10:00:00Z"),
        ("2026-01-06T08:00:00Z", "z-series", "2026-01-06T09:00:00Z"),
    ]


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def post_422(alarm_lines: str, *, event_lines: tuple[str, ...] = ()):
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        *event_lines,
        alarm_lines,
    ))
    resp = alarms(ics)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def test_unsupported_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:EMAIL"))
    assert "ACTION" in detail and "DISPLAY" in detail


def test_audio_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:AUDIO"))
    assert "unsupported VALARM ACTION" in detail


def test_missing_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "DESCRIPTION:ping"))
    assert "ACTION" in detail


def test_display_without_description_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:DISPLAY"))
    assert "DESCRIPTION" in detail


def test_missing_trigger_returns_422():
    detail = post_422(alarm("ACTION:DISPLAY", "DESCRIPTION:ping"))
    assert "TRIGGER" in detail


def test_unsupported_related_value_returns_422():
    detail = post_422(
        alarm("TRIGGER;RELATED=MIDDLE:PT10M", *DISPLAY)
    )
    assert "RELATED" in detail


def test_related_on_absolute_trigger_returns_422():
    detail = post_422(
        alarm("TRIGGER;VALUE=DATE-TIME;RELATED=START:20260105T080000Z", *DISPLAY)
    )
    assert "RELATED" in detail


def test_repeat_without_duration_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "REPEAT:2", *DISPLAY))
    assert "DURATION" in detail


def test_duration_without_repeat_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "DURATION:PT5M", *DISPLAY))
    assert "REPEAT" in detail


def test_negative_repeat_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:-1", "DURATION:PT5M", *DISPLAY)
    )
    assert "REPEAT" in detail


def test_non_positive_alarm_duration_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:2", "DURATION:PT0S", *DISPLAY)
    )
    assert "DURATION" in detail


def test_excessive_repeat_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:1001", "DURATION:PT1S", *DISPLAY)
    )
    assert "1000" in detail


def test_unknown_tzid_on_absolute_trigger_returns_422():
    detail = post_422(
        alarm(
            "TRIGGER;VALUE=DATE-TIME;TZID=Mars/Olympus_Mons:20260105T080000",
            *DISPLAY,
        )
    )
    assert "unknown timezone" in detail
    assert "Mars/Olympus_Mons" in detail


def test_invalid_alarm_outside_window_still_returns_422():
    # Validation is eager for every VALARM in the calendar, even when its
    # event (and therefore its trigger) lies outside the query window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260305T090000Z",
        "DTEND:20260305T100000Z",
        alarm("TRIGGER:-PT15M", "ACTION:EMAIL"),
    ))
    resp = alarms(
        ics,
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-02-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "ACTION" in resp.json()["detail"]


def test_calendar_level_failures_still_return_422():
    # Unparseable ICS and invalid windows reuse the expander's validation.
    resp = alarms("not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]

    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        alarm("TRIGGER:-PT15M", *DISPLAY),
    ))
    resp = alarms(ics, "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]

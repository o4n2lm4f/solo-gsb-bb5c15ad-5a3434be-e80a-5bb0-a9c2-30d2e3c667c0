"""Key tests for the /validate endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//validate-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def validate(ics: str):
    return client.post("/validate", json={"ics": ics})


def codes(response) -> list[str]:
    return [i["code"] for i in response.json()["issues"]]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_calendar_is_valid():
    ics = cal(event(
        "UID:ok-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:Fine",
    ))
    resp = validate(ics)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "error_count": 0, "issues": []}


def test_master_and_override_is_valid():
    ics = cal(
        event(
            "UID:series-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
        ),
    )
    resp = validate(ics)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    assert resp.json()["issues"] == []


def test_empty_calendar_is_valid():
    resp = validate("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


def test_all_day_event_with_next_day_dtend_is_valid():
    ics = cal(event(
        "UID:day-1",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260106",
    ))
    resp = validate(ics)
    assert resp.json()["valid"] is True


# ---------------------------------------------------------------------------
# Parse failures stay 200 with a structured diagnostic
# ---------------------------------------------------------------------------


def test_garbage_returns_200_with_parse_error():
    resp = validate("this is not an iCalendar")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 1
    issue = body["issues"][0]
    assert issue["code"] == "parse_error"
    assert issue["event_index"] is None
    assert issue["uid"] is None
    assert "parse" in issue["message"].lower()


def test_empty_string_returns_parse_error():
    resp = validate("")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert codes(resp) == ["parse_error"]
    assert body["issues"][0]["event_index"] is None


def test_missing_ics_field_is_still_422():
    resp = client.post("/validate", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Per-event checks
# ---------------------------------------------------------------------------


def test_missing_uid():
    ics = cal(event(
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
    ))
    resp = validate(ics)
    body = resp.json()
    assert body["valid"] is False
    assert codes(resp) == ["missing_uid"]
    issue = body["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] is None


def test_missing_dtstart_reports_uid():
    ics = cal(event("UID:no-start", "DTEND:20260105T100000Z"))
    resp = validate(ics)
    assert codes(resp) == ["missing_dtstart"]
    issue = resp.json()["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] == "no-start"


def test_event_missing_uid_and_dtstart_accumulates_both():
    ics = cal(event("SUMMARY:broken"))
    resp = validate(ics)
    assert codes(resp) == ["missing_dtstart", "missing_uid"]
    for issue in resp.json()["issues"]:
        assert issue["event_index"] == 0
        assert issue["uid"] is None


def test_dtend_and_duration_are_mutually_exclusive():
    ics = cal(event(
        "UID:both-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "DURATION:PT60M",
    ))
    resp = validate(ics)
    assert codes(resp) == ["dtend_and_duration"]
    issue = resp.json()["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] == "both-1"


def test_end_equal_to_start_is_invalid():
    ics = cal(event(
        "UID:zero-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T090000Z",
    ))
    resp = validate(ics)
    assert codes(resp) == ["end_before_start"]
    issue = resp.json()["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] == "zero-1"


def test_end_before_start_is_invalid():
    ics = cal(event(
        "UID:back-1",
        "DTSTART:20260105T100000Z",
        "DURATION:-PT1H",
    ))
    resp = validate(ics)
    assert codes(resp) == ["end_before_start"]


def test_end_before_start_respects_timezones():
    # Berlin 10:00 on 2026-01-05 is 09:00Z; UTC end 08:30Z is earlier.
    ics = cal(event(
        "UID:tz-1",
        "DTSTART;TZID=Europe/Berlin:20260105T100000",
        "DTEND:20260105T083000Z",
    ))
    resp = validate(ics)
    assert codes(resp) == ["end_before_start"]


def test_cross_timezone_ordered_event_is_valid():
    # Berlin 10:00 (09:00Z) -> UTC 10:00Z is later.
    ics = cal(event(
        "UID:tz-2",
        "DTSTART;TZID=Europe/Berlin:20260105T100000",
        "DTEND:20260105T100000Z",
    ))
    resp = validate(ics)
    assert resp.json()["valid"] is True


def test_all_day_dtend_on_same_date_is_invalid():
    ics = cal(event(
        "UID:day-bad",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260105",
    ))
    resp = validate(ics)
    assert codes(resp) == ["end_before_start"]


def test_single_event_can_accumulate_multiple_issues():
    ics = cal(event(
        "DTEND:20260101T080000Z",
        "DURATION:PT2H",
    ))
    resp = validate(ics)
    # Missing UID and DTSTART, plus DTEND/DURATION conflict. The ordering
    # check cannot run without a start, so no end_before_start is emitted.
    assert codes(resp) == [
        "dtend_and_duration",
        "missing_dtstart",
        "missing_uid",
    ]
    assert "end_before_start" not in codes(resp)


# ---------------------------------------------------------------------------
# Cross-event checks
# ---------------------------------------------------------------------------


def test_duplicate_master_events_flag_each_master():
    ics = cal(
        event("UID:dup", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:dup", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
    )
    resp = validate(ics)
    issues = resp.json()["issues"]
    assert [i["code"] for i in issues] == ["duplicate_master_event"] * 2
    assert sorted(i["event_index"] for i in issues) == [0, 1]
    assert {i["uid"] for i in issues} == {"dup"}


def test_duplicate_masters_with_valid_override():
    ics = cal(
        event("UID:dup", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:dup", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
        event(
            "UID:dup",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
        ),
    )
    resp = validate(ics)
    # The override is not orphaned (a master exists); only the two masters
    # are flagged.
    assert codes(resp) == ["duplicate_master_event", "duplicate_master_event"]


def test_orphan_override_without_master():
    ics = cal(event(
        "UID:orphan-1",
        "RECURRENCE-ID:20260105T090000Z",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
    ))
    resp = validate(ics)
    assert codes(resp) == ["orphan_override"]
    issue = resp.json()["issues"][0]
    assert issue["event_index"] == 0
    assert issue["uid"] == "orphan-1"


def test_override_for_other_uid_does_not_satisfy_master():
    ics = cal(
        event("UID:a", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event(
            "UID:b",
            "RECURRENCE-ID:20260105T090000Z",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
        ),
    )
    resp = validate(ics)
    assert codes(resp) == ["orphan_override"]
    assert resp.json()["issues"][0]["event_index"] == 1


# ---------------------------------------------------------------------------
# Ordering and accumulation across events
# ---------------------------------------------------------------------------


def test_issues_sorted_by_event_index_then_code():
    # Event 1 (index 1) is bad first; event 0 is clean.
    ics = cal(
        event("UID:good", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("DTSTART:20260105T090000Z", "DTEND:20260105T080000Z"),
    )
    resp = validate(ics)
    issues = resp.json()["issues"]
    assert [i["event_index"] for i in issues] == [1, 1]
    assert [i["code"] for i in issues] == ["end_before_start", "missing_uid"]


def test_mixed_calendar_preserves_source_order_and_valid_flag():
    ics = cal(
        event("DTSTART:20260105T090000Z"),  # 0: missing uid, zero duration ok
        event("UID:fine", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
        event("UID:late", "DTSTART:20260107T120000Z", "DTEND:20260107T110000Z"),
    )
    resp = validate(ics)
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 2
    assert [(i["event_index"], i["code"]) for i in body["issues"]] == [
        (0, "missing_uid"),
        (2, "end_before_start"),
    ]


def test_error_count_matches_issues_and_valid_is_false():
    ics = cal(
        event("UID:e", "DTSTART:20260105T090000Z", "DTEND:20260105T090000Z"),
        event("UID:e", "DTSTART:20260106T090000Z", "DTEND:20260106T090000Z"),
    )
    body = validate(ics).json()
    assert body["valid"] is False
    assert body["error_count"] == len(body["issues"]) == 4

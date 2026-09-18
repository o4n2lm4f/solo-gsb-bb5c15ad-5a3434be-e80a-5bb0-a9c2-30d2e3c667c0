"""Key tests for the /attendance endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//attendance-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def attendance(ics: str, window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z"):
    return client.post(
        "/attendance",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def attendees_of(resp, index=0):
    return resp.json()["instances"][index]["attendees"]


# ---------------------------------------------------------------------------
# Basic parsing and defaults
# ---------------------------------------------------------------------------

def test_one_off_event_reports_attendees_with_defaults():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:bob@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "window_start": "2026-01-01T00:00:00Z",
        "window_end": "2026-02-01T00:00:00Z",
        "count": 1,
        "instances": [
            {
                "uid": "evt-1",
                "recurrence_id": None,
                "start": "2026-01-05T09:00:00Z",
                "end": "2026-01-05T10:00:00Z",
                "counts": {"NEEDS-ACTION": 1},
                "attendees": [
                    {
                        "address": "bob@example.com",
                        "partstat": "NEEDS-ACTION",
                        "role": "REQ-PARTICIPANT",
                    }
                ],
            }
        ],
    }


def test_partstat_and_role_passed_through_uppercased():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;partstat=accepted;role=chair:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=DELEGATED;ROLE=OPT-PARTICIPANT:mailto:bob@example.com",
    ))
    resp = attendance(ics)
    assert attendees_of(resp) == [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "CHAIR"},
        {"address": "bob@example.com", "partstat": "DELEGATED", "role": "OPT-PARTICIPANT"},
    ]
    assert resp.json()["instances"][0]["counts"] == {"ACCEPTED": 1, "DELEGATED": 1}


def test_counts_group_every_partstat():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:b@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:c@example.com",
        "ATTENDEE;PARTSTAT=TENTATIVE:mailto:d@example.com",
        "ATTENDEE;PARTSTAT=DELEGATED:mailto:e@example.com",
        "ATTENDEE:mailto:f@example.com",
    ))
    resp = attendance(ics)
    assert resp.json()["instances"][0]["counts"] == {
        "ACCEPTED": 2,
        "DECLINED": 1,
        "TENTATIVE": 1,
        "DELEGATED": 1,
        "NEEDS-ACTION": 1,
    }


def test_event_without_attendees_has_empty_list_and_counts():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:solo work",
    ))
    resp = attendance(ics)
    inst = resp.json()["instances"][0]
    assert inst["attendees"] == []
    assert inst["counts"] == {}


# ---------------------------------------------------------------------------
# Address normalization and stable sorting
# ---------------------------------------------------------------------------

def test_mailto_prefix_stripped_and_lowercased():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:Alice.Doe@EXAMPLE.COM",
    ))
    assert attendees_of(attendance(ics))[0]["address"] == "alice.doe@example.com"


def test_mailto_scheme_is_case_insensitive():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:MAILTO:Bob@Example.com",
    ))
    assert attendees_of(attendance(ics))[0]["address"] == "bob@example.com"


def test_non_mailto_uris_are_kept_verbatim():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:https://Example.com/Users/42",
        "ATTENDEE:urn:uuid:B3F4-ABCD",
        "ATTENDEE:tel:+1-555-0100",
    ))
    addresses = [a["address"] for a in attendees_of(attendance(ics))]
    assert addresses == [
        "https://Example.com/Users/42",
        "tel:+1-555-0100",
        "urn:uuid:B3F4-ABCD",
    ]


def test_attendees_sorted_stably_by_normalized_address():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:zoe@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:Alice@Example.com",
        "ATTENDEE;PARTSTAT=TENTATIVE:mailto:mike@example.com",
    ))
    addresses = [a["address"] for a in attendees_of(attendance(ics))]
    assert addresses == [
        "alice@example.com",
        "mike@example.com",
        "zoe@example.com",
    ]


def test_sorting_uses_normalized_not_raw_address():
    # "ZOE@example.com" normalizes to "zoe@example.com" and must sort after
    # the lowercase "alice" address.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:ZOE@example.com",
        "ATTENDEE:mailto:alice@example.com",
    ))
    addresses = [a["address"] for a in attendees_of(attendance(ics))]
    assert addresses == ["alice@example.com", "zoe@example.com"]


# ---------------------------------------------------------------------------
# Duplicate addresses within one effective list
# ---------------------------------------------------------------------------

def test_duplicate_address_with_same_params_collapses_to_one():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:A@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    assert attendees_of(resp) == [
        {"address": "a@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"}
    ]
    assert resp.json()["instances"][0]["counts"] == {"ACCEPTED": 1}


def test_duplicate_address_with_conflicting_partstat_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:A@Example.COM",
    ))
    resp = attendance(ics)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "a@example.com" in detail
    assert "conflicting" in detail


def test_duplicate_address_with_conflicting_role_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED;ROLE=CHAIR:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:a@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "conflicting" in resp.json()["detail"]


def test_explicit_default_params_match_omitted_ones():
    # Explicit NEEDS-ACTION/REQ-PARTICIPANT equals the defaults, so the
    # duplicate address is consistent and collapses.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:a@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    assert len(attendees_of(resp)) == 1


# ---------------------------------------------------------------------------
# Recurrence: inheritance, overrides, cancellations
# ---------------------------------------------------------------------------

def weekly_master() -> str:
    return event(
        "UID:series-1",
        "DTSTART:20260105T090000Z",  # Mondays Jan 5/12/19
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "SUMMARY:Weekly sync",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:bob@example.com",
    )


def test_recurring_instances_inherit_master_attendees():
    resp = attendance(cal(weekly_master()))
    assert resp.status_code == 200
    instances = resp.json()["instances"]
    assert [i["recurrence_id"] for i in instances] == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
        "2026-01-19T09:00:00Z",
    ]
    for inst in instances:
        assert inst["counts"] == {"ACCEPTED": 1, "DECLINED": 1}
        assert [a["address"] for a in inst["attendees"]] == [
            "alice@example.com",
            "bob@example.com",
        ]


def test_override_with_attendees_replaces_the_whole_list():
    ics = cal(
        weekly_master(),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "SUMMARY:Weekly sync (smaller group)",
            "ATTENDEE;PARTSTAT=TENTATIVE:mailto:carol@example.com",
            "ATTENDEE:mailto:dave@example.com",
        ),
    )
    resp = attendance(ics)
    instances = {i["recurrence_id"]: i for i in resp.json()["instances"]}
    moved = instances["2026-01-12T09:00:00Z"]
    assert moved["start"] == "2026-01-12T11:00:00Z"
    assert moved["end"] == "2026-01-12T12:00:00Z"
    assert moved["counts"] == {"TENTATIVE": 1, "NEEDS-ACTION": 1}
    assert [a["address"] for a in moved["attendees"]] == [
        "carol@example.com",
        "dave@example.com",
    ]
    # Untouched instances still inherit the master list.
    assert instances["2026-01-05T09:00:00Z"]["counts"] == {
        "ACCEPTED": 1,
        "DECLINED": 1,
    }
    assert instances["2026-01-19T09:00:00Z"]["counts"] == {
        "ACCEPTED": 1,
        "DECLINED": 1,
    }


def test_override_without_attendees_keeps_inheriting_master():
    ics = cal(
        weekly_master(),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "SUMMARY:Weekly sync (moved)",
        ),
    )
    resp = attendance(ics)
    instances = {i["recurrence_id"]: i for i in resp.json()["instances"]}
    moved = instances["2026-01-12T09:00:00Z"]
    assert moved["counts"] == {"ACCEPTED": 1, "DECLINED": 1}
    assert [a["address"] for a in moved["attendees"]] == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_override_with_empty_attendee_set_replaces_inheritance():
    # Declaring ATTENDEE at all means "whole group replacement", even when
    # the effective list ends up with one entry; here the replacement list
    # is just Dave.
    ics = cal(
        weekly_master(),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:dave@example.com",
        ),
    )
    resp = attendance(ics)
    instances = {i["recurrence_id"]: i for i in resp.json()["instances"]}
    assert instances["2026-01-12T09:00:00Z"]["attendees"] == [
        {"address": "dave@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"}
    ]


def test_cancelled_override_has_no_attendance():
    ics = cal(
        weekly_master(),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:dave@example.com",
        ),
    )
    resp = attendance(ics)
    rids = [i["recurrence_id"] for i in resp.json()["instances"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_exdated_occurrence_disappears():
    ics = cal(
        weekly_master().replace("RRULE:FREQ=WEEKLY;COUNT=3",
                                "RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE:20260112T090000Z")
    )
    resp = attendance(ics)
    rids = [i["recurrence_id"] for i in resp.json()["instances"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_inherited_master_list_conflict_is_validated_on_instances():
    # A master list conflict must surface even though no override is
    # involved; every expanded instance shares that list.
    ics = cal(event(
        "UID:series-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=2",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:a@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "conflicting" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Window handling and ordering
# ---------------------------------------------------------------------------

def test_instances_outside_window_are_omitted():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260305T090000Z",
        "DTEND:20260305T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
    ))
    resp = attendance(ics)
    assert resp.status_code == 200
    assert resp.json() == {
        "window_start": "2026-01-01T00:00:00Z",
        "window_end": "2026-02-01T00:00:00Z",
        "count": 0,
        "instances": [],
    }


def test_instance_overlapping_window_edge_is_included():
    # 08:30-09:30 overlaps the window starting 09:00 (half-open overlap).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T083000Z",
        "DTEND:20260105T093000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
    ))
    resp = attendance(
        ics,
        window_start="2026-01-05T09:00:00Z",
        window_end="2026-01-05T10:00:00Z",
    )
    assert resp.json()["count"] == 1


def test_instances_sorted_by_start_then_uid():
    ics = cal(
        event(
            "UID:b-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        ),
        event(
            "UID:a-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        ),
        event(
            "UID:c-event",
            "DTSTART:20260104T090000Z",
            "DTEND:20260104T100000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        ),
    )
    resp = attendance(ics)
    keys = [(i["start"], i["uid"]) for i in resp.json()["instances"]]
    assert keys == [
        ("2026-01-04T09:00:00Z", "c-event"),
        ("2026-01-05T10:00:00Z", "a-event"),
        ("2026-01-05T10:00:00Z", "b-event"),
    ]


def test_naive_window_timestamps_treated_as_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
    ))
    resp = attendance(
        ics,
        window_start="2026-01-05T09:00:00",
        window_end="2026-01-05T10:00:00",
    )
    assert resp.status_code == 200
    assert resp.json()["window_start"] == "2026-01-05T09:00:00Z"
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def test_inverted_window_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
    ))
    resp = attendance(ics, "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_unparsable_ics_returns_422():
    resp = attendance("not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_conflict_in_event_outside_window_still_returns_422():
    # Eager validation: every VEVENT's own ATTENDEE list is checked even
    # when none of its instances overlap the window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260305T090000Z",
        "DTEND:20260305T100000Z",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:a@example.com",
        "ATTENDEE;PARTSTAT=DECLINED:mailto:a@example.com",
    ))
    resp = attendance(
        ics,
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-02-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "conflicting" in resp.json()["detail"]


def test_conflict_in_override_list_is_validated():
    ics = cal(
        weekly_master(),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:dave@example.com",
            "ATTENDEE;PARTSTAT=TENTATIVE:mailto:DAVE@example.com",
        ),
    )
    resp = attendance(ics)
    assert resp.status_code == 422
    assert "dave@example.com" in resp.json()["detail"]

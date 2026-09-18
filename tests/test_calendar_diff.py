"""Key tests for the /calendar-diff endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"

WINDOW_START = "2026-01-01T00:00:00Z"
WINDOW_END = "2026-02-01T00:00:00Z"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def event_utc(uid: str, start: str, end: str, *extra: str) -> str:
    return event(f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}", *extra)


def diff(old_ics: str, new_ics: str, *, window_start=WINDOW_START, window_end=WINDOW_END):
    return client.post(
        "/calendar-diff",
        json={
            "old_ics": old_ics,
            "new_ics": new_ics,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def ids(entries):
    return [(e["uid"], e["recurrence_id"]) for e in entries]


# ---------------------------------------------------------------------------
# Added / removed / unchanged
# ---------------------------------------------------------------------------

def test_added_removed_and_unchanged_non_recurring():
    old = cal(
        event_utc("keep", "20260105T090000Z", "20260105T100000Z", "SUMMARY:same"),
        event_utc("gone", "20260106T090000Z", "20260106T100000Z"),
    )
    new = cal(
        event_utc("keep", "20260105T090000Z", "20260105T100000Z", "SUMMARY:same"),
        event_utc("born", "20260107T090000Z", "20260107T100000Z"),
    )
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    assert ids(body["added"]) == [("born", None)]
    assert ids(body["removed"]) == [("gone", None)]
    assert body["modified"] == []


def test_identical_calendars_return_empty_lists():
    ics = cal(event_utc("only", "20260105T090000Z", "20260105T100000Z",
                        "SUMMARY:x", "STATUS:CONFIRMED"))
    resp = diff(ics, ics)
    assert resp.status_code == 200
    assert resp.json() == {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "added": [],
        "removed": [],
        "modified": [],
    }


# ---------------------------------------------------------------------------
# Modified: start / end / summary / status
# ---------------------------------------------------------------------------

def test_modified_start_end_summary_status_each_detected():
    old = cal(
        event_utc("t", "20260105T090000Z", "20260105T100000Z", "SUMMARY:A"),
        event_utc("e", "20260106T090000Z", "20260106T100000Z", "SUMMARY:B"),
        event_utc("s", "20260107T090000Z", "20260107T100000Z", "SUMMARY:C"),
        event_utc("x", "20260108T090000Z", "20260108T100000Z",
                  "SUMMARY:D", "STATUS:CONFIRMED"),
    )
    new = cal(
        event_utc("t", "20260105T110000Z", "20260105T120000Z", "SUMMARY:A"),
        event_utc("e", "20260106T090000Z", "20260106T103000Z", "SUMMARY:B"),
        event_utc("s", "20260107T090000Z", "20260107T100000Z", "SUMMARY:C2"),
        event_utc("x", "20260108T090000Z", "20260108T100000Z",
                  "SUMMARY:D", "STATUS:TENTATIVE"),
    )
    resp = diff(old, new)
    assert resp.status_code == 200
    assert [m["after"]["uid"] for m in resp.json()["modified"]] == [
        "e", "s", "t", "x"  # sorted by identity (uid)
    ]


def test_status_change_to_cancelled_on_non_recurring_is_removed():
    # A non-recurring event with STATUS:CANCELLED expands to nothing, so the
    # identity disappears from the new side (same semantics as an /expand).
    old = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z"))
    new = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z",
                        "STATUS:CANCELLED"))
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    assert ids(body["removed"]) == [("u", None)]
    assert body["added"] == []
    assert body["modified"] == []


def test_modified_reports_before_and_after():
    old = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z", "SUMMARY:old"))
    new = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z", "SUMMARY:new"))
    resp = diff(old, new)
    assert resp.status_code == 200
    changed = resp.json()["modified"]
    assert len(changed) == 1
    assert changed[0]["before"]["summary"] == "old"
    assert changed[0]["after"]["summary"] == "new"
    # Identity fields are echoed on both sides.
    for side in ("before", "after"):
        assert changed[0][side]["uid"] == "u"
        assert changed[0][side]["recurrence_id"] is None


# ---------------------------------------------------------------------------
# Recurring instances: identity is UID + RECURRENCE-ID
# ---------------------------------------------------------------------------

def test_recurring_instances_keyed_by_uid_and_recurrence_id():
    old = cal(event(
        "UID:r",
        "DTSTART:20260105T090000Z",  # Mondays
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",  # Jan 5, 12, 19
        "SUMMARY:weekly",
    ))
    new = cal(
        event(
            "UID:r",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
            "SUMMARY:weekly",
        ),
        # Jan 12 instance rescheduled to Jan 13, identity unchanged.
        event(
            "UID:r",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260113T150000Z",
            "DTEND:20260113T160000Z",
            "SUMMARY:weekly moved",
        ),
    )
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    rid = "2026-01-12T09:00:00Z"
    assert body["added"] == []
    assert body["removed"] == []
    assert len(body["modified"]) == 1
    changed = body["modified"][0]
    assert (changed["before"]["uid"], changed["before"]["recurrence_id"]) == ("r", rid)
    assert (changed["after"]["uid"], changed["after"]["recurrence_id"]) == ("r", rid)
    assert changed["before"]["start"] == "2026-01-12T09:00:00Z"
    assert changed["after"]["start"] == "2026-01-13T15:00:00Z"


def test_new_recurring_occurrence_is_added_by_identity():
    old = cal(event(
        "UID:r",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=2",  # Jan 5, 12
    ))
    new = cal(event(
        "UID:r",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",  # Jan 5, 12, 19
    ))
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    assert ids(body["added"]) == [("r", "2026-01-19T09:00:00Z")]
    assert body["removed"] == []
    assert body["modified"] == []


def test_cancelled_override_makes_instance_removed():
    master = event(
        "UID:r",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=2",
    )
    old = cal(master)
    new = cal(
        master,
        event(
            "UID:r",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    assert ids(body["removed"]) == [("r", "2026-01-12T09:00:00Z")]
    assert body["added"] == []
    assert body["modified"] == []


def test_non_recurring_replaced_by_recurring_same_uid():
    # Old version: a single non-recurring event (identity has no rid).
    old = cal(event_utc("mix", "20260105T090000Z", "20260105T100000Z",
                        "SUMMARY:single"))
    # New version: the same UID becomes a daily series. The non-recurring
    # identity (UID only) disappears; recurring identities appear.
    new = cal(event(
        "UID:mix",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "SUMMARY:series",
    ))
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    assert ids(body["removed"]) == [("mix", None)]
    assert ids(body["added"]) == [
        ("mix", "2026-01-05T09:00:00Z"),
        ("mix", "2026-01-06T09:00:00Z"),
    ]
    assert body["modified"] == []


# ---------------------------------------------------------------------------
# Ordering: by identity (UID, then RECURRENCE-ID, None first)
# ---------------------------------------------------------------------------

def test_results_sorted_by_identity():
    old = cal(
        event_utc("b", "20260105T090000Z", "20260105T100000Z"),
        event_utc("c", "20260106T090000Z", "20260106T100000Z"),
    )
    new = cal(
        event_utc("a2", "20260107T090000Z", "20260107T100000Z"),
        event_utc("c", "20260106T090000Z", "20260106T100000Z"),
    )
    resp = diff(old, new)
    assert resp.status_code == 200
    assert [e["uid"] for e in resp.json()["added"]] == ["a2"]
    assert [e["uid"] for e in resp.json()["removed"]] == ["b"]


# ---------------------------------------------------------------------------
# Window filtering and 422 semantics
# ---------------------------------------------------------------------------

def test_instances_outside_shared_window_are_not_compared():
    old = cal(event_utc("moved", "20260220T090000Z", "20260220T100000Z"))
    new = cal(event_utc("moved", "20260225T090000Z", "20260225T100000Z"))
    # Both versions lie outside the January window: nothing to diff.
    resp = diff(old, new)
    assert resp.status_code == 200
    assert resp.json()["modified"] == []
    assert resp.json()["added"] == []
    assert resp.json()["removed"] == []


def test_move_into_window_is_removed_plus_added_not_modified():
    # The occurrence's identity (rid) is unchanged, but only the old slot
    # overlaps on one side... both slots actually lie in the January window
    # here, so the same identity exists on both sides -> modified. Instead,
    # move across the window boundary so each side sees a disjoint set.
    master_lines = (
        "UID:r",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=10",
    )
    old = cal(
        event(*master_lines),
        event(
            "UID:r",
            "RECURRENCE-ID:20260202T090000Z",  # Feb occurrence...
            "DTSTART:20260115T140000Z",        # ...moved into January
            "DTEND:20260115T150000Z",
        ),
    )
    new = cal(event(*master_lines))  # override reverted
    resp = diff(old, new)
    assert resp.status_code == 200
    body = resp.json()
    rid = "2026-02-02T09:00:00Z"
    assert ids(body["removed"]) == [("r", rid)]
    # New calendar gains the regular February occurrence, but it lies outside
    # the window, so it is not reported.
    assert body["added"] == []
    assert body["modified"] == []


def test_invalid_window_returns_422():
    ics = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z"))
    resp = diff(ics, ics, window_start="2026-02-01T00:00:00Z",
                window_end="2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_invalid_old_calendar_returns_422():
    new = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z"))
    resp = diff("not an ics", new)
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_invalid_new_calendar_returns_422():
    old = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z"))
    resp = diff(old, "BEGIN:VCALENDAR\r\nbogus")
    assert resp.status_code == 422


def test_unknown_tzid_in_either_calendar_returns_422():
    good = cal(event_utc("u", "20260105T090000Z", "20260105T100000Z"))
    bad = cal(event(
        "UID:u",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp_a = diff(bad, good)
    resp_b = diff(good, bad)
    assert resp_a.status_code == resp_b.status_code == 422
    assert "unknown timezone" in resp_a.json()["detail"]

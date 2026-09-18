"""Key tests for the /availability endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//avail-api//EN\r\n"
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


NINE_TO_FIVE = {d: [{"start": "09:00", "end": "17:00"}]
                for d in ("monday", "tuesday", "wednesday", "thursday", "friday")}


def availability(calendars=None, *, timezone="UTC", working_hours=None,
                 minimum_minutes=1,
                 window_start="2026-01-05T00:00:00Z",
                 window_end="2026-01-12T00:00:00Z"):
    return client.post(
        "/availability",
        json={
            "calendars": calendars or {},
            "window_start": window_start,
            "window_end": window_end,
            "timezone": timezone,
            "working_hours": NINE_TO_FIVE if working_hours is None else working_hours,
            "minimum_minutes": minimum_minutes,
        },
    )


def slot_pairs(response):
    return [(s["start"], s["end"]) for s in response.json()["slots"]]


# ---------------------------------------------------------------------------
# Free-time basics
# ---------------------------------------------------------------------------

def test_empty_calendars_returns_full_working_periods():
    resp = availability(window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")  # Monday
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    slot = resp.json()["slots"][0]
    assert slot == {
        "start": "2026-01-05T09:00:00Z",
        "end": "2026-01-05T17:00:00Z",
        "minutes": 480,
    }


def test_busy_event_splits_free_time():
    ics = cal(event_utc("e1", "20260105T100000Z", "20260105T110000Z"))
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T10:00:00Z"),
        ("2026-01-05T11:00:00Z", "2026-01-05T17:00:00Z"),
    ]


def test_free_time_is_the_intersection_of_all_calendars():
    # Monday Jan 5: alice busy 10:00-11:00, bob busy 14:00-15:00.
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T110000Z"))
    bob = cal(event_utc("b", "20260105T140000Z", "20260105T150000Z"))
    resp = availability({"alice": alice, "bob": bob},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T10:00:00Z"),
        ("2026-01-05T11:00:00Z", "2026-01-05T14:00:00Z"),
        ("2026-01-05T15:00:00Z", "2026-01-05T17:00:00Z"),
    ]


def test_overlapping_busy_across_calendars_counts_once():
    alice = cal(event_utc("a", "20260105T100000Z", "20260105T120000Z"))
    bob = cal(event_utc("b", "20260105T110000Z", "20260105T130000Z"))
    resp = availability({"alice": alice, "bob": bob},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T10:00:00Z"),
        ("2026-01-05T13:00:00Z", "2026-01-05T17:00:00Z"),
    ]


def test_adjacent_busy_intervals_merge_without_zero_slot():
    # Half-open [10,11) and [11,12) share the boundary instant; no free slot
    # may appear at 11:00.
    ics = cal(
        event_utc("a", "20260105T100000Z", "20260105T110000Z"),
        event_utc("b", "20260105T110000Z", "20260105T120000Z"),
    )
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T10:00:00Z"),
        ("2026-01-05T12:00:00Z", "2026-01-05T17:00:00Z"),
    ]


def test_cancelled_and_zero_duration_instances_are_ignored():
    ics = cal(
        event_utc("a", "20260105T100000Z", "20260105T110000Z", "STATUS:CANCELLED"),
        event_utc("b", "20260105T120000Z", "20260105T120000Z"),
    )
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T17:00:00Z")
    ]


def test_busy_overflowing_working_period_is_clipped():
    # 08:00-10:00 event only blocks 09:00-10:00 of the working day.
    ics = cal(event_utc("a", "20260105T080000Z", "20260105T100000Z"))
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert slot_pairs(resp) == [
        ("2026-01-05T10:00:00Z", "2026-01-05T17:00:00Z")
    ]


def test_all_day_event_blocks_full_working_day():
    ics = cal(event(
        "UID:a",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260106",
    ))
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert resp.json()["slots"] == []


def test_recurring_busy_is_expanded():
    ics = cal(event(
        "UID:a",
        "DTSTART:20260105T120000Z",
        "DTEND:20260105T130000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    resp = availability({"alice": ics},
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-08T00:00:00Z")
    pairs = slot_pairs(resp)
    # Jan 5 (Mon), 6 (Tue), 7 (Wed) each lose 12:00-13:00; two slots per day.
    assert pairs == [
        ("2026-01-05T09:00:00Z", "2026-01-05T12:00:00Z"),
        ("2026-01-05T13:00:00Z", "2026-01-05T17:00:00Z"),
        ("2026-01-06T09:00:00Z", "2026-01-06T12:00:00Z"),
        ("2026-01-06T13:00:00Z", "2026-01-06T17:00:00Z"),
        ("2026-01-07T09:00:00Z", "2026-01-07T12:00:00Z"),
        ("2026-01-07T13:00:00Z", "2026-01-07T17:00:00Z"),
    ]


# ---------------------------------------------------------------------------
# Working hours and window
# ---------------------------------------------------------------------------

def test_non_working_weekdays_produce_no_slots():
    resp = availability(
        working_hours={"monday": [{"start": "09:00", "end": "17:00"}]},
        window_start="2026-01-03T00:00:00Z",  # Saturday
        window_end="2026-01-04T00:00:00Z",
    )
    assert resp.json()["slots"] == []


def test_multiple_working_periods_per_day_with_lunch_gap():
    ics = cal(event_utc("a", "20260105T150000Z", "20260105T160000Z"))
    resp = availability(
        {"alice": ics},
        working_hours={"monday": [
            {"start": "09:00", "end": "12:00"},
            {"start": "13:00", "end": "17:00"},
        ]},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-06T00:00:00Z",
    )
    # The 12:00-13:00 lunch gap is not free time; afternoon busy splits it.
    assert slot_pairs(resp) == [
        ("2026-01-05T09:00:00Z", "2026-01-05T12:00:00Z"),
        ("2026-01-05T13:00:00Z", "2026-01-05T15:00:00Z"),
        ("2026-01-05T16:00:00Z", "2026-01-05T17:00:00Z"),
    ]


def test_window_is_clipped_to_working_periods():
    resp = availability(
        window_start="2026-01-05T10:00:00Z",
        window_end="2026-01-05T12:00:00Z",
    )
    assert slot_pairs(resp) == [
        ("2026-01-05T10:00:00Z", "2026-01-05T12:00:00Z")
    ]


# ---------------------------------------------------------------------------
# Minimum length
# ---------------------------------------------------------------------------

def test_minimum_minutes_filters_short_slots():
    # Busy 10:30-11:00 leaves gaps of 90 and 360 minutes.
    ics = cal(event_utc("a", "20260105T103000Z", "20260105T110000Z"))
    resp = availability({"alice": ics}, minimum_minutes=120,
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    slots = resp.json()["slots"]
    assert [(s["start"], s["minutes"]) for s in slots] == [
        ("2026-01-05T11:00:00Z", 360)
    ]


def test_minimum_minutes_is_inclusive_at_boundary():
    ics = cal(event_utc("a", "20260105T103000Z", "20260105T110000Z"))
    resp = availability({"alice": ics}, minimum_minutes=90,
                        window_start="2026-01-05T00:00:00Z",
                        window_end="2026-01-06T00:00:00Z")
    assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# DST: working periods are built per local date
# ---------------------------------------------------------------------------

def test_working_hours_follow_dst_transition():
    # America/New_York springs forward 2026-03-08 02:00 local:
    # 09:00 local is 14:00Z before, 13:00Z from Sunday onward.
    weekend = {
        "saturday": [{"start": "09:00", "end": "17:00"}],
        "sunday": [{"start": "09:00", "end": "17:00"}],
        "monday": [{"start": "09:00", "end": "17:00"}],
    }
    resp = availability(
        timezone="America/New_York", working_hours=weekend,
        window_start="2026-03-07T00:00:00Z",
        window_end="2026-03-10T00:00:00Z",
    )
    assert slot_pairs(resp) == [
        ("2026-03-07T14:00:00Z", "2026-03-07T22:00:00Z"),  # EST
        ("2026-03-08T13:00:00Z", "2026-03-08T21:00:00Z"),  # EDT
        ("2026-03-09T13:00:00Z", "2026-03-09T21:00:00Z"),  # EDT
    ]


def test_busy_event_local_time_follows_dst():
    # Recurring 10:00-11:00 local; first occurrence 15:00Z, then 14:00Z.
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=America/New_York:20260306T100000",
        "DTEND;TZID=America/New_York:20260306T110000",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    weekend = {
        "friday": [{"start": "09:00", "end": "17:00"}],
        "saturday": [{"start": "09:00", "end": "17:00"}],
        "sunday": [{"start": "09:00", "end": "17:00"}],
    }
    resp = availability(
        {"alice": ics}, timezone="America/New_York", working_hours=weekend,
        window_start="2026-03-06T00:00:00Z",
        window_end="2026-03-09T00:00:00Z",
    )
    pairs = slot_pairs(resp)
    # Working 09:00-17:00 local; busy 10:00-11:00 local. EST day: busy
    # 15:00-16:00Z. EDT day: the busy local hour lands at 14:00-15:00Z.
    assert ("2026-03-06T14:00:00Z", "2026-03-06T15:00:00Z") in pairs
    assert ("2026-03-06T16:00:00Z", "2026-03-06T22:00:00Z") in pairs
    assert ("2026-03-08T13:00:00Z", "2026-03-08T14:00:00Z") in pairs
    assert ("2026-03-08T15:00:00Z", "2026-03-08T21:00:00Z") in pairs


# ---------------------------------------------------------------------------
# Ordering and formatting
# ---------------------------------------------------------------------------

def test_slots_sorted_by_start_and_use_z_format():
    ics = cal(event_utc("a", "20260107T120000Z", "20260107T130000Z"))
    resp = availability({"alice": ics})
    starts = [s["start"] for s in resp.json()["slots"]]
    assert starts == sorted(starts)
    for s in resp.json()["slots"]:
        assert s["start"].endswith("Z")
        assert s["end"].endswith("Z")
        assert "+" not in s["start"]


# ---------------------------------------------------------------------------
# Error handling: 422
# ---------------------------------------------------------------------------

def test_unknown_timezone_returns_422():
    resp = availability(timezone="Mars/Olympus_Mons")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]
    assert "Mars/Olympus_Mons" in resp.json()["detail"]


def test_inverted_working_period_returns_422():
    resp = availability(
        working_hours={"monday": [{"start": "12:00", "end": "12:00"}]},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-06T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "after start" in resp.text


def test_overnight_working_period_returns_422():
    resp = availability(
        working_hours={"monday": [{"start": "22:00", "end": "06:00"}]},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-06T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "overnight" in resp.text


def test_offset_aware_working_time_returns_422():
    resp = availability(
        working_hours={"monday": [{"start": "09:00+02:00", "end": "17:00"}]},
        window_start="2026-01-05T00:00:00Z",
        window_end="2026-01-06T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "naive local times" in resp.text


def test_unknown_weekday_key_returns_422():
    resp = availability(working_hours={"funday": []})
    assert resp.status_code == 422


def test_zero_minimum_returns_422():
    resp = availability(minimum_minutes=0)
    assert resp.status_code == 422


def test_negative_minimum_returns_422():
    resp = availability(minimum_minutes=-15)
    assert resp.status_code == 422


def test_inverted_window_returns_422():
    resp = availability(window_start="2026-01-12T00:00:00Z",
                        window_end="2026-01-05T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_invalid_ics_returns_422():
    resp = availability({"alice": "not an iCalendar document"})
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_unknown_tzid_inside_calendar_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T100000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T110000",
    ))
    resp = availability({"alice": ics})
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]

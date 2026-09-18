"""Key tests for the /agenda endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//agenda-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def agenda(
    ics: str,
    window_start="2026-01-01T00:00:00Z",
    window_end="2026-02-01T00:00:00Z",
    timezone="Asia/Tokyo",
):
    return client.post(
        "/agenda",
        json={
            "ics": ics,
            "window_start": window_start,
            "window_end": window_end,
            "timezone": timezone,
        },
    )


def rows(response):
    return [
        (e["date"], e["all_day"], e["start"], e["end"], e["uid"])
        for e in response.json()["entries"]
    ]


# ---------------------------------------------------------------------------
# Timed events: localization and midnight splitting
# ---------------------------------------------------------------------------

def test_timed_event_renders_in_local_timezone():
    ics = cal(event(
        "UID:meet-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:Sync",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == "Asia/Tokyo"
    assert body["count"] == 1
    entry = body["entries"][0]
    assert entry["date"] == "2026-01-05"
    assert entry["all_day"] is False
    assert entry["start"] == "2026-01-05T18:00:00+09:00"
    assert entry["end"] == "2026-01-05T19:00:00+09:00"
    assert entry["continues_before"] is False
    assert entry["continues_after"] is False
    assert entry["uid"] == "meet-1"
    assert entry["summary"] == "Sync"


def test_timed_event_crossing_local_midnight_is_split():
    # 14:00Z (23:00 JST) until 02:00Z next day (11:00 JST).
    ics = cal(event(
        "UID:night-1",
        "DTSTART:20260105T140000Z",
        "DTEND:20260106T020000Z",
        "SUMMARY:Night shift",
    ))
    resp = agenda(ics)
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 2

    first, second = entries
    assert first["date"] == "2026-01-05"
    assert first["start"] == "2026-01-05T23:00:00+09:00"
    assert first["end"] == "2026-01-06T00:00:00+09:00"
    assert first["continues_before"] is False
    assert first["continues_after"] is True

    assert second["date"] == "2026-01-06"
    assert second["start"] == "2026-01-06T00:00:00+09:00"
    assert second["end"] == "2026-01-06T11:00:00+09:00"
    assert second["continues_before"] is True
    assert second["continues_after"] is False


def test_long_event_is_split_into_three_days():
    # Jan 5 06:00Z (15:00 JST) for 48 hours -> Jan 5, 6, 7 local.
    ics = cal(event(
        "UID:long-1",
        "DTSTART:20260105T060000Z",
        "DTEND:20260107T060000Z",
    ))
    resp = agenda(ics)
    entries = resp.json()["entries"]
    assert [e["date"] for e in entries] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert entries[0]["continues_before"] is False
    assert entries[0]["continues_after"] is True
    assert entries[1]["continues_before"] is True
    assert entries[1]["continues_after"] is True
    assert entries[2]["continues_before"] is True
    assert entries[2]["continues_after"] is False


def test_midnight_boundaries_use_wall_clock_across_dst():
    # America/New_York springs forward 2026-03-08 02:00 -> 03:00 local.
    ics = cal(event(
        "UID:dst-1",
        "DTSTART:20260307T120000Z",
        "DTEND:20260309T120000Z",
    ))
    resp = agenda(
        ics,
        window_start="2026-03-01T00:00:00Z",
        window_end="2026-04-01T00:00:00Z",
        timezone="America/New_York",
    )
    entries = resp.json()["entries"]
    by_date = {e["date"]: e for e in entries}
    assert list(by_date) == ["2026-03-07", "2026-03-08", "2026-03-09"]
    # The middle fragment is the full local day: midnight to next midnight,
    # with the offset switching -05:00 -> -04:00 at the DST boundary.
    middle = by_date["2026-03-08"]
    assert middle["start"] == "2026-03-08T00:00:00-05:00"
    assert middle["end"] == "2026-03-09T00:00:00-04:00"
    assert middle["continues_before"] is True
    assert middle["continues_after"] is True


# ---------------------------------------------------------------------------
# All-day (DATE) events: authored dates, never shifted by the display tz
# ---------------------------------------------------------------------------

def test_single_day_all_day_event():
    ics = cal(event(
        "UID:allday-1",
        "DTSTART;VALUE=DATE:20260105",
        "SUMMARY:Holiday",
    ))
    resp = agenda(ics)
    entries = resp.json()["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["all_day"] is True
    assert entry["date"] == "2026-01-05"
    assert entry["start"] == "2026-01-05T00:00:00+09:00"
    assert entry["end"] == "2026-01-06T00:00:00+09:00"
    assert entry["continues_before"] is False
    assert entry["continues_after"] is False


def test_multi_day_all_day_event_expands_per_authored_date():
    # DATE range Jan 5 (inclusive) through Jan 8 (exclusive) = 3 days.
    ics = cal(event(
        "UID:allday-2",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260108",
    ))
    resp = agenda(ics)
    entries = resp.json()["entries"]
    assert [e["date"] for e in entries] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert all(e["all_day"] for e in entries)
    flags = [(e["continues_before"], e["continues_after"]) for e in entries]
    assert flags == [(False, True), (True, True), (True, False)]
    # Timestamps are local midnights even on a negative-offset display tz;
    # the authored DATE must not slide to Jan 4 by a UTC-midnight conversion.
    assert entries[0]["start"] == "2026-01-05T00:00:00+09:00"


def test_all_day_event_keeps_authored_dates_regardless_of_display_tz():
    ics = cal(event(
        "UID:allday-3",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260107",
    ))
    for tzname, offset in (
        ("Asia/Tokyo", "+09:00"),
        ("America/Los_Angeles", "-08:00"),
        ("UTC", "+00:00"),
    ):
        resp = agenda(ics, timezone=tzname)
        entries = resp.json()["entries"]
        assert [e["date"] for e in entries] == ["2026-01-05", "2026-01-06"]
        assert entries[0]["start"] == f"2026-01-05T00:00:00{offset}"
        assert entries[0]["end"] == f"2026-01-06T00:00:00{offset}"


def test_recurring_all_day_series_expands_day_by_day():
    ics = cal(event(
        "UID:allday-series",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260106",
        "RRULE:FREQ=DAILY;COUNT=3",
        "SUMMARY:On call",
    ))
    resp = agenda(ics, timezone="America/New_York")
    entries = resp.json()["entries"]
    assert [e["date"] for e in entries] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert all(e["all_day"] for e in entries)
    assert all(e["continues_after"] is False for e in entries)


# ---------------------------------------------------------------------------
# Ordering: date asc, all-day first, then start, then uid
# ---------------------------------------------------------------------------

def test_entries_sorted_date_then_allday_then_start_then_uid():
    ics = cal(
        # timed, Jan 5 09:00Z (18:00 JST)
        event("UID:timed-b", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        # all-day Jan 5 -> must sort ahead of the timed event despite later uid
        event("UID:zzz-allday", "DTSTART;VALUE=DATE:20260105",
              "DTEND;VALUE=DATE:20260106"),
        # timed earlier local time Jan 5 (00:00Z = 09:00 JST)
        event("UID:timed-a", "DTSTART:20260105T000000Z", "DTEND:20260105T010000Z"),
        # all-day Jan 4 -> earliest overall
        event("UID:allday-jan4", "DTSTART;VALUE=DATE:20260104",
              "DTEND;VALUE=DATE:20260106"),
    )
    resp = agenda(ics)
    assert resp.status_code == 200
    got = [(e["date"], e["all_day"], e["start"], e["uid"]) for e in resp.json()["entries"]]
    assert got == [
        ("2026-01-04", True, "2026-01-04T00:00:00+09:00", "allday-jan4"),
        ("2026-01-05", True, "2026-01-05T00:00:00+09:00", "allday-jan4"),
        ("2026-01-05", True, "2026-01-05T00:00:00+09:00", "zzz-allday"),
        ("2026-01-05", False, "2026-01-05T09:00:00+09:00", "timed-a"),
        ("2026-01-05", False, "2026-01-05T18:00:00+09:00", "timed-b"),
    ]


def test_tie_broken_by_uid():
    ics = cal(
        event("UID:z-event", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:a-event", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
    )
    resp = agenda(ics)
    assert [e["uid"] for e in resp.json()["entries"]] == ["a-event", "z-event"]


# ---------------------------------------------------------------------------
# Windowing, floating times, overrides, cancellations
# ---------------------------------------------------------------------------

def test_window_overlap_is_judged_in_utc_like_expand():
    # Starts 23:00Z Jan 9 (08:00 JST Jan 10): included only by overlap.
    ics = cal(event(
        "UID:edge-1",
        "DTSTART:20260109T230000Z",
        "DTEND:20260110T010000Z",
    ))
    resp = agenda(
        ics,
        window_start="2026-01-10T00:00:00Z",
        window_end="2026-01-11T00:00:00Z",
    )
    entries = resp.json()["entries"]
    assert [e["date"] for e in entries] == ["2026-01-10"]
    # Fragment starts at the instance's true local start, not clipped to window.
    assert entries[0]["start"] == "2026-01-10T08:00:00+09:00"


def test_floating_time_is_utc():
    ics = cal(event(
        "UID:float-1",
        "DTSTART:20260105T090000",
        "DURATION:PT1H",
    ))
    resp = agenda(ics)
    entry = resp.json()["entries"][0]
    assert entry["start"] == "2026-01-05T18:00:00+09:00"
    assert entry["end"] == "2026-01-05T19:00:00+09:00"


def test_cancelled_instances_are_omitted():
    ics = cal(
        event("UID:evt-1", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z",
              "STATUS:CANCELLED"),
        event("UID:evt-2", "DTSTART;VALUE=DATE:20260106",
              "DTEND;VALUE=DATE:20260107", "STATUS:CANCELLED"),
    )
    resp = agenda(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_cancelled_recurring_override_is_omitted_but_sibling_kept():
    ics = cal(
        event(
            "UID:series-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
        ),
        event(
            "UID:series-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T090000Z",
            "DTEND:20260106T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = agenda(ics)
    entries = resp.json()["entries"]
    assert [e["date"] for e in entries] == ["2026-01-05", "2026-01-07"]


def test_date_override_flips_occurrence_to_all_day():
    ics = cal(
        event(
            "UID:series-2",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:series-2",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART;VALUE=DATE:20260106",
            "DTEND;VALUE=DATE:20260107",
            "SUMMARY:moved to all day",
        ),
    )
    resp = agenda(ics)
    entries = resp.json()["entries"]
    assert [(e["date"], e["all_day"]) for e in entries] == [
        ("2026-01-05", False),
        ("2026-01-06", True),
    ]
    assert entries[1]["summary"] == "moved to all day"


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def test_unknown_timezone_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z",
                    "DTEND:20260105T100000Z"))
    resp = agenda(ics, timezone="Mars/Olympus_Mons")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]
    assert "Mars/Olympus_Mons" in resp.json()["detail"]


def test_unparseable_calendar_returns_422():
    resp = agenda("not an icalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_invalid_rrule_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z",
                    "RRULE:FREQ=BOGUS"))
    resp = agenda(ics)
    assert resp.status_code == 422
    assert "invalid RRULE" in resp.json()["detail"]


def test_unknown_tzid_in_calendar_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp = agenda(ics)
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_invalid_window_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = agenda(
        ics,
        window_start="2026-02-01T00:00:00Z",
        window_end="2026-01-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_missing_field_is_a_schema_422():
    resp = client.post(
        "/agenda",
        json={"ics": "BEGIN:VCALENDAR\r\nEND:VCALENDAR",
              "window_start": "2026-01-01T00:00:00Z"},
    )
    assert resp.status_code == 422

"""Key tests for the /fair-rotation endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//rotation-api//EN\r\n"
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


def rotation(*, organizer_timezone="UTC", duration_minutes=60, dates=None,
             participants=None):
    return client.post(
        "/fair-rotation",
        json={
            "organizer_timezone": organizer_timezone,
            "duration_minutes": duration_minutes,
            "dates": dates if dates is not None else [
                {"date": "2026-03-09", "candidates": ["09:00"]}
            ],
            "participants": participants or [],
        },
    )


def participant(name, *, timezone="UTC", busy_ics="", preferred=None):
    return {
        "name": name,
        "timezone": timezone,
        "busy_ics": busy_ics,
        "preferred_periods": preferred or [],
    }


# ---------------------------------------------------------------------------
# Joint selection and costs
# ---------------------------------------------------------------------------

def test_joint_plan_balances_cumulative_deviation_across_dates():
    # Alice prefers 09:00-10:00, Bob 10:00-11:00 (both UTC). Per date the
    # 09:00 candidate costs Bob 60 and the 10:00 candidate costs Alice 60.
    # Picking 09:00 twice would max out Bob at 120; the joint optimum
    # alternates so both accumulate 60. The (09:00, 10:00) and (10:00, 09:00)
    # plans tie, and candidate input order picks the former.
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["09:00", "10:00"]},
            {"date": "2026-03-10", "candidates": ["09:00", "10:00"]},
        ],
        participants=[
            participant("alice", preferred=[{"start": "09:00", "end": "10:00"}]),
            participant("bob", preferred=[{"start": "10:00", "end": "11:00"}]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [(m["date"], m["candidate_index"]) for m in body["meetings"]] == [
        ("2026-03-09", 0),
        ("2026-03-10", 1),
    ]
    assert [m["start"] for m in body["meetings"]] == [
        "2026-03-09T09:00:00Z",
        "2026-03-10T10:00:00Z",
    ]
    costs = {p["name"]: p["cumulative_deviation_minutes"]
             for p in body["participants"]}
    assert costs == {"alice": 60, "bob": 60}
    assert body["max_cumulative_deviation_minutes"] == 60
    assert body["total_deviation_minutes"] == 120
    assert body["meeting_count"] == 2


def test_full_response_shape_and_per_meeting_deviations():
    resp = rotation(
        organizer_timezone="America/New_York",
        duration_minutes=30,
        dates=[{"date": "2026-03-09", "candidates": ["12:00"]}],
        participants=[
            participant("alice", timezone="Europe/Berlin",
                        preferred=[{"start": "09:00", "end": "18:00"}]),
            participant("bob", timezone="Asia/Tokyo",
                        preferred=[{"start": "09:00", "end": "18:00"}]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["organizer_timezone"] == "America/New_York"
    assert body["duration_minutes"] == 30
    # 12:00 in New York (EDT, UTC-4) is 16:00Z: 17:00 in Berlin (inside
    # 09:00-18:00) and 01:00 in Tokyo (fully outside).
    (meeting,) = body["meetings"]
    assert meeting["date"] == "2026-03-09"
    assert meeting["candidate_index"] == 0
    assert meeting["start"] == "2026-03-09T16:00:00Z"
    assert meeting["end"] == "2026-03-09T16:30:00Z"
    assert meeting["local_start"] == "2026-03-09T12:00:00-04:00"
    assert meeting["local_end"] == "2026-03-09T12:30:00-04:00"
    assert meeting["deviations"] == [
        {"participant": "alice", "deviation_minutes": 0},
        {"participant": "bob", "deviation_minutes": 30},
    ]
    assert body["participants"] == [
        {"name": "alice", "timezone": "Europe/Berlin",
         "cumulative_deviation_minutes": 0},
        {"name": "bob", "timezone": "Asia/Tokyo",
         "cumulative_deviation_minutes": 30},
    ]


def test_empty_participants_pick_first_candidates_with_zero_costs():
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["10:00", "09:00"]},
            {"date": "2026-03-10", "candidates": ["11:00", "08:00"]},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    # No participants: every candidate is feasible and equally good, so the
    # input-order tie-break selects the first candidate of each date.
    assert [(m["candidate_index"], m["start"]) for m in body["meetings"]] == [
        (0, "2026-03-09T10:00:00Z"),
        (0, "2026-03-10T11:00:00Z"),
    ]
    assert body["participants"] == []
    assert body["max_cumulative_deviation_minutes"] == 0
    assert body["total_deviation_minutes"] == 0


def test_partial_preference_coverage_counts_only_uncovered_minutes():
    # 60-minute meeting 09:30-10:30 vs preferred 09:00-10:00: 30 minutes
    # outside. Second meeting fully inside. Cumulative is 30.
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["09:30"]},
            {"date": "2026-03-10", "candidates": ["09:00"]},
        ],
        participants=[
            participant("alice", preferred=[{"start": "09:00", "end": "10:00"}]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [m["deviations"][0]["deviation_minutes"] for m in body["meetings"]] == [30, 0]
    assert body["participants"][0]["cumulative_deviation_minutes"] == 30


def test_participant_without_preferred_periods_deviates_fully():
    resp = rotation(
        duration_minutes=45,
        dates=[{"date": "2026-03-09", "candidates": ["09:00", "10:00"]}],
        participants=[participant("alice")],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["participants"][0]["cumulative_deviation_minutes"] == 45
    # Both candidates cost the same, so input order wins.
    assert body["meetings"][0]["candidate_index"] == 0


def test_fractional_deviation_minutes_for_subminute_candidates():
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["09:00:30"]}],
        participants=[
            participant("alice", preferred=[{"start": "09:00", "end": "10:00"}]),
        ],
    )
    assert resp.status_code == 200
    # Meeting 09:00:30-10:00:30 sticks 30 seconds past the preferred period.
    assert resp.json()["participants"][0]["cumulative_deviation_minutes"] == 0.5


# ---------------------------------------------------------------------------
# Objective ordering: max, then total, then input order
# ---------------------------------------------------------------------------

def test_max_cumulative_is_the_primary_objective():
    # c1 (09:30): alice 30, bob 30 -> max 30, total 60.
    # c2 (10:00): alice 60, bob 0  -> max 60, total 60.
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["09:30", "10:00"]}],
        participants=[
            participant("alice", preferred=[{"start": "09:00", "end": "10:00"}]),
            participant("bob", preferred=[{"start": "10:00", "end": "11:00"}]),
        ],
    )
    assert resp.json()["meetings"][0]["candidate_index"] == 0
    assert resp.json()["max_cumulative_deviation_minutes"] == 30


def test_total_deviation_breaks_max_ties():
    # c1 (08:30): alice 30, bob 60 -> max 60, total 90.
    # c2 (10:00): alice 60, bob 0  -> max 60, total 60.
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["08:30", "10:00"]}],
        participants=[
            participant("alice", preferred=[{"start": "08:00", "end": "09:00"}]),
            participant("bob", preferred=[{"start": "10:00", "end": "11:00"}]),
        ],
    )
    assert resp.json()["meetings"][0]["candidate_index"] == 1
    assert resp.json()["total_deviation_minutes"] == 60


def test_input_order_breaks_remaining_ties():
    # Both candidates cost alice 60; the earlier input candidate wins even
    # though it is later in the day.
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["12:00", "08:00"]}],
        participants=[
            participant("alice", preferred=[{"start": "09:00", "end": "10:00"}]),
        ],
    )
    assert resp.json()["meetings"][0]["candidate_index"] == 0
    assert resp.json()["meetings"][0]["start"] == "2026-03-09T12:00:00Z"


# ---------------------------------------------------------------------------
# Busy calendars and expansion semantics
# ---------------------------------------------------------------------------

def test_busy_event_filters_conflicting_candidate():
    busy = cal(event_utc("b1", "20260309T133000Z", "20260309T143000Z"))
    resp = rotation(
        organizer_timezone="America/New_York",
        dates=[{"date": "2026-03-09", "candidates": ["09:00", "12:00"]}],
        participants=[participant("alice", timezone="Europe/Berlin",
                                  busy_ics=busy)],
    )
    assert resp.status_code == 200
    meeting = resp.json()["meetings"][0]
    # 09:00 New York is 13:00-14:00Z and overlaps the busy event; 12:00 wins.
    assert meeting["candidate_index"] == 1
    assert meeting["start"] == "2026-03-09T16:00:00Z"


def test_recurring_busy_blocks_every_matching_date():
    busy = cal(event(
        "UID:standup",
        "DTSTART:20260309T090000Z",
        "DTEND:20260309T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["09:00", "12:00"]},
            {"date": "2026-03-10", "candidates": ["09:00", "12:00"]},
            {"date": "2026-03-11", "candidates": ["09:00", "12:00"]},
        ],
        participants=[participant("alice", busy_ics=busy)],
    )
    assert resp.status_code == 200
    assert [m["start"] for m in resp.json()["meetings"]] == [
        "2026-03-09T12:00:00Z",
        "2026-03-10T12:00:00Z",
        "2026-03-11T12:00:00Z",
    ]


def test_cancelled_and_zero_duration_busy_events_do_not_block():
    busy = cal(
        event_utc("c1", "20260309T090000Z", "20260309T100000Z",
                  "STATUS:CANCELLED"),
        event_utc("c2", "20260309T090000Z", "20260309T090000Z"),
    )
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["09:00"]}],
        participants=[participant("alice", busy_ics=busy)],
    )
    assert resp.status_code == 200
    assert resp.json()["meetings"][0]["start"] == "2026-03-09T09:00:00Z"


def test_recurrence_override_moves_the_blocking_instance():
    # Daily 09:00-10:00Z series; the March 10 occurrence is rescheduled to
    # 12:00-13:00Z, so on March 10 the 09:00 candidate is free and 12:00 is
    # blocked instead.
    busy = cal(
        event(
            "UID:daily",
            "DTSTART:20260309T090000Z",
            "DTEND:20260309T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:daily",
            "RECURRENCE-ID:20260310T090000Z",
            "DTSTART:20260310T120000Z",
            "DTEND:20260310T130000Z",
        ),
    )
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["09:00", "12:00"]},
            {"date": "2026-03-10", "candidates": ["09:00", "12:00"]},
        ],
        participants=[participant("alice", busy_ics=busy)],
    )
    assert resp.status_code == 200
    assert [m["start"] for m in resp.json()["meetings"]] == [
        "2026-03-09T12:00:00Z",
        "2026-03-10T09:00:00Z",
    ]


def test_touching_busy_interval_does_not_block():
    # Half-open intervals: a busy event ending exactly at the candidate
    # start leaves the candidate feasible.
    busy = cal(event_utc("b1", "20260309T080000Z", "20260309T090000Z"))
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["09:00"]}],
        participants=[participant("alice", busy_ics=busy)],
    )
    assert resp.status_code == 200


def test_empty_busy_ics_means_no_busy_time():
    resp = rotation(
        dates=[{"date": "2026-03-09", "candidates": ["09:00"]}],
        participants=[participant("alice", busy_ics="")],
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# DST handling
# ---------------------------------------------------------------------------

def test_organizer_candidates_follow_dst_transition():
    # America/New_York springs forward on 2026-03-08: 09:00 local is 14:00Z
    # before the transition and 13:00Z after it.
    resp = rotation(
        organizer_timezone="America/New_York",
        dates=[
            {"date": "2026-03-07", "candidates": ["09:00"]},
            {"date": "2026-03-09", "candidates": ["09:00"]},
        ],
    )
    assert resp.status_code == 200
    assert [(m["start"], m["local_start"]) for m in resp.json()["meetings"]] == [
        ("2026-03-07T14:00:00Z", "2026-03-07T09:00:00-05:00"),
        ("2026-03-09T13:00:00Z", "2026-03-09T09:00:00-04:00"),
    ]


def test_participant_preference_follows_participant_dst():
    # Europe/Berlin springs forward on 2026-03-29: 13:00Z is 14:00 local
    # (inside 14:00-15:00) before and 15:00 local (outside) after.
    resp = rotation(
        dates=[
            {"date": "2026-03-28", "candidates": ["13:00"]},
            {"date": "2026-03-30", "candidates": ["13:00"]},
        ],
        participants=[
            participant("alice", timezone="Europe/Berlin",
                        preferred=[{"start": "14:00", "end": "15:00"}]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [m["deviations"][0]["deviation_minutes"] for m in body["meetings"]] == [0, 60]
    assert body["participants"][0]["cumulative_deviation_minutes"] == 60


# ---------------------------------------------------------------------------
# Error handling: 422
# ---------------------------------------------------------------------------

def test_date_without_feasible_candidate_returns_422():
    busy = cal(event_utc("b1", "20260309T000000Z", "20260310T000000Z"))
    resp = rotation(
        dates=[
            {"date": "2026-03-09", "candidates": ["09:00", "12:00"]},
            {"date": "2026-03-10", "candidates": ["09:00"]},
        ],
        participants=[participant("alice", busy_ics=busy)],
    )
    assert resp.status_code == 422
    assert "2026-03-09" in resp.json()["detail"]


def test_combination_limit_returns_422():
    times = [f"{(i * 6) // 60:02d}:{(i * 6) % 60:02d}" for i in range(224)]
    resp = rotation(
        duration_minutes=5,
        dates=[
            {"date": "2026-03-09", "candidates": times},
            {"date": "2026-03-10", "candidates": times},
        ],
    )
    assert resp.status_code == 422
    assert "50176" in resp.json()["detail"]


def test_combination_limit_boundary_is_allowed():
    times_a = [f"{(i * 6) // 60:02d}:{(i * 6) % 60:02d}" for i in range(223)]
    times_b = [f"{(i * 6) // 60:02d}:{(i * 6) % 60:02d}" for i in range(224)]
    resp = rotation(
        duration_minutes=5,
        dates=[
            {"date": "2026-03-09", "candidates": times_a},
            {"date": "2026-03-10", "candidates": times_b},
        ],
    )
    assert resp.status_code == 200  # 223 * 224 = 49952 <= 50000
    assert resp.json()["meeting_count"] == 2


def test_unknown_organizer_timezone_returns_422():
    resp = rotation(organizer_timezone="Mars/Olympus_Mons")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_unknown_participant_timezone_returns_422():
    resp = rotation(participants=[participant("alice", timezone="Mars/Olympus_Mons")])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_invalid_busy_ics_returns_422():
    resp = rotation(participants=[participant("alice", busy_ics="not ical")])
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_unknown_tzid_inside_busy_ics_returns_422():
    busy = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260309T100000",
        "DTEND;TZID=Mars/Olympus_Mons:20260309T110000",
    ))
    resp = rotation(participants=[participant("alice", busy_ics=busy)])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_duplicate_participant_name_returns_422():
    resp = rotation(participants=[participant("alice"), participant("alice")])
    assert resp.status_code == 422
    assert "duplicate participant name" in resp.json()["detail"]


def test_zero_duration_returns_422():
    resp = rotation(duration_minutes=0)
    assert resp.status_code == 422


def test_negative_duration_returns_422():
    resp = rotation(duration_minutes=-30)
    assert resp.status_code == 422


def test_empty_dates_returns_422():
    resp = rotation(dates=[])
    assert resp.status_code == 422


def test_empty_candidates_returns_422():
    resp = rotation(dates=[{"date": "2026-03-09", "candidates": []}])
    assert resp.status_code == 422


def test_offset_aware_candidate_time_returns_422():
    resp = rotation(dates=[{"date": "2026-03-09", "candidates": ["09:00+02:00"]}])
    assert resp.status_code == 422
    assert "naive local times" in resp.text


def test_inverted_preferred_period_returns_422():
    resp = rotation(participants=[
        participant("alice", preferred=[{"start": "18:00", "end": "09:00"}])
    ])
    assert resp.status_code == 422
    assert "after start" in resp.text


def test_offset_aware_preferred_period_returns_422():
    resp = rotation(participants=[
        participant("alice", preferred=[{"start": "09:00+01:00", "end": "17:00"}])
    ])
    assert resp.status_code == 422
    assert "naive local times" in resp.text


def test_unknown_nested_field_returns_422():
    resp = rotation(dates=[{"date": "2026-03-09", "candidates": ["09:00"],
                            "room": "A"}])
    assert resp.status_code == 422
    resp = rotation(participants=[{**participant("alice"), "vip": True}])
    assert resp.status_code == 422

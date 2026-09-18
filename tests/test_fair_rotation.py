"""Key tests for the /fair-rotation endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//rot-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event_utc(uid: str, start: str, end: str, *extra: str) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        + "".join(line + "\r\n" for line in extra)
        + "END:VEVENT\r\n"
    )


def event_local(uid: str, start: str, end: str, tz: str, *extra: str) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART;TZID={tz}:{start}\r\nDTEND;TZID={tz}:{end}\r\n"
        + "".join(line + "\r\n" for line in extra)
        + "END:VEVENT\r\n"
    )


def all_day(uid: str, start: str, end: str) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}\r\n"
        "END:VEVENT\r\n"
    )


def rotation(
    *,
    organizer_timezone="UTC",
    dates=("2026-03-02", "2026-03-03"),
    candidate_times=("08:00", "16:00"),
    duration_minutes=60,
    participants,
):
    return client.post(
        "/fair-rotation",
        json={
            "organizer_timezone": organizer_timezone,
            "dates": list(dates),
            "candidate_times": list(candidate_times),
            "duration_minutes": duration_minutes,
            "participants": participants,
        },
    )


def participant(
    participant_id,
    *,
    timezone="UTC",
    ics=None,
    preferred_ranges=None,
):
    return {
        "participant_id": participant_id,
        "timezone": timezone,
        "ics": cal() if ics is None else ics,
        "preferred_ranges": [] if preferred_ranges is None else preferred_ranges,
    }


# ---------------------------------------------------------------------------
# Fair selection: joint optimization over all dates
# ---------------------------------------------------------------------------


def test_rotation_alternates_to_minimize_max_then_total():
    # Each candidate is ideal for exactly one participant: choosing 08:00
    # both days leaves bob at 120 while alice is 0; the joint optimum
    # alternates 08:00 / 16:00 so each accumulates 60.
    resp = rotation(
        participants=[
            participant("alice", preferred_ranges=[{"start": "08:00", "end": "09:00"}]),
            participant("bob", preferred_ranges=[{"start": "16:00", "end": "17:00"}]),
        ]
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [m["candidate_index"] for m in data["meetings"]] == [0, 1]
    assert data["max_deviation"] == 60
    assert data["total_deviation"] == 120
    assert data["cumulative_costs"] == {"alice": 60, "bob": 60}


def test_response_carries_per_meeting_and_cumulative_costs():
    resp = rotation(
        participants=[
            participant("alice", preferred_ranges=[{"start": "08:00", "end": "09:00"}]),
            participant("bob", preferred_ranges=[{"start": "16:00", "end": "17:00"}]),
        ]
    )
    meetings = resp.json()["meetings"]
    assert meetings[0]["deviations"] == {"alice": 0, "bob": 60}
    assert meetings[0]["cumulative_costs"] == {"alice": 0, "bob": 60}
    assert meetings[1]["deviations"] == {"alice": 60, "bob": 0}
    assert meetings[1]["cumulative_costs"] == {"alice": 60, "bob": 60}
    assert all(m["start"].endswith("Z") and m["end"].endswith("Z") for m in meetings)


def test_max_deviation_dominates_total_deviation():
    # Three dates, two candidates (60-minute meetings):
    #   c0 08:00 -> alice 0, bob 40 ; c1 09:00 -> alice 30, bob 30
    # all-c0: (alice 0,   bob 120) -> max 120, total 120
    # all-c1: (alice 90,  bob 90)  -> max  90, total 180
    # The smaller max wins even though its total is larger: all three
    # meetings choose candidate 1.
    resp = rotation(
        dates=("2026-03-02", "2026-03-03", "2026-03-04"),
        candidate_times=("08:00", "09:00"),
        participants=[
            participant(
                "alice",
                preferred_ranges=[
                    {"start": "08:00", "end": "09:00"},
                    {"start": "09:30", "end": "10:00"},
                ],
            ),
            participant(
                "bob",
                preferred_ranges=[{"start": "08:40", "end": "09:30"}],
            ),
        ],
    )
    data = resp.json()
    # Every meeting is forced to candidate 1 by the max objective; its
    # per-meeting cost is therefore the c1 vector (30, 30).
    assert all(m["deviations"] == {"alice": 30, "bob": 30} for m in data["meetings"])
    assert [m["candidate_index"] for m in data["meetings"]] == [1, 1, 1]
    assert data["max_deviation"] == 90
    assert data["total_deviation"] == 180


def test_total_deviation_breaks_a_max_tie():
    # Two dates, two candidates:
    #   c0 08:00 -> alice 0, bob 30, carol 30
    #   c1 09:00 -> alice 0, bob 30, carol 0
    # (c0,c0) -> (0, 60, 60) max 60 total 120
    # (c1,c1) -> (0, 60,  0) max 60 total  60  <- same max, smaller total
    resp = rotation(
        dates=("2026-03-02", "2026-03-03"),
        candidate_times=("08:00", "09:00"),
        participants=[
            participant("alice", preferred_ranges=[{"start": "08:00", "end": "10:00"}]),
            participant("bob", preferred_ranges=[{"start": "08:30", "end": "09:30"}]),
            participant(
                "carol",
                preferred_ranges=[
                    {"start": "08:30", "end": "09:00"},
                    {"start": "09:00", "end": "10:00"},
                ],
            ),
        ],
    )
    data = resp.json()
    assert [m["candidate_index"] for m in data["meetings"]] == [1, 1]
    assert data["max_deviation"] == 60
    assert data["total_deviation"] == 60
    assert data["cumulative_costs"] == {"alice": 0, "bob": 60, "carol": 0}


def test_selection_is_joint_not_greedy_per_date():
    # 12-minute meetings on three dates; per-meeting deviation vectors:
    #   c0 08:00 -> alice 0,  bob 12
    #   c1 09:00 -> alice 10, bob 0
    # all-c0 finishes (0, 36) -> max 36; all-c1 finishes (30, 0) -> max 30;
    # the joint optimum takes c0 once and c1 twice: (20, 12) -> max 20.
    # A purely per-date greedy rule (or always-the-locally-cheap candidate)
    # cannot discover the balanced distribution.
    resp = rotation(
        dates=("2026-03-02", "2026-03-03", "2026-03-04"),
        candidate_times=("08:00", "09:00"),
        duration_minutes=12,
        participants=[
            # c0 fully inside (0), c1 shares only its last 2 minutes -> 10
            participant("alice", preferred_ranges=[{"start": "08:00", "end": "09:02"}]),
            # c0 fully outside (12), c1 fully inside (0)
            participant("bob", preferred_ranges=[{"start": "09:00", "end": "09:12"}]),
        ],
    )
    data = resp.json()
    assert data["meetings"][0]["deviations"] == {"alice": 0, "bob": 12}
    assert data["meetings"][1]["deviations"] == {"alice": 10, "bob": 0}
    assert [m["candidate_index"] for m in data["meetings"]] == [0, 1, 1]
    assert data["cumulative_costs"] == {"alice": 20, "bob": 12}
    assert data["max_deviation"] == 20
    assert data["total_deviation"] == 32


def test_input_order_is_final_tie_break():
    # No preferences: every schedule costs 0, so the earliest candidate in
    # input order must win on every date regardless of time ordering.
    resp = rotation(
        candidate_times=("16:00", "08:00"),
        participants=[participant("alice", preferred_ranges=[])],
    )
    assert resp.status_code == 200
    assert [m["candidate_index"] for m in resp.json()["meetings"]] == [0, 0]


def test_combination_count_is_reported():
    resp = rotation(
        candidate_times=("08:00", "10:00", "14:00"),
        participants=[participant("alice", preferred_ranges=[])],
    )
    assert resp.json()["combinations"] == 9  # 3 candidates on 2 dates


# ---------------------------------------------------------------------------
# Busy calendars reuse the expansion semantics
# ---------------------------------------------------------------------------


def test_busy_instance_removes_conflicting_candidate():
    # Alice busy 08:00-09:00 on the first date: that candidate is dropped
    # there (the second date keeps it), so day 1 must use 16:00.
    ics = cal(event_utc("a1", "20260302T080000Z", "20260302T090000Z"))
    resp = rotation(
        participants=[
            participant(
                "alice",
                ics=ics,
                preferred_ranges=[{"start": "08:00", "end": "17:00"}],
            ),
            participant("bob", preferred_ranges=[{"start": "08:00", "end": "17:00"}]),
        ]
    )
    assert resp.status_code == 200
    assert [m["candidate_index"] for m in resp.json()["meetings"]] == [1, 0]


def test_busy_from_any_participant_blocks():
    ics = cal(event_utc("b1", "20260302T160000Z", "20260302T170000Z"))
    resp = rotation(
        participants=[
            participant("alice", preferred_ranges=[]),
            participant("bob", ics=ics, preferred_ranges=[]),
        ]
    )
    assert resp.json()["meetings"][0]["candidate_index"] == 0


def test_touching_boundary_does_not_conflict():
    # Half-open [start, end): an event ending exactly at 08:00 is free.
    ics = cal(event_utc("a1", "20260302T070000Z", "20260302T080000Z"))
    resp = rotation(
        participants=[participant("alice", ics=ics, preferred_ranges=[])]
    )
    assert resp.json()["meetings"][0]["candidate_index"] == 0


def test_cancelled_and_transparent_instances_do_not_block():
    ics = cal(
        event_utc("a1", "20260302T080000Z", "20260302T090000Z", "STATUS:CANCELLED"),
        event_utc("a2", "20260302T080000Z", "20260302T090000Z", "TRANSP:TRANSPARENT"),
    )
    resp = rotation(
        participants=[participant("alice", ics=ics, preferred_ranges=[])]
    )
    assert resp.status_code == 200
    assert resp.json()["meetings"][0]["candidate_index"] == 0


def test_recurring_busy_is_expanded():
    ics = cal(
        "BEGIN:VEVENT\r\n"
        "UID:r1\r\nDTSTART:20260302T080000Z\r\nDTEND:20260302T090000Z\r\n"
        "RRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
    )
    resp = rotation(
        participants=[participant("alice", ics=ics, preferred_ranges=[])]
    )
    # 08:00 blocked on both dates; 16:00 remains.
    assert [m["candidate_index"] for m in resp.json()["meetings"]] == [1, 1]


def test_all_day_event_blocks_in_participant_timezone():
    # All-day March 5 in New York (UTC-5) covers 05:00Z–March 6 05:00Z.
    ics = cal(all_day("ad", "20260305", "20260306"))
    resp = rotation(
        organizer_timezone="UTC",
        dates=("2026-03-05",),
        candidate_times=("08:00",),
        participants=[
            participant("alice", timezone="America/New_York", ics=ics)
        ],
    )
    assert resp.status_code == 422
    assert "no feasible candidate" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DST handling in every timezone
# ---------------------------------------------------------------------------


def test_candidates_follow_organizer_dst():
    # America/New_York springs forward 2026-03-08: 09:00 local is 14:00Z
    # before, 13:00Z from the transition day onward.
    resp = rotation(
        organizer_timezone="America/New_York",
        dates=("2026-03-07", "2026-03-08"),
        candidate_times=("09:00",),
        participants=[participant("alice", timezone="UTC")],
    )
    meetings = resp.json()["meetings"]
    assert meetings[0]["start"] == "2026-03-07T14:00:00Z"
    assert meetings[0]["end"] == "2026-03-07T15:00:00Z"
    assert meetings[1]["start"] == "2026-03-08T13:00:00Z"
    assert meetings[1]["end"] == "2026-03-08T14:00:00Z"


def test_preferences_follow_participant_dst():
    # NY participant prefers 09:00-10:00 local. On the EST date that lines
    # up with 14:00Z (candidate 0); on the EDT date with 13:00Z (candidate 1).
    resp = rotation(
        organizer_timezone="UTC",
        dates=("2026-03-06", "2026-03-09"),
        candidate_times=("14:00", "13:00"),
        participants=[
            participant(
                "alice",
                timezone="America/New_York",
                preferred_ranges=[{"start": "09:00", "end": "10:00"}],
            )
        ],
    )
    meetings = resp.json()["meetings"]
    assert meetings[0]["candidate_index"] == 0
    assert meetings[0]["deviations"] == {"alice": 0}
    assert meetings[1]["candidate_index"] == 1
    assert meetings[1]["deviations"] == {"alice": 0}


def test_meeting_crossing_participant_midnight_splits_preference():
    # 23:30 UTC meeting on Mar 4 is 08:30 next-day in Tokyo (UTC+9); a
    # Tokyo preference of 08:00-09:00 covers the 08:30-09:30 local meeting
    # for 30 of the 60 minutes.
    resp = rotation(
        organizer_timezone="UTC",
        dates=("2026-03-04",),
        candidate_times=("23:30",),
        duration_minutes=60,
        participants=[
            participant(
                "alice",
                timezone="Asia/Tokyo",
                preferred_ranges=[{"start": "08:00", "end": "09:00"}],
            )
        ],
    )
    meeting = resp.json()["meetings"][0]
    assert meeting["start"] == "2026-03-04T23:30:00Z"
    assert meeting["deviations"] == {"alice": 30}


def test_participant_busy_calendar_uses_its_own_dst():
    # A fixed 09:00-10:00 New_York local event occupies 14:00Z on the EST
    # date (Mar 6) and 13:00Z on the EDT date (Mar 9). The same wall-clock
    # busy hour therefore blocks the DST-correct UTC candidate on each date.
    ics = cal(
        event_local("e1", "20260306T090000", "20260306T100000", "America/New_York"),
        event_local("e2", "20260309T090000", "20260309T100000", "America/New_York"),
    )
    est = rotation(
        dates=("2026-03-06",),
        candidate_times=("14:00",),
        participants=[participant("alice", timezone="America/New_York", ics=ics)],
    )
    assert est.status_code == 422
    assert "no feasible candidate" in est.json()["detail"]

    edt = rotation(
        dates=("2026-03-09",),
        candidate_times=("13:00",),
        participants=[participant("alice", timezone="America/New_York", ics=ics)],
    )
    assert edt.status_code == 422
    assert "no feasible candidate" in edt.json()["detail"]

    # The neighboring UTC instant stays free each date, proving the block
    # moves with the offset rather than covering a fixed UTC hour.
    free = rotation(
        dates=("2026-03-06",),
        candidate_times=("13:00",),
        participants=[participant("alice", timezone="America/New_York", ics=ics)],
    )
    assert free.status_code == 200

# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------


def test_no_feasible_candidate_on_any_date_returns_422():
    ics = cal(event_utc("a1", "20260302T073000Z", "20260302T173000Z"))
    resp = rotation(
        participants=[participant("alice", ics=ics, preferred_ranges=[])]
    )
    assert resp.status_code == 422
    assert "2026-03-02" in resp.json()["detail"]


def test_combination_overflow_returns_422():
    # 37^3 = 50653 > 50000.
    times = [f"{h:02d}:{m:02d}" for h in range(0, 19) for m in (0, 20, 40)][:37]
    resp = rotation(
        dates=("2026-03-02", "2026-03-03", "2026-03-04"),
        candidate_times=tuple(times),
        duration_minutes=15,
        participants=[participant("alice", preferred_ranges=[])],
    )
    assert resp.status_code == 422
    assert "50653" in resp.json()["detail"]
    assert "50000" in resp.json()["detail"]


def test_combination_count_at_the_limit_is_allowed():
    times = [f"{h:02d}:{m:02d}" for h in range(0, 18) for m in (0, 20, 40)][:36]
    resp = rotation(
        dates=("2026-03-02", "2026-03-03", "2026-03-04"),
        candidate_times=tuple(times),
        duration_minutes=15,
        participants=[participant("alice", preferred_ranges=[])],
    )
    assert resp.status_code == 200
    assert resp.json()["combinations"] == 46656


def test_unknown_organizer_timezone_returns_422():
    resp = rotation(
        organizer_timezone="Mars/Olympus_Mons",
        participants=[participant("alice")],
    )
    assert resp.status_code == 422
    assert "unknown organizer timezone" in resp.json()["detail"]


def test_unknown_participant_timezone_returns_422():
    resp = rotation(
        participants=[participant("alice", timezone="Mars/Olympus_Mons")]
    )
    assert resp.status_code == 422
    assert "alice" in resp.json()["detail"]


def test_duplicate_participant_id_returns_422():
    resp = rotation(
        participants=[
            participant("alice"),
            participant("alice"),
        ]
    )
    assert resp.status_code == 422
    assert "duplicate participant_id" in resp.json()["detail"]


def test_invalid_participant_ics_returns_422():
    resp = rotation(participants=[participant("alice", ics="not calendar text")])
    assert resp.status_code == 422
    assert "alice" in resp.json()["detail"]


def test_unknown_tzid_in_busy_calendar_returns_422():
    ics = cal(
        event_local("a1", "20260302T080000", "20260302T090000", "Mars/Olympus_Mons")
    )
    resp = rotation(participants=[participant("alice", ics=ics)])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_bad_transp_returns_422():
    ics = cal(
        event_utc("a1", "20260302T080000Z", "20260302T090000Z", "TRANSP:WAT")
    )
    resp = rotation(participants=[participant("alice", ics=ics)])
    assert resp.status_code == 422
    assert "TRANSP" in resp.json()["detail"]


def test_offset_bearing_candidate_time_returns_422():
    resp = rotation(
        candidate_times=("08:00+02:00",),
        participants=[participant("alice")],
    )
    assert resp.status_code == 422


def test_offset_bearing_preferred_time_returns_422():
    resp = rotation(
        participants=[
            participant("alice", preferred_ranges=[{"start": "09:00+02:00", "end": "10:00"}])
        ]
    )
    assert resp.status_code == 422


def test_inverted_preferred_range_returns_422():
    resp = rotation(
        participants=[
            participant("alice", preferred_ranges=[{"start": "10:00", "end": "10:00"}])
        ]
    )
    assert resp.status_code == 422
    assert "after start" in resp.text


def test_zero_duration_returns_422():
    resp = rotation(
        duration_minutes=0, participants=[participant("alice")]
    )
    assert resp.status_code == 422


def test_empty_dates_candidates_or_participants_returns_422():
    base = {
        "organizer_timezone": "UTC",
        "dates": ["2026-03-02"],
        "candidate_times": ["08:00"],
        "duration_minutes": 60,
        "participants": [participant("alice")],
    }
    for key, empty in (("dates", []), ("candidate_times", []), ("participants", [])):
        body = dict(base)
        body[key] = empty
        assert client.post("/fair-rotation", json=body).status_code == 422


def test_extra_field_returns_422():
    resp = client.post(
        "/fair-rotation",
        json={
            "organizer_timezone": "UTC",
            "dates": ["2026-03-02"],
            "candidate_times": ["08:00"],
            "duration_minutes": 60,
            "participants": [participant("alice")],
            "unexpected": True,
        },
    )
    assert resp.status_code == 422

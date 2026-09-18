"""Key tests for the /provenance endpoint.

Run from the repository root:  python -m pytest tests/test_provenance.py -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//provenance-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def provenance(
    ics: str,
    uid: str = "evt-1",
    window_start="2026-01-01T00:00:00Z",
    window_end="2026-02-01T00:00:00Z",
):
    return client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": uid,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def by_rid(response) -> dict[str, dict]:
    return {o["recurrence_id"]: o for o in response.json()["occurrences"]}


def rids(response) -> list[str]:
    return [o["recurrence_id"] for o in response.json()["occurrences"]]


# ---------------------------------------------------------------------------
# Generation: DTSTART / RRULE / RDATE
# ---------------------------------------------------------------------------


def test_non_recurring_event_is_generated_by_dtstart():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:one off",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200
    occ = resp.json()["occurrences"]
    assert len(occ) == 1
    item = occ[0]
    assert item["generated_by"] == [{"kind": "DTSTART", "rrule": None}]
    assert item["excluded_by"] == []
    assert item["override"] is None
    assert item["scheduled_start"] == "2026-01-05T09:00:00Z"
    assert item["scheduled_end"] == "2026-01-05T10:00:00Z"
    assert item["final_start"] == "2026-01-05T09:00:00Z"
    assert item["final_end"] == "2026-01-05T10:00:00Z"
    assert item["final_status"] == "CONFIRMED"
    assert item["recurrence_id"] == "2026-01-05T09:00:00Z"


def test_rrule_occurrences_quote_rule_text():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200
    assert rids(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    for item in resp.json()["occurrences"]:
        assert item["generated_by"] == [
            {"kind": "RRULE", "rrule": "FREQ=DAILY;COUNT=3"}
        ]
        assert item["final_status"] == "CONFIRMED"
        assert item["final_start"] == item["scheduled_start"]


def test_rdate_only_seed_is_dtstart_extra_is_rdate():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RDATE:20260120T090000Z,20260121T090000Z",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200
    items = by_rid(resp)
    assert [g["kind"] for g in items["2026-01-05T09:00:00Z"]["generated_by"]] == [
        "DTSTART"
    ]
    assert [g["kind"] for g in items["2026-01-20T09:00:00Z"]["generated_by"]] == [
        "RDATE"
    ]
    assert [g["kind"] for g in items["2026-01-21T09:00:00Z"]["generated_by"]] == [
        "RDATE"
    ]


def test_rdate_coinciding_with_rrule_reports_both_sources():
    # The RDATE duplicates an RRULE instance; both authored properties
    # generate the slot, so both are listed (canonical order RRULE first).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "RDATE:20260106T090000Z",
    ))
    resp = provenance(ics)
    item = by_rid(resp)["2026-01-06T09:00:00Z"]
    assert [g["kind"] for g in item["generated_by"]] == ["RRULE", "RDATE"]
    # The DTSTART instance, in contrast, is covered by the rule alone.
    seed = by_rid(resp)["2026-01-05T09:00:00Z"]
    assert [g["kind"] for g in seed["generated_by"]] == ["RRULE"]


def test_sources_honor_dst_and_tzid():
    # America/New_York: EST until 2026-03-08, then EDT.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260301T090000",
        "DTEND;TZID=America/New_York:20260301T100000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    resp = provenance(ics, window_start="2026-02-25T00:00:00Z",
                      window_end="2026-04-01T00:00:00Z")
    assert rids(resp) == [
        "2026-03-01T14:00:00Z",
        "2026-03-08T13:00:00Z",
        "2026-03-15T13:00:00Z",
    ]
    first = resp.json()["occurrences"][0]
    assert first["scheduled_end"] == "2026-03-01T15:00:00Z"


# ---------------------------------------------------------------------------
# EXDATE exclusion
# ---------------------------------------------------------------------------


def test_exdate_excluded_occurrence_has_no_final_but_keeps_chain():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000Z",
    ))
    resp = provenance(ics)
    assert rids(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    excluded = by_rid(resp)["2026-01-06T09:00:00Z"]
    assert [g["kind"] for g in excluded["generated_by"]] == ["RRULE"]
    assert excluded["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert excluded["override"] is None
    assert excluded["final_start"] is None
    assert excluded["final_end"] is None
    assert excluded["final_status"] is None
    # The surviving neighbors still carry final values.
    alive = by_rid(resp)["2026-01-07T09:00:00Z"]
    assert alive["excluded_by"] == []
    assert alive["final_status"] == "CONFIRMED"


def test_exdate_with_tzid_matches_utc_slot():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Europe/Berlin:20260105T100000",
        "DTEND;TZID=Europe/Berlin:20260105T110000",
        "RRULE:FREQ=DAILY;COUNT=2",
        "EXDATE;TZID=Europe/Berlin:20260106T100000",  # = 09:00Z
    ))
    resp = provenance(ics)
    item = by_rid(resp)["2026-01-06T09:00:00Z"]
    assert item["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert item["final_start"] is None


def test_stray_exdate_naming_no_occurrence_is_not_a_candidate():
    # The EXDATE names Jan 25, which neither RRULE nor RDATE generates.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "EXDATE:20260125T090000Z",
    ))
    resp = provenance(ics)
    assert rids(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
    ]


def test_exdate_with_resurrecting_override_is_both_excluded_and_final():
    # An override at an EXDATEd slot wins (matches the expander), but the
    # provenance still records that the slot was also EXDATEd.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
            "EXDATE:20260106T090000Z",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
        ),
    )
    resp = provenance(ics)
    item = by_rid(resp)["2026-01-06T09:00:00Z"]
    assert item["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert item["override"] == {
        "action": "RESCHEDULED",
        "recurrence_id": "2026-01-06T09:00:00Z",
        "start": "2026-01-06T15:00:00Z",
        "end": "2026-01-06T16:00:00Z",
        "status": "CONFIRMED",
    }
    assert item["final_start"] == "2026-01-06T15:00:00Z"
    assert item["final_end"] == "2026-01-06T16:00:00Z"
    assert item["final_status"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# RECURRENCE-ID reschedule / modify / cancel
# ---------------------------------------------------------------------------


def test_rescheduled_override_keeps_scheduled_slot_and_shows_final_slot():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",  # Mondays
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",  # Jan 5, 12, 19
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260113T100000Z",
            "DTEND:20260113T110000Z",
            "SUMMARY:moved",
        ),
    )
    resp = provenance(ics)
    assert resp.status_code == 200
    item = by_rid(resp)["2026-01-12T09:00:00Z"]
    assert item["scheduled_start"] == "2026-01-12T09:00:00Z"
    assert item["scheduled_end"] == "2026-01-12T10:00:00Z"
    assert item["override"]["action"] == "RESCHEDULED"
    assert item["override"]["start"] == "2026-01-13T10:00:00Z"
    assert item["override"]["end"] == "2026-01-13T11:00:00Z"
    assert item["final_start"] == "2026-01-13T10:00:00Z"
    assert item["final_end"] == "2026-01-13T11:00:00Z"
    assert item["final_status"] == "CONFIRMED"


def test_in_place_edit_is_action_modified():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=1",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260105T090000Z",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T103000Z",
            "SUMMARY:longer",
        ),
    )
    resp = provenance(ics)
    item = resp.json()["occurrences"][0]
    assert item["override"]["action"] == "MODIFIED"
    assert item["final_end"] == "2026-01-05T10:30:00Z"


def test_cancelled_override_reports_cancelled_status_without_times():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = provenance(ics)
    item = by_rid(resp)["2026-01-12T09:00:00Z"]
    assert item["override"]["action"] == "CANCELLED"
    assert item["override"]["start"] is None
    assert item["override"]["end"] is None
    assert item["override"]["status"] == "CANCELLED"
    assert item["final_start"] is None
    assert item["final_end"] is None
    assert item["final_status"] == "CANCELLED"
    # The untouched sibling survives.
    other = by_rid(resp)["2026-01-05T09:00:00Z"]
    assert other["final_status"] == "CONFIRMED"


def test_cancelled_master_marks_every_occurrence_cancelled():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "STATUS:CANCELLED",
    ))
    resp = provenance(ics)
    assert rids(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
    ]
    for item in resp.json()["occurrences"]:
        assert item["final_start"] is None
        assert item["final_end"] is None
        assert item["final_status"] == "CANCELLED"


def test_orphan_override_moved_into_window_has_empty_generation():
    # RID (March 2) is not generated by this series (only Jan 5 and 12);
    # the override moves it into the January window, so it is included
    # with no generation source and scheduled times inferred from the master.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    resp = provenance(ics)
    item = by_rid(resp)["2026-03-02T09:00:00Z"]
    assert item["generated_by"] == []
    assert item["scheduled_start"] == "2026-03-02T09:00:00Z"
    assert item["scheduled_end"] == "2026-03-02T10:00:00Z"
    assert item["override"]["action"] == "RESCHEDULED"
    assert item["final_start"] == "2026-01-15T12:00:00Z"
    assert item["final_status"] == "CONFIRMED"


def test_generated_override_moved_into_window_quotes_rrule_source():
    # COUNT=10 generates March 2 (Jan 5 + 9 weeks); even though that slot
    # lies past the window, the point lookup against the RRULE attributes it.
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
    resp = provenance(ics)
    item = by_rid(resp)["2026-03-02T09:00:00Z"]
    assert [g["kind"] for g in item["generated_by"]] == ["RRULE"]


def test_orphan_override_fully_outside_window_is_omitted():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",  # Jan 5, 12
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260303T090000Z",
            "DTEND:20260303T100000Z",
        ),
    )
    resp = provenance(ics)
    assert rids(resp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
    ]


def test_moved_out_occurrence_stays_explained_via_scheduled_slot():
    # Jan 12 moved to March: scheduled slot overlaps the window, so the
    # chain is returned even though the final slot is outside.
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
    resp = provenance(ics)
    item = by_rid(resp)["2026-01-12T09:00:00Z"]
    assert item["override"]["action"] == "RESCHEDULED"
    assert item["final_start"] == "2026-03-10T09:00:00Z"


# ---------------------------------------------------------------------------
# Ordering and response envelope
# ---------------------------------------------------------------------------


def test_occurrences_stably_sorted_by_effective_instance_time():
    # The moved instance (scheduled Jan 12) lives on Jan 13 10:00, after
    # the Jan 13 09:00 series occurrence -- sorting uses the final time.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=10",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260113T100000Z",
            "DTEND:20260113T110000Z",
        ),
    )
    resp = provenance(ics, window_start="2026-01-12T00:00:00Z",
                      window_end="2026-01-14T00:00:00Z")
    ordered = [
        (o["recurrence_id"], o["final_start"]) for o in resp.json()["occurrences"]
    ]
    assert ordered == [
        ("2026-01-13T09:00:00Z", "2026-01-13T09:00:00Z"),
        ("2026-01-12T09:00:00Z", "2026-01-13T10:00:00Z"),
    ]
    # Sorting uses the effective time; the rescheduled candidate keeps its
    # identity (the Jan 12 RECURRENCE-ID) while appearing at the new slot.
    assert ordered[1][0] == "2026-01-12T09:00:00Z"


def test_response_envelope_echoes_window_uid_count():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ))
    resp = provenance(ics)
    body = resp.json()
    assert body["uid"] == "evt-1"
    assert body["window_start"] == "2026-01-01T00:00:00Z"
    assert body["window_end"] == "2026-02-01T00:00:00Z"
    assert body["count"] == len(body["occurrences"]) == 2


def test_other_uids_in_calendar_are_ignored():
    ics = cal(
        event("UID:other", "DTSTART:20260105T090000Z",
              "DTEND:20260105T100000Z", "RRULE:FREQ=DAILY;COUNT=5"),
        event("UID:evt-1", "DTSTART:20260105T090000Z",
              "DTEND:20260105T100000Z"),
    )
    resp = provenance(ics)
    assert resp.status_code == 200
    assert rids(resp) == ["2026-01-05T09:00:00Z"]
    assert all(o["recurrence_id"] == "2026-01-05T09:00:00Z"
               for o in resp.json()["occurrences"])


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------


def test_unknown_uid_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = provenance(ics, uid="missing")
    assert resp.status_code == 422
    assert "missing" in resp.json()["detail"]


def test_unparseable_ics_returns_422():
    resp = provenance("not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_invalid_window_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = provenance(
        ics, window_start="2026-02-01T00:00:00Z",
        window_end="2026-01-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_invalid_rrule_returns_422():
    ics = cal(event(
        "UID:evt-1", "DTSTART:20260105T090000Z", "RRULE:FREQ=BOGUS;COUNT=2"
    ))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "invalid RRULE" in resp.json()["detail"]


def test_unknown_tzid_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_missing_uid_field_returns_422():
    resp = client.post(
        "/provenance",
        json={
            "ics": cal(event("UID:evt-1", "DTSTART:20260105T090000Z")),
            "window_start": "2026-01-01T00:00:00Z",
            "window_end": "2026-02-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Extra edge cases
# ---------------------------------------------------------------------------


def test_floating_times_are_interpreted_as_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000",
        "DTEND:20260105T100000",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200
    items = by_rid(resp)
    assert set(items) == {
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T09:00:00Z",
    }
    assert items["2026-01-06T09:00:00Z"]["excluded_by"] == [
        "2026-01-06T09:00:00Z"
    ]


def test_date_valued_series_uses_midnight_utc_slots():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;VALUE=DATE:20260105",
        "DTEND;VALUE=DATE:20260106",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE;VALUE=DATE:20260106",
    ))
    resp = provenance(ics)
    items = by_rid(resp)
    excluded = items["2026-01-06T00:00:00Z"]
    assert excluded["excluded_by"] == ["2026-01-06T00:00:00Z"]
    assert excluded["scheduled_end"] == "2026-01-07T00:00:00Z"
    assert excluded["final_status"] is None
    assert items["2026-01-05T00:00:00Z"]["final_status"] == "CONFIRMED"


def test_orphan_cancelled_override_is_explained_when_scheduled_in_window():
    # Non-recurring master; the orphan CANCELLED override still reports
    # the chain (empty generation, null times, CANCELLED status).
    ics = cal(
        event("UID:evt-1", "DTSTART:20260105T090000Z",
              "DTEND:20260105T100000Z"),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T090000Z",
            "DTEND:20260106T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = provenance(ics)
    items = by_rid(resp)
    orphan = items["2026-01-06T09:00:00Z"]
    assert orphan["generated_by"] == []
    assert orphan["override"]["action"] == "CANCELLED"
    assert orphan["final_start"] is None
    assert orphan["final_status"] == "CANCELLED"
    assert items["2026-01-05T09:00:00Z"]["final_status"] == "CONFIRMED"


def test_multiple_rrules_are_listed_per_rule_with_rrule_text():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3",
    ))
    resp = provenance(ics)
    items = by_rid(resp)
    # Jan 5 (Monday) is generated by both rules, in source order.
    assert [g["kind"] for g in items["2026-01-05T09:00:00Z"]["generated_by"]] == [
        "RRULE",
        "RRULE",
    ]
    assert all(
        g["rrule"].startswith("FREQ=")
        for g in items["2026-01-05T09:00:00Z"]["generated_by"]
    )
    assert [g["rrule"] for g in items["2026-01-06T09:00:00Z"]["generated_by"]] == [
        "FREQ=DAILY;COUNT=2"
    ]


def test_thisandfuture_range_returns_422():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;RANGE=THISANDFUTURE:20260106T090000Z",
            "DTSTART:20260106T100000Z",
        ),
    )
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "THISANDFUTURE" in resp.json()["detail"]


def test_empty_uid_string_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = provenance(ics, uid="")
    assert resp.status_code == 422

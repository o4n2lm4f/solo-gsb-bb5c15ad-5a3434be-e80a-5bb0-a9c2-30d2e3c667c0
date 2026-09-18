"""Key tests for the /compress-schedule endpoint.

Every chosen representation is verified two ways: its declared
strategy/exception metadata and a round trip through POST /expand that
must reproduce the exact input start and end sets.
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.compressor import FALLBACK_STRATEGY, compress_instances
from app.main import app

client = TestClient(app)

UTC = dt.timezone.utc
WIN_START = "2025-06-01T00:00:00Z"
WIN_END = "2026-12-31T00:00:00Z"


def inst(starts: list[str], hour="09:00:00", length_hours=1):
    """Build equal-length one-hour instances from ISO date strings."""
    out = []
    h, m, s = (int(x) for x in hour.split(":"))
    for day in starts:
        start = dt.datetime.fromisoformat(day).replace(
            hour=h, minute=m, second=s, tzinfo=UTC
        )
        out.append((start, start + dt.timedelta(hours=length_hours)))
    return out


def compress(starts: list[str], uid="series-1", **kw):
    pairs = inst(starts, **kw)
    result = compress_instances(uid, pairs)
    return result, pairs


def roundtrip(ics: str):
    resp = client.post(
        "/expand",
        json={"ics": ics, "window_start": WIN_START, "window_end": WIN_END},
    )
    assert resp.status_code == 200, resp.text
    return [(i["start"], i["end"]) for i in resp.json()["instances"]]


def iso(pairs):
    return sorted(
        (s.isoformat().replace("+00:00", "Z"), e.isoformat().replace("+00:00", "Z"))
        for s, e in pairs
    )


def assert_roundtrips(result, pairs):
    assert roundtrip(result.ics) == iso(pairs)


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def test_contiguous_daily_series_prefers_rrule_daily():
    result, pairs = compress(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    )
    assert result.strategy == "RRULE:FREQ=DAILY;INTERVAL=1"
    assert result.exception_count == 0
    assert "EXDATE" not in result.ics and "RDATE" not in result.ics
    assert_roundtrips(result, pairs)


def test_weekly_series_prefers_weekly_rrule():
    # Four Mondays, one week apart: WEEKLY/1 beats every other candidate.
    result, pairs = compress(["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"])
    assert result.strategy == "RRULE:FREQ=WEEKLY;INTERVAL=1"
    assert result.exception_count == 0
    assert_roundtrips(result, pairs)


def test_biweekly_series_prefers_interval_2():
    result, pairs = compress(
        ["2026-01-05", "2026-01-19", "2026-02-02", "2026-02-16"]
    )
    assert result.strategy == "RRULE:FREQ=WEEKLY;INTERVAL=2"
    assert result.exception_count == 0
    assert_roundtrips(result, pairs)


def test_every_n_days_intervals_3_and_4():
    result3, pairs3 = compress(
        ["2026-01-05", "2026-01-08", "2026-01-11", "2026-01-14"]
    )
    assert result3.strategy == "RRULE:FREQ=DAILY;INTERVAL=3"
    assert result3.exception_count == 0
    assert_roundtrips(result3, pairs3)

    result4, pairs4 = compress(
        ["2026-01-05", "2026-01-09", "2026-01-13", "2026-01-17"]
    )
    assert result4.strategy == "RRULE:FREQ=DAILY;INTERVAL=4"
    assert result4.exception_count == 0
    assert_roundtrips(result4, pairs4)


def test_missing_grid_points_become_exdates():
    # Three Mon–Fri weeks (14 weekdays) with Wednesday Jan 14 skipped: the
    # DAILY/1 grid generates every day, so the two weekend days per week and
    # the skipped Wednesday are written as EXDATEs.
    days = [
        f"2026-01-{d:02d}"
        for d in (5, 6, 7, 8, 9, 12, 13, 15, 16, 19, 20, 21, 22, 23)
    ]
    result, pairs = compress(days)
    assert result.strategy == "RRULE:FREQ=DAILY;INTERVAL=1"
    assert result.exception_count == 5
    for stamp in (
        "20260110T090000Z",
        "20260111T090000Z",
        "20260114T090000Z",
        "20260117T090000Z",
        "20260118T090000Z",
    ):
        assert stamp in result.ics
    assert "RDATE" not in result.ics
    assert_roundtrips(result, pairs)


def test_out_of_grid_instances_become_rdates():
    # Long DAILY/2 grid Jan 5..17 (odd steps) plus the off-grid Jan 6,
    # which must be carried as RDATE.
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09",
            "2026-01-11", "2026-01-13", "2026-01-15", "2026-01-17"]
    result, pairs = compress(days)
    assert result.strategy == "RRULE:FREQ=DAILY;INTERVAL=2"
    assert result.exception_count == 1
    assert "RDATE:20260106T090000Z" in result.ics
    assert "EXDATE" not in result.ics
    assert_roundtrips(result, pairs)


def test_mixed_exdates_and_rdates():
    # DAILY/2 grid 5,7,9,11,13,15,17 with UNTIL at the latest start (Jan 18):
    # Jan 11 is a gap (EXDATE); Jan 6 and Jan 18 are off-grid (RDATE).
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09",
            "2026-01-13", "2026-01-15", "2026-01-17", "2026-01-18"]
    result, pairs = compress(days)
    assert result.strategy == "RRULE:FREQ=DAILY;INTERVAL=2"
    assert result.exception_count == 3
    assert "EXDATE:20260111T090000Z" in result.ics
    assert "RDATE:20260106T090000Z,20260118T090000Z" in result.ics
    assert_roundtrips(result, pairs)


def test_dtstart_until_are_earliest_and_latest_starts():
    result, pairs = compress(
        ["2026-01-26", "2026-01-05", "2026-01-12", "2026-01-19"]
    )
    assert result.strategy == "RRULE:FREQ=WEEKLY;INTERVAL=1"
    assert "DTSTART:20260105T090000Z" in result.ics
    assert "UNTIL=20260126T090000Z" in result.ics
    assert_roundtrips(result, pairs)


def test_single_instance_falls_back_to_rdate():
    result, pairs = compress(["2026-01-05"])
    assert result.strategy == FALLBACK_STRATEGY
    assert result.exception_count == 0
    assert "RRULE" not in result.ics and "EXDATE" not in result.ics
    assert_roundtrips(result, pairs)


def test_small_irregular_set_falls_back_to_rdate():
    # Two sparse instances: listing one RDATE is shorter than any RRULE.
    result, pairs = compress(["2026-01-05", "2026-01-20"])
    assert result.strategy == FALLBACK_STRATEGY
    assert result.exception_count == 1
    assert "RRULE" not in result.ics
    assert "RDATE:20260120T090000Z" in result.ics
    assert_roundtrips(result, pairs)


def test_durations_are_preserved():
    pairs = inst(["2026-01-05", "2026-01-06"], hour="13:30:00", length_hours=2)
    result = compress_instances("dur-1", pairs)
    assert "DTSTART:20260105T133000Z" in result.ics
    assert "DTEND:20260105T153000Z" in result.ics
    assert roundtrip(result.ics) == iso(pairs)


# ---------------------------------------------------------------------------
# Byte-minimality and tie-breaking
# ---------------------------------------------------------------------------


def test_winner_has_fewest_bytes_of_all_legal_candidates():
    from app.compressor import (
        FREQUENCIES,
        INTERVALS,
        _build_ics,
        _grid,
    )

    pairs = inst(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09", "2026-01-12"]
    )
    starts = sorted(s for s, _ in pairs)
    first, last = starts[0], starts[-1]
    duration = pairs[0][1] - pairs[0][0]
    actual = set(starts)

    candidate_sizes = []
    for freq in FREQUENCIES:
        for interval in INTERVALS:
            step = dt.timedelta(
                **{"weeks" if freq == "WEEKLY" else "days": interval}
            )
            grid = set(_grid(first, last, step))
            exdates = sorted(grid - actual)
            rdates = sorted(actual - grid)
            ics = _build_ics(
                "series-1", first, duration, last, freq, interval, exdates, rdates
            )
            candidate_sizes.append(len(ics))
    fallback = _build_ics(
        "series-1", first, duration, last, None, None, [], starts[1:]
    )
    candidate_sizes.append(len(fallback))

    result = compress_instances("series-1", pairs)
    assert len(result.ics.encode("utf-8")) == min(candidate_sizes)


def test_tie_prefers_fewer_exceptions_then_rrule_daily_smaller_interval():
    from app.compressor import _Candidate

    def candidate(strategy, size, exc, rrule=True, freq=0, interval=1):
        return _Candidate(strategy, exc, b"x" * size, rrule, freq, interval)

    chosen = min(
        [
            candidate("bigger", 300, 0),
            candidate("smaller", 200, 9),
        ],
        key=_Candidate.sort_key,
    )
    assert chosen.strategy == "smaller"  # bytes dominate

    chosen = min(
        [
            candidate("more-exc", 200, 3),
            candidate("less-exc", 200, 1),
        ],
        key=_Candidate.sort_key,
    )
    assert chosen.strategy == "less-exc"

    chosen = min(
        [
            candidate("rdates", 200, 1, rrule=False, freq=2, interval=5),
            candidate("rrule", 200, 1, rrule=True, freq=1, interval=4),
        ],
        key=_Candidate.sort_key,
    )
    assert chosen.strategy == "rrule"

    chosen = min(
        [
            candidate("weekly", 200, 1, freq=1, interval=1),
            candidate("daily", 200, 1, freq=0, interval=4),
        ],
        key=_Candidate.sort_key,
    )
    assert chosen.strategy == "daily"

    chosen = min(
        [
            candidate("iv4", 200, 1, freq=0, interval=4),
            candidate("iv1", 200, 1, freq=0, interval=1),
        ],
        key=_Candidate.sort_key,
    )
    assert chosen.strategy == "iv1"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_endpoint_response_shape_and_expand_roundtrip():
    payload = {
        "uid": "http-1",
        "instances": [
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
            {"start": "2026-01-12T09:00:00Z", "end": "2026-01-12T10:00:00Z"},
            {"start": "2026-01-19T09:00:00Z", "end": "2026-01-19T10:00:00Z"},
            {"start": "2026-01-26T09:00:00Z", "end": "2026-01-26T10:00:00Z"},
        ],
    }
    resp = client.post("/compress-schedule", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"strategy", "exception_count", "ics"}
    assert body["strategy"] == "RRULE:FREQ=WEEKLY;INTERVAL=1"
    assert body["exception_count"] == 0

    expanded = client.post(
        "/expand",
        json={
            "ics": body["ics"],
            "window_start": "2026-01-01T00:00:00Z",
            "window_end": "2026-02-01T00:00:00Z",
        },
    ).json()["instances"]
    assert [(i["start"], i["end"]) for i in expanded] == [
        (x["start"], x["end"]) for x in payload["instances"]
    ]
    assert {i["uid"] for i in expanded} == {"http-1"}


# ---------------------------------------------------------------------------
# Validation: 422
# ---------------------------------------------------------------------------


def post(instances, uid="series-1"):
    return client.post(
        "/compress-schedule", json={"uid": uid, "instances": instances}
    )


def one(date="2026-01-05", sh="09:00:00Z", eh="10:00:00Z"):
    return {"start": f"{date}T{sh}", "end": f"{date}T{eh}"}


def test_empty_uid_is_422():
    assert post([one()], uid="").status_code == 422


def test_empty_instances_is_422():
    assert post([]).status_code == 422


def test_naive_datetime_is_422():
    resp = post(
        [{"start": "2026-01-05T09:00:00", "end": "2026-01-05T10:00:00"}]
    )
    assert resp.status_code == 422


def test_non_utc_offset_datetime_is_422():
    resp = post(
        [
            {
                "start": "2026-01-05T10:00:00+01:00",
                "end": "2026-01-05T11:00:00+01:00",
            }
        ]
    )
    assert resp.status_code == 422


def test_end_not_after_start_is_422():
    resp = post([one(eh="09:00:00Z")])
    assert resp.status_code == 422
    assert "later than its start" in resp.json()["detail"]


def test_duplicate_starts_are_422():
    resp = post(
        [
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
        ]
    )
    assert resp.status_code == 422
    assert "duplicate instance start" in resp.json()["detail"]


def test_overlapping_instances_are_422():
    resp = post(
        [
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
            {"start": "2026-01-05T09:30:00Z", "end": "2026-01-05T10:30:00Z"},
        ]
    )
    assert resp.status_code == 422
    assert "overlap" in resp.json()["detail"]


def test_overlap_detected_for_unsorted_input():
    resp = post(
        [
            {"start": "2026-01-05T09:30:00Z", "end": "2026-01-05T10:30:00Z"},
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
        ]
    )
    assert resp.status_code == 422


def test_unequal_lengths_are_422():
    resp = post(
        [
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
            {"start": "2026-01-06T09:00:00Z", "end": "2026-01-06T09:30:00Z"},
        ]
    )
    assert resp.status_code == 422
    assert "equal duration" in resp.json()["detail"]


def test_touching_equal_length_intervals_are_allowed():
    resp = post(
        [
            {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
            {"start": "2026-01-05T10:00:00Z", "end": "2026-01-05T11:00:00Z"},
        ]
    )
    assert resp.status_code == 200


def test_zero_duration_instance_is_422():
    resp = post([one(sh="09:00:00Z", eh="09:00:00Z")])
    assert resp.status_code == 422

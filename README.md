# iCalendar Recurrence Expansion & Availability API

A local HTTP API that expands iCalendar (RFC 5545) recurring events into
concrete UTC instances inside a query window, and finds the **common free
time** across several named calendars when intersected with per-weekday
local working hours.

- Expands `DTSTART` + `RRULE` + `RDATE` − `EXDATE`
- `/provenance` traces one series (`UID`) inside a window and returns each
  candidate occurrence's chain: the authored `RRULE`/`RDATE`/`DTSTART`
  sources that generate it, every matching `EXDATE`, the `RECURRENCE-ID`
  override that reschedules/modifies/cancels it, and the final effective
  `DTSTART`/`DTEND`/`STATUS` — including candidates removed by `EXDATE` or
  cancellation, stably sorted by effective instance time
- Merges overrides by `UID` + `RECURRENCE-ID`: an override can **reschedule**
  an instance (judged against the window by its *actual* new start/end; the
  original slot is never kept) or **cancel** it via `STATUS:CANCELLED`
- Interprets `TZID` as IANA timezones with correct DST handling
- `/agenda` renders the same expanded instances as a **local agenda** for an
  IANA display timezone: timed events crossing local midnight are split into
  per-date fragments carrying `continues_before`/`continues_after`, while
  `DATE` all-day events are returned day by day on their authored calendar
  dates (never shifted by UTC offsets); entries are ordered by local date,
  all-day-first, then start and `UID`
- `/availability` builds working hours per **local date** (DST-correct),
  clips all busy instances to them, merges half-open intervals (adjacent
  included), and returns the complement shared by every calendar
- `/conflicts` scans named calendars together and reports the UTC segments
  where at least `minimum_concurrency` instances overlap, with the
  participating instances for each segment
- `/calendar-diff` compares two ICS versions over one UTC window by
  `UID + RECURRENCE-ID` and returns identity-sorted `added`/`removed`/
  `modified` instances (with `before`/`after`)
- `/alarms` expands `VALARM` reminders for every instance in a window:
  a master event's alarms apply to each recurring instance, overrides
  carry their own, and `REPEAT`/`DURATION` fan alarms out
- `/attendance` reports each windowed instance's `ATTENDEE` responses:
  recurring instances inherit the master's attendees, a `RECURRENCE-ID`
  override that declares any `ATTENDEE` replaces the whole list (one
  without keeps inheriting), with `PARTSTAT` counts and stable address
  ordering
- `/validate` structurally checks raw iCalendar text and always replies
  `200` — even unparsable input — with a sorted list of stable-coded
  diagnostics (missing `UID`/`DTSTART`, `DTEND` + `DURATION`, inverted
  times, duplicate master events, orphan overrides)
- `/compress-schedule` goes the other way: it compresses a set of
  equal-length UTC instances into the **shortest** complete ICS, trying
  `DAILY`/`WEEKLY` rules with `INTERVAL` 1–4 (grid gaps → `EXDATE`,
  out-of-grid instances → `RDATE`) plus a `DTSTART`+`RDATE` fallback, and
  guarantees the result expands back to exactly the same instances
- `/freebusy-export` turns several calendars (`calendar_id` + ICS + IANA
  timezone) into one **VFREEBUSY** text each over a shared UTC window:
  `STATUS:CANCELLED` and `TRANSP:TRANSPARENT` instances never block time
  (recurring instances inherit the master's `TRANSP`; an override that
  declares `TRANSP` replaces it), `DATE` all-day events occupy local full
  days in the calendar's own timezone (DST-correct), and the clipped busy
  intervals are merged into sorted `FREEBUSY` lines
- `/fair-rotation` schedules a **rotating cross-timezone meeting series**:
  given the organizer's timezone, one candidate local start time list per
  organizer-local date, a duration, and participants (IANA timezone + busy
  ICS + preferred local periods), it picks one feasible candidate per date
  — jointly across all dates — minimizing first the largest per-participant
  cumulative deviation from the preferred periods, then the total
  deviation, then candidate input order
- Results sorted by UTC start time, then `UID` (stable)
- Clear `422` errors for invalid rules, unknown timezones, or bad input

Stack: Python · FastAPI · icalendar · python-dateutil · zoneinfo

## Quick start (Docker Compose)

```bash
docker compose up --build
```

The API listens on **http://localhost:8000** (interactive docs at `/docs`,
health check at `/health`).

## API

### `POST /expand`

Request body:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

An instance is returned when it **overlaps** `[window_start, window_end)`
(a zero-duration event must start inside the window). Each instance carries
`uid`, `recurrence_id` (UTC, `null` for non-recurring events), `summary`,
`start`, `end`, and `status`; all timestamps are UTC (`Z`).

### Example

```bash
curl -s http://localhost:8000/expand \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART;TZID=America/New_York:20260301T090000\r\nDTEND;TZID=America/New_York:20260301T100000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=America/New_York:20260308T090000\r\nSUMMARY:Weekly sync\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nRECURRENCE-ID;TZID=America/New_York:20260315T090000\r\nDTSTART;TZID=America/New_York:20260316T110000\r\nDTEND;TZID=America/New_York:20260316T120000\r\nSUMMARY:Weekly sync (moved)\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-02-25T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

Response — note the DST shift (14:00Z → 13:00Z), the `EXDATE`-removed
March 8 occurrence, and the March 15 instance replaced by its override at
the new time (the original slot is gone):

```json
{
  "window_start": "2026-02-25T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "count": 2,
  "instances": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-01T14:00:00Z",
      "summary": "Weekly sync",
      "start": "2026-03-01T14:00:00Z",
      "end": "2026-03-01T15:00:00Z",
      "status": "CONFIRMED"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-15T13:00:00Z",
      "summary": "Weekly sync (moved)",
      "start": "2026-03-16T15:00:00Z",
      "end": "2026-03-16T16:00:00Z",
      "status": "CONFIRMED"
    }
  ]
}
```

### `POST /provenance`

Explain **where every candidate occurrence of one series comes from**. The
request takes the same `ics` and UTC window as `/expand`, plus the `uid` of
one `VEVENT` series; other `UID`s in the calendar are ignored.

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `uid`          | string | `UID` of the series to trace (non-empty)           |

Each element of `occurrences` is one candidate occurrence of the series,
stably sorted by its effective instance time (final `start`, else the
scheduled slot), then `RECURRENCE-ID`:

- `recurrence_id` — the occurrence's scheduled slot in UTC (its identity);
- `generated_by` — authored sources generating the slot, in canonical
  order `RRULE` (with the unfolded rule text in `rrule`), `RDATE`, then
  `DTSTART` alone (an `RDATE` duplicating an `RRULE` hit lists both; the
  `DTSTART` seed of a ruled series is covered by the rule and not repeated);
- `excluded_by` — UTC instants of every `EXDATE` matching the slot
  (`TZID`-local values are normalized); a stray `EXDATE` that names no
  generated slot is not an occurrence;
- `override` — the `RECURRENCE-ID` component applied to the slot, with
  `action` `RESCHEDULED` (non-cancelled, new start), `MODIFIED`
  (non-cancelled, same start — e.g. an end-time or summary edit) or
  `CANCELLED`, plus the override's own start/end/status;
- `scheduled_start`/`scheduled_end` — the slot the series generates
  (`null` only for an override whose `RECURRENCE-ID` the master never
  generates);
- `final_start`/`final_end`/`final_status` — the effective values after
  every transformation. They equal the scheduled values for an untouched
  occurrence; an `EXDATE` exclusion leaves all three `null`; a `CANCELLED`
  master or override yields `final_status: "CANCELLED"` with null times;
  an override at an `EXDATEd` slot still wins, so the chain records both
  the exclusion and the final values.

A candidate is included when **either** its scheduled slot **or** its
final slot overlaps `[window_start, window_end)`: an occurrence moved out
of the window keeps its chain (visible via the scheduled slot), and an
occurrence moved into the window from a slot the series never generates
appears with an empty `generated_by`. The window and calendar validation
of `/expand` is reused unchanged, and a `uid` absent from the calendar
returns `422`.

### Example

```bash
curl -s http://localhost:8000/provenance \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE:20260112T090000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nRECURRENCE-ID:20260119T090000Z\r\nDTSTART:20260120T110000Z\r\nDTEND:20260120T120000Z\r\nSUMMARY:moved\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "uid": "weekly-1",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

Response — Jan 5 survives unchanged, Jan 12 is reported *with* its
generation source despite being `EXDATE`d (`final_*` null), and Jan 19
keeps its scheduled slot while the override produces the final instance
on Jan 20:

```json
{
  "uid": "weekly-1",
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 3,
  "occurrences": [
    {
      "recurrence_id": "2026-01-05T09:00:00Z",
      "generated_by": [{"kind": "RRULE", "rrule": "FREQ=WEEKLY;COUNT=3"}],
      "excluded_by": [],
      "override": null,
      "scheduled_start": "2026-01-05T09:00:00Z",
      "scheduled_end": "2026-01-05T10:00:00Z",
      "final_start": "2026-01-05T09:00:00Z",
      "final_end": "2026-01-05T10:00:00Z",
      "final_status": "CONFIRMED"
    },
    {
      "recurrence_id": "2026-01-12T09:00:00Z",
      "generated_by": [{"kind": "RRULE", "rrule": "FREQ=WEEKLY;COUNT=3"}],
      "excluded_by": ["2026-01-12T09:00:00Z"],
      "override": null,
      "scheduled_start": "2026-01-12T09:00:00Z",
      "scheduled_end": "2026-01-12T10:00:00Z",
      "final_start": null,
      "final_end": null,
      "final_status": null
    },
    {
      "recurrence_id": "2026-01-19T09:00:00Z",
      "generated_by": [{"kind": "RRULE", "rrule": "FREQ=WEEKLY;COUNT=3"}],
      "excluded_by": [],
      "override": {
        "action": "RESCHEDULED",
        "recurrence_id": "2026-01-19T09:00:00Z",
        "start": "2026-01-20T11:00:00Z",
        "end": "2026-01-20T12:00:00Z",
        "status": "CONFIRMED"
      },
      "scheduled_start": "2026-01-19T09:00:00Z",
      "scheduled_end": "2026-01-19T10:00:00Z",
      "final_start": "2026-01-20T11:00:00Z",
      "final_end": "2026-01-20T12:00:00Z",
      "final_status": "CONFIRMED"
    }
  ]
}
```

### `POST /agenda`

Renders the expanded instances as an agenda ordered by **local date**, using
an IANA display timezone. It reuses the exact recurrence engine behind
`/expand` (same overrides, exclusions, window overlap and `422`s) and then
projects each instance onto local days. Request body:

| Field          | Type   | Description                                                    |
| -------------- | ------ | -------------------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)                         |
| `window_start` | string | Start of the UTC query window (ISO 8601, naive = UTC)          |
| `window_end`   | string | End of the UTC query window (ISO 8601, naive = UTC)            |
| `timezone`     | string | IANA timezone for local dates and timestamps, e.g. `Asia/Tokyo` |

Agenda semantics:

- A **timed event** is converted to the display timezone. When it spans more
  than one local date it is **split at every local midnight** into one
  fragment per date; each fragment carries `uid`, `summary`, the local
  `date`, local `start`/`end` (with the zone offset), and the booleans
  `continues_before` (the instance began on an earlier date) and
  `continues_after` (it continues onto a later date). Midnight boundaries are
  built day by day with `zoneinfo`, so fragments stay wall-clock correct
  across DST spring-forward/fall-back days.
- A **`DATE` all-day event** (including multi-day `DTSTART`/`DTEND` ranges and
  recurring `DATE` series) is returned once per **authored calendar date**
  with `all_day: true`. DATE values carry no timezone, so the dates are the
  exact dates written in the ICS for *every* display timezone — they are
  **never converted through a UTC midnight** (a 00:00 UTC DATE rendered in
  Tokyo would otherwise jump to the previous day). Each day's local
  `start`/`end` are that date's local midnights; `continues_before`/
  `continues_after` mark interior days of a multi-day range.
- Fragment `start`/`end` are the true instance bounds clipped to the local
  day only — they are **not** clipped to the UTC query window, so an event
  overlapping the window from before still shows its real local start.
- Entries are sorted by local date ascending; within a date **all-day
  entries come first**, then timed fragments by local `start`, then `uid`.
  `STATUS:CANCELLED` instances (master or override) are omitted.
- An unknown display `timezone` returns `422`, as do all the calendar and
  window failures described for `/expand`.

### Example

```bash
curl -s http://localhost:8000/agenda \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:night-1\r\nDTSTART:20260105T140000Z\r\nDTEND:20260106T020000Z\r\nSUMMARY:Night shift\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:trip\r\nDTSTART;VALUE=DATE:20260106\r\nDTEND;VALUE=DATE:20260108\r\nSUMMARY:Conference\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z",
    "timezone": "Asia/Tokyo"
  }'
```

The timed event runs 23:00–11:00 JST across the Jan 5/6 midnight and is
split into two fragments with continuation flags; the two-day all-day
conference stays on its authored Jan 6–7 dates even though Tokyo is UTC+9,
and sorts ahead of the timed fragment on Jan 6:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "timezone": "Asia/Tokyo",
  "count": 4,
  "entries": [
    {
      "date": "2026-01-05",
      "uid": "night-1",
      "summary": "Night shift",
      "start": "2026-01-05T23:00:00+09:00",
      "end": "2026-01-06T00:00:00+09:00",
      "all_day": false,
      "continues_before": false,
      "continues_after": true
    },
    {
      "date": "2026-01-06",
      "uid": "trip",
      "summary": "Conference",
      "start": "2026-01-06T00:00:00+09:00",
      "end": "2026-01-07T00:00:00+09:00",
      "all_day": true,
      "continues_before": false,
      "continues_after": true
    },
    {
      "date": "2026-01-06",
      "uid": "night-1",
      "summary": "Night shift",
      "start": "2026-01-06T00:00:00+09:00",
      "end": "2026-01-06T11:00:00+09:00",
      "all_day": false,
      "continues_before": true,
      "continues_after": false
    },
    {
      "date": "2026-01-07",
      "uid": "trip",
      "summary": "Conference",
      "start": "2026-01-07T00:00:00+09:00",
      "end": "2026-01-08T00:00:00+09:00",
      "all_day": true,
      "continues_before": true,
      "continues_after": false
    }
  ]
}
```

### `POST /availability`

Finds free slots that lie inside the working hours **and** avoid busy
instances in *every* supplied calendar. Request body:

| Field             | Type                | Description                                                       |
| ----------------- | ------------------- | ----------------------------------------------------------------- |
| `calendars`       | object              | Map of name → iCalendar text; may be empty or omitted             |
| `window_start`    | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `window_end`      | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `timezone`        | string              | IANA timezone in which `working_hours` are interpreted            |
| `working_hours`   | object              | Keys `monday`…`sunday`; each a list of `{start, end}` local times |
| `minimum_minutes` | integer             | Only free slots at least this long are returned (must be > 0)     |

Working periods are generated for each local date in the window and then
converted to UTC, so a fixed local time shifts correctly across DST
transitions. A missing weekday (or an empty list) means no working time
that day; multiple periods per day are supported (e.g. a lunch break).
Periods must not be inverted (`end <= start`) or cross midnight.

Busy time is every expanded instance that is not `STATUS:CANCELLED` and
has a positive duration; instances are clipped to the working periods and
merged as half-open `[start, end)` intervals, with touching intervals
merged (back-to-back meetings leave no free instant between them). The
complement inside the working periods is returned, sorted by `start`,
keeping only slots of at least `minimum_minutes` (inclusive at the
boundary). Each slot carries UTC `start`, `end`, and integer `minutes`.

### Example

```bash
curl -s http://localhost:8000/availability \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": {
      "alice": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART;TZID=Europe/Berlin:20260309T100000\r\nDTEND;TZID=Europe/Berlin:20260309T110000\r\nSUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "bob": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\nDTSTART:20260309T140000Z\r\nDTEND:20260309T153000Z\r\nSUMMARY:Interview\r\nEND:VEVENT\r\nEND:VCALENDAR"
    },
    "window_start": "2026-03-09T00:00:00Z",
    "window_end": "2026-03-10T00:00:00Z",
    "timezone": "Europe/Berlin",
    "working_hours": {
      "monday": [
        {"start": "09:00", "end": "12:00"},
        {"start": "13:00", "end": "17:00"}
      ]
    },
    "minimum_minutes": 30
  }'
```

Berlin is UTC+1 on this date, so the working periods are `08:00–11:00Z`
and `12:00–16:00Z`; Alice blocks `09:00–10:00Z` and Bob `14:00–15:30Z`.
The lunch break is not free time, and the final 30-minute tail is kept
because the minimum length is inclusive:

```json
{
  "window_start": "2026-03-09T00:00:00Z",
  "window_end": "2026-03-10T00:00:00Z",
  "timezone": "Europe/Berlin",
  "count": 4,
  "slots": [
    {"start": "2026-03-09T08:00:00Z", "end": "2026-03-09T09:00:00Z", "minutes": 60},
    {"start": "2026-03-09T10:00:00Z", "end": "2026-03-09T11:00:00Z", "minutes": 60},
    {"start": "2026-03-09T12:00:00Z", "end": "2026-03-09T14:00:00Z", "minutes": 120},
    {"start": "2026-03-09T15:30:00Z", "end": "2026-03-09T16:00:00Z", "minutes": 30}
  ]
}
```

### `POST /conflicts`

Finds the UTC segments where at least `minimum_concurrency` instances from
the named calendars overlap. Each calendar is expanded with the same
recurrence engine used by `/expand`; only instances that are **not**
`STATUS:CANCELLED` and have a **positive duration** take part, and every
participating interval is clipped to the half-open query window
`[window_start, window_end)`. Request body:

| Field                 | Type   | Description                                                    |
| --------------------- | ------ | -------------------------------------------------------------- |
| `calendars`           | object | Map of name → iCalendar text                                   |
| `window_start`        | string | ISO 8601 timestamp, converted to UTC (naive = UTC)             |
| `window_end`          | string | ISO 8601 timestamp, converted to UTC (naive = UTC)             |
| `minimum_concurrency` | int    | Report segments with at least this many active instances (≥ 2) |

A sweep line visits every interval start and end instant in chronological
order. Intervals are half-open `[start, end)`, so at one and the same
instant **ends are processed before starts**: a meeting ending at 11:00 and
one starting at 11:00 are never concurrent. Each non-empty segment whose
active count reaches the threshold is returned with its UTC `start`/`end`,
its `count`, and the participating instances. Participants are sorted by
`calendar`, then `UID`, then `RECURRENCE-ID` (a `null` `RECURRENCE-ID`
sorts first). Two adjacent segments are merged only when their participant
sets are exactly identical; the segments are sorted by `start`. Each
participant is serialized like an `/expand` instance plus the `calendar`
name; its `start`/`end` are the instance times clipped to the window.

### Example

```bash
curl -s http://localhost:8000/conflicts \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": {
      "alice": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART:20260309T090000Z\r\nDTEND:20260309T110000Z\r\nSUMMARY:Deep work\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "bob": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\nDTSTART:20260309T100000Z\r\nDTEND:20260309T120000Z\r\nSUMMARY:Interview\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "carol": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:c1\r\nDTSTART:20260309T103000Z\r\nDTEND:20260309T113000Z\r\nSUMMARY:Review\r\nEND:VEVENT\r\nEND:VCALENDAR"
    },
    "window_start": "2026-03-09T00:00:00Z",
    "window_end": "2026-03-10T00:00:00Z",
    "minimum_concurrency": 2
  }'
```

Alice is busy 09:00–11:00, Bob 10:00–12:00 and Carol 10:30–11:30. The
three-way overlap 10:30–11:00 is bracketed by two two-instance segments;
all three are reported and stay separate because the participants differ:

```json
{
  "window_start": "2026-03-09T00:00:00Z",
  "window_end": "2026-03-10T00:00:00Z",
  "minimum_concurrency": 2,
  "count": 3,
  "segments": [
    {
      "start": "2026-03-09T10:00:00Z",
      "end": "2026-03-09T10:30:00Z",
      "count": 2,
      "participants": [
        {
          "calendar": "alice",
          "uid": "a1",
          "recurrence_id": null,
          "summary": "Deep work",
          "start": "2026-03-09T09:00:00Z",
          "end": "2026-03-09T11:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        }
      ]
    },
    {
      "start": "2026-03-09T10:30:00Z",
      "end": "2026-03-09T11:00:00Z",
      "count": 3,
      "participants": [
        {
          "calendar": "alice",
          "uid": "a1",
          "recurrence_id": null,
          "summary": "Deep work",
          "start": "2026-03-09T09:00:00Z",
          "end": "2026-03-09T11:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "carol",
          "uid": "c1",
          "recurrence_id": null,
          "summary": "Review",
          "start": "2026-03-09T10:30:00Z",
          "end": "2026-03-09T11:30:00Z",
          "status": "CONFIRMED"
        }
      ]
    },
    {
      "start": "2026-03-09T11:00:00Z",
      "end": "2026-03-09T11:30:00Z",
      "count": 2,
      "participants": [
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "carol",
          "uid": "c1",
          "recurrence_id": null,
          "summary": "Review",
          "start": "2026-03-09T10:30:00Z",
          "end": "2026-03-09T11:30:00Z",
          "status": "CONFIRMED"
        }
      ]
    }
  ]
}
```

### `POST /calendar-diff`

Compares an **old** and a **new** version of a calendar over one shared UTC
window. Both ICS texts are expanded with the same recurrence engine used by
`/expand`; instance identity is fixed to `UID + RECURRENCE-ID` for recurring
instances and to `UID` alone for non-recurring events (`recurrence_id` is
`null`). Only instances that overlap the common window take part in the
comparison.

| Field          | Type   | Description                                         |
| -------------- | ------ | --------------------------------------------------- |
| `old_ics`      | string | Old version of the iCalendar text                   |
| `new_ics`      | string | New version of the iCalendar text                   |
| `window_start` | string | Shared ISO 8601 window start, converted to UTC      |
| `window_end`   | string | Shared ISO 8601 window end, converted to UTC        |

The response lists:

- `added` — identities present only in the new calendar,
- `removed` — identities present only in the old calendar,
- `modified` — identities present on both sides whose `start`, `end`,
  `summary` or `status` changed, each with a `before` and an `after` object
  in the same shape as an `/expand` instance.

Completely identical instances are not returned. All three lists are sorted
by identity (`UID`, then `RECURRENCE-ID`; a `null` `RECURRENCE-ID` sorts
first). Cancelling an occurrence with a `STATUS:CANCELLED` override makes it
disappear from the expansion, so it is reported as `removed`; likewise an
instance rescheduled across the window boundary appears as
`removed`/`added`, while one that still overlaps the window on both sides is
`modified` (its identity never changes).

### Example

```bash
curl -s http://localhost:8000/calendar-diff \
  -H 'Content-Type: application/json' \
  -d '{
    "old_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:oneoff\r\nDTSTART:20260304T140000Z\r\nDTEND:20260304T150000Z\r\nSUMMARY:Old review\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "new_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260309T090000Z\r\nDTSTART:20260309T100000Z\r\nDTEND:20260309T101500Z\r\nSUMMARY:Standup (15 min later)\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:workshop\r\nDTSTART:20260320T130000Z\r\nDTEND:20260320T150000Z\r\nSUMMARY:Workshop\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-03-01T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

The new calendar adds `workshop`, drops `oneoff`, and moves the March 9
standup instance (its `RECURRENCE-ID` stays `2026-03-09T09:00:00Z`); the
unchanged March 2/16/23 standups are not reported:

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "added": [
    {
      "uid": "workshop",
      "recurrence_id": null,
      "summary": "Workshop",
      "start": "2026-03-20T13:00:00Z",
      "end": "2026-03-20T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "removed": [
    {
      "uid": "oneoff",
      "recurrence_id": null,
      "summary": "Old review",
      "start": "2026-03-04T14:00:00Z",
      "end": "2026-03-04T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "modified": [
    {
      "before": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup",
        "start": "2026-03-09T09:00:00Z",
        "end": "2026-03-09T09:15:00Z",
        "status": "CONFIRMED"
      },
      "after": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup (15 min later)",
        "start": "2026-03-09T10:00:00Z",
        "end": "2026-03-09T10:15:00Z",
        "status": "CONFIRMED"
      }
    }
  ]
}
```

### `POST /alarms`

Expands the `VALARM` reminders of every `VEVENT` instance that overlaps the
query window, returning only reminders whose **trigger instant** falls
inside `[window_start, window_end)`. Request body is the same shape as
`/expand`:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

Alarm semantics:

- Only `ACTION:DISPLAY` alarms are supported (`AUDIO`, `EMAIL`, … → `422`);
  a `DESCRIPTION` is required.
- `TRIGGER` may be **absolute** (`VALUE=DATE-TIME`, optionally with a
  `TZID`; a floating value is assumed UTC) or a **duration** relative to
  the instance start (`TRIGGER:-PT15M`) or end
  (`TRIGGER;RELATED=END:PT10M`). Both positive and negative durations are
  accepted; `RELATED=START` is the default.
- `REPEAT` plus `DURATION` (the pair must appear together) fans one alarm
  out into `REPEAT + 1` reminders, spaced by `DURATION` (e.g.
  `REPEAT:2`/`DURATION:PT5M` fires at the initial trigger and twice more,
  five minutes apart). `REPEAT` must be non-negative and `DURATION`
  positive; a single alarm may not fan out beyond 1000 reminders.
- A **master** event's alarms apply to *every* expanded instance of the
  series; a `RECURRENCE-ID` override carries **its own** alarms and the
  master's alarms do not apply to it (an override without `VALARM`s alarms
  nobody). A cancelled occurrence produces no reminders.
- The window is half-open: a trigger exactly at `window_start` is included,
  one exactly at `window_end` is excluded.

Each reminder carries `uid`, `recurrence_id` (UTC, `null` for
non-recurring events), `summary` (of the instance it belongs to), `trigger`
(UTC, `Z`), and the alarm `description`. Reminders are stably sorted by
trigger time, then `UID`, then `RECURRENCE-ID` (a `null` `RECURRENCE-ID`
sorts first).

### Example

```bash
curl -s http://localhost:8000/alarms \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:Weekly sync\r\nBEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\nDESCRIPTION:Join the call\r\nREPEAT:1\r\nDURATION:PT10M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

The one master alarm applies to both Mondays and repeats once ten minutes
after the initial trigger:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 4,
  "alarms": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:55:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:55:00Z",
      "description": "Join the call"
    }
  ]
}
```

### `POST /attendance`

Reports, for every `VEVENT` instance overlapping the query window, the
attendee list in effect for that instance together with a `PARTSTAT`
summary. Request body is the same shape as `/expand`:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

Attendance semantics:

- Every instance of a recurring series **inherits the master event's**
  `ATTENDEE` list. A `RECURRENCE-ID` override that declares **any**
  `ATTENDEE` **replaces the whole list** for that instance (the master's
  attendees no longer apply); an override with no `ATTENDEE` keeps
  inheriting, just like its `SUMMARY` and duration. Cancelled and
  `EXDATE`d occurrences are not returned.
- Each attendee carries a normalized `address`, `partstat`, and `role`.
  A `mailto:` address has its scheme prefix removed and is lowercased
  (`mailto:Alice@Example.com` → `alice@example.com`); every other URI
  (e.g. `https:`, `tel:`, `urn:`) is kept verbatim. Missing `PARTSTAT`
  defaults to `NEEDS-ACTION`; missing `ROLE` defaults to
  `REQ-PARTICIPANT`.
- `counts` maps each `PARTSTAT` present on the instance to its number of
  attendees (an empty attendee list yields `{}`).
- Attendees are **stably sorted by normalized address**. The same address
  may repeat within one effective list only when its parameters agree
  (it then collapses to one entry); a repeat with conflicting `PARTSTAT`
  or `ROLE` returns `422`. Every `VEVENT`'s own list is validated
  eagerly, including events outside the window.

### Example

```bash
curl -s http://localhost:8000/attendance \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:att-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Planning\r\nATTENDEE;PARTSTAT=ACCEPTED;CN=Alice:mailto:Alice@Example.com\r\nATTENDEE;PARTSTAT=DECLINED:mailto:bob@example.com\r\nATTENDEE:https://cal.example.com/users/carol\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:att-1\r\nRECURRENCE-ID:20260112T090000Z\r\nDTSTART:20260112T090000Z\r\nDTEND:20260112T093000Z\r\nSUMMARY:Planning (small group)\r\nATTENDEE;PARTSTAT=TENTATIVE:mailto:dave@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

The January 5 and 19 instances inherit the master's three attendees
(`Alice@Example.com` is normalized to `alice@example.com`; the non-mailto
URI is preserved). The January 12 override declares its own attendee and
therefore replaces the entire list; `counts` follows each effective list:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 3,
  "instances": [
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "start": "2026-01-05T09:00:00Z",
      "end": "2026-01-05T10:00:00Z",
      "counts": {
        "ACCEPTED": 1,
        "DECLINED": 1,
        "NEEDS-ACTION": 1
      },
      "attendees": [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
        {"address": "bob@example.com", "partstat": "DECLINED", "role": "REQ-PARTICIPANT"},
        {"address": "https://cal.example.com/users/carol", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"}
      ]
    },
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "start": "2026-01-12T09:00:00Z",
      "end": "2026-01-12T09:30:00Z",
      "counts": {
        "TENTATIVE": 1
      },
      "attendees": [
        {"address": "dave@example.com", "partstat": "TENTATIVE", "role": "REQ-PARTICIPANT"}
      ]
    },
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-19T09:00:00Z",
      "start": "2026-01-19T09:00:00Z",
      "end": "2026-01-19T10:00:00Z",
      "counts": {
        "ACCEPTED": 1,
        "DECLINED": 1,
        "NEEDS-ACTION": 1
      },
      "attendees": [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
        {"address": "bob@example.com", "partstat": "DECLINED", "role": "REQ-PARTICIPANT"},
        {"address": "https://cal.example.com/users/carol", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"}
      ]
    }
  ]
}
```

### `POST /validate`

Structurally validates iCalendar text and reports every problem found as a
diagnostic — **without any query window**. Unlike the other endpoints,
validation never fails a request for bad calendar content: both valid and
invalid calendars return `200`, and even text that cannot be parsed yields a
structured issue rather than an error status.

| Field | Type   | Description                            |
| ----- | ------ | -------------------------------------- |
| `ics` | string | iCalendar text (zero or more `VEVENT`s) |

The response is:

| Field         | Type    | Description                                                |
| ------------- | ------- | ---------------------------------------------------------- |
| `valid`       | boolean | `true` only when there are no issues                       |
| `error_count` | integer | Number of issues (`issues.length`)                         |
| `issues`      | array   | Diagnostics sorted by `event_index` (nulls first), `code`  |

Each issue carries:

| Field         | Type           | Description                                                       |
| ------------- | -------------- | ----------------------------------------------------------------- |
| `code`        | string         | Stable machine-readable code (see below)                          |
| `message`     | string         | Human-readable description                                        |
| `event_index` | integer \| null | 0-based index of the offending `VEVENT` in source order; `null` when no event can be located |
| `uid`         | string \| null  | The event's `UID`; `null` when the event has no usable UID        |

The checks run per `VEVENT`, in the order the events appear in the source
text; a single event can accumulate several issues. Cross-event checks run
afterwards. The stable codes are:

| Code                     | Meaning                                                                          |
| ------------------------ | -------------------------------------------------------------------------------- |
| `parse_error`            | The text could not be parsed as iCalendar at all (`event_index`/`uid` are null); no further checks are possible |
| `missing_uid`            | A `VEVENT` lacks the required `UID` property                                     |
| `missing_dtstart`        | A `VEVENT` lacks `DTSTART`, or its `DTSTART` is not a valid DATE/DATE-TIME value |
| `dtend_and_duration`     | A `VEVENT` declares both `DTEND` and `DURATION`, which RFC 5545 forbids          |
| `end_before_start`       | The declared end (`DTEND`, or `DTSTART + DURATION`) is equal to or earlier than the start, after normalization to UTC |
| `duplicate_master_event` | More than one `VEVENT` with the same `UID` has no `RECURRENCE-ID`; every such master is flagged |
| `orphan_override`        | A `VEVENT` with `RECURRENCE-ID` has no master event (a same-`UID` `VEVENT` without `RECURRENCE-ID`) |

Notes on semantics:

- Floating (timezone-less) values are assumed UTC, as on the other
  endpoints, so cross-timezone comparisons are correct (including DST);
  `DATE`-valued starts count as midnight UTC and a one-day all-day event
  has a next-day `DTEND`.
- An event with only `DTSTART` (zero-duration) is not flagged; only an
  *explicitly declared* end is order-checked. When `DTSTART` is unusable
  the ordering check is skipped (the `missing_dtstart` issue stands
  instead).
- Events without a usable `UID` take part only in the per-event checks;
  they can neither count as masters nor bind overrides.
- When both `DTEND` and `DURATION` are present, `DTEND` is still used for
  the ordering check, so an inverted event may report both
  `dtend_and_duration` and `end_before_start`.
- A missing `ics` request field is a normal schema failure (`422`); every
  well-formed request, even with garbage `ics`, gets `200`.

### Example

```bash
curl -s http://localhost:8000/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:series\r\nDTSTART:20260105T090000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:series\r\nDTSTART:20260106T090000Z\r\nDTEND:20260106T100000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20260120T090000Z\r\nDTSTART:20260120T110000Z\r\nDTEND:20260120T100000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART:20260121T090000Z\r\nDTEND:20260121T080000Z\r\nEND:VEVENT\r\nEND:VCALENDAR"
  }'
```

The second event is a second master for `UID:series` (both masters are
flagged); the override's end precedes its start; the last event is missing
its `UID` and also inverted. Issues come back ordered by event index and
then code:

```json
{
  "valid": false,
  "error_count": 5,
  "issues": [
    {
      "code": "duplicate_master_event",
      "message": "UID 'series' has 2 VEVENTs without RECURRENCE-ID; a recurring series may have only one master event",
      "event_index": 0,
      "uid": "series"
    },
    {
      "code": "duplicate_master_event",
      "message": "UID 'series' has 2 VEVENTs without RECURRENCE-ID; a recurring series may have only one master event",
      "event_index": 1,
      "uid": "series"
    },
    {
      "code": "end_before_start",
      "message": "VEVENT end 2026-01-20T10:00:00Z is not later than its start 2026-01-20T11:00:00Z",
      "event_index": 2,
      "uid": "series"
    },
    {
      "code": "end_before_start",
      "message": "VEVENT end 2026-01-21T08:00:00Z is not later than its start 2026-01-21T09:00:00Z",
      "event_index": 3,
      "uid": null
    },
    {
      "code": "missing_uid",
      "message": "VEVENT is missing the required UID property",
      "event_index": 3,
      "uid": null
    }
  ]
}
```

### `POST /compress-schedule`

The inverse of `/expand`: given a list of equal-length, non-overlapping UTC
instances with unique starts, it returns the **smallest** complete iCalendar
text that represents them. Request body:

| Field       | Type   | Description                                                          |
| ----------- | ------ | -------------------------------------------------------------------- |
| `uid`       | string | Non-empty `UID` for the generated `VEVENT`                           |
| `instances` | array  | Non-empty list of `{start, end}`; both must be UTC (`Z`) and `end > start` |

Compression rules:

- The earliest start becomes `DTSTART`; every event keeps the single common
  duration (written as `DTEND`).
- Eight rule-based candidates are enumerated: `FREQ=DAILY` and
  `FREQ=WEEKLY`, each with `INTERVAL` 1, 2, 3, 4, and `UNTIL` pinned to the
  latest input start. For a candidate's grid, grid points missing from the
  input become `EXDATE` and input starts the grid does not generate become
  `RDATE` (multiple values share one comma-separated property).
- A ninth **fallback** candidate uses only `DTSTART` + one `RDATE` per
  remaining instance (no `RRULE`); this always wins for tiny or irregular
  sets where any rule would be longer.
- Every candidate is fed back through the exact `/expand` engine and is only
  eligible when it reproduces the *identical* set of starts and ends.
- Candidates are ranked by the **UTF-8 byte length of the complete ICS**
  (fewest wins). Ties are broken in order by: fewer exceptions
  (`EXDATE` + `RDATE` values), an `RRULE` plan over the fallback, `DAILY`
  over `WEEKLY`, then the smaller `INTERVAL`.

The response is:

| Field             | Type    | Description                                                          |
| ----------------- | ------- | -------------------------------------------------------------------- |
| `strategy`        | string  | `RRULE:FREQ=<DAILY\|WEEKLY>;INTERVAL=<1..4>`, or `RDATE` (fallback)  |
| `exception_count` | integer | Number of `EXDATE` plus `RDATE` values in the returned ICS           |
| `ics`             | string  | Complete `VCALENDAR` text that `/expand` turns back into the inputs  |

### Example

```bash
curl -s http://localhost:8000/compress-schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "uid": "team-sync",
    "instances": [
      {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
      {"start": "2026-01-14T09:00:00Z", "end": "2026-01-14T10:00:00Z"},
      {"start": "2026-01-19T09:00:00Z", "end": "2026-01-19T10:00:00Z"},
      {"start": "2026-01-26T09:00:00Z", "end": "2026-01-26T10:00:00Z"},
      {"start": "2026-02-02T09:00:00Z", "end": "2026-02-02T10:00:00Z"},
      {"start": "2026-02-09T09:00:00Z", "end": "2026-02-09T10:00:00Z"},
      {"start": "2026-02-16T09:00:00Z", "end": "2026-02-16T10:00:00Z"},
      {"start": "2026-02-23T09:00:00Z", "end": "2026-02-23T10:00:00Z"}
    ]
  }'
```

These are eight Monday 09:00 meetings (Jan 5–Feb 23) with the Jan 12 meeting
skipped and a one-off Wednesday Jan 14 session added. A `WEEKLY;INTERVAL=1`
rule covers the Mondays, the skipped Monday is an `EXDATE`, and the Wednesday
is an `RDATE`; the result is shorter than listing all eight dates:

```json
{
  "strategy": "RRULE:FREQ=WEEKLY;INTERVAL=1",
  "exception_count": 2,
  "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//compress-schedule//EN\r\nBEGIN:VEVENT\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nUID:team-sync\r\nRRULE:FREQ=WEEKLY;UNTIL=20260223T090000Z;INTERVAL=1\r\nRDATE:20260114T090000Z\r\nEXDATE:20260112T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
}
```

Posting that `ics` to `/expand` (any window covering Jan 5–Feb 23, e.g.
`2025-12-01`–`2026-04-01`) returns exactly the eight submitted starts and
ends, including the Wednesday `RDATE` and without the Jan 12 `EXDATE`. With
only one or two instances the response instead has `"strategy": "RDATE"` and
carries them all after `DTSTART` — which is the byte-minimal representation.

### `POST /freebusy-export`

Exports each supplied calendar as a **VFREEBUSY** text: the merged busy
periods of that calendar inside one shared UTC query window. Request body:

| Field          | Type   | Description                                                        |
| -------------- | ------ | ------------------------------------------------------------------ |
| `calendars`    | array  | Objects with `calendar_id`, `ics`, `timezone` (see below); may be empty |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC)                 |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC)                 |

Each `calendars` entry carries:

| Field         | Type   | Description                                                       |
| ------------- | ------ | ----------------------------------------------------------------- |
| `calendar_id` | string | Non-empty identifier echoed back with the export; must be unique  |
| `ics`         | string | iCalendar text (one or more `VEVENT`s)                            |
| `timezone`    | string | IANA timezone resolving `DATE` all-day events into local full days |

Every calendar is expanded with the same recurrence engine as `/expand`
(same overrides, exclusions, window overlap and `422`s). An instance blocks
time only when it is **not** `STATUS:CANCELLED`, has a positive duration,
and is not `TRANSP:TRANSPARENT` — recurring instances inherit the master
event's `TRANSP`, while a `RECURRENCE-ID` override that declares `TRANSP`
replaces the inherited value for its instance. A `DATE` all-day instance
occupies its authored calendar dates as **local full days** in the
calendar's own `timezone` (midnight-to-midnight via `zoneinfo`, so a
spring-forward day occupies 23 UTC hours and a fall-back day 25). The busy
intervals are clipped to the half-open window and merged, with touching
intervals collapsed.

The response echoes the UTC window and lists one entry per calendar,
sorted by `calendar_id`:

| Field         | Type   | Description                                                       |
| ------------- | ------ | ----------------------------------------------------------------- |
| `calendar_id` | string | The calendar's identifier                                         |
| `vfreebusy`   | string | Complete `VCALENDAR` text holding one `VFREEBUSY` component       |

The `VFREEBUSY` component carries `UID` (the `calendar_id`), `DTSTAMP`
(generation time), `DTSTART`/`DTEND` (the UTC window), and one
`FREEBUSY:start/end` line per merged busy period — UTC `Z` basic format,
sorted by start. A calendar with no busy time yields a component without
`FREEBUSY` lines.

### Example

```bash
curl -s http://localhost:8000/freebusy-export \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": [
      {
        "calendar_id": "alice",
        "timezone": "America/New_York",
        "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:sync\r\nDTSTART;TZID=America/New_York:20260302T090000\r\nDTEND;TZID=America/New_York:20260302T100000\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:Weekly sync\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:offsite\r\nDTSTART;VALUE=DATE:20260308\r\nDTEND;VALUE=DATE:20260309\r\nSUMMARY:Offsite\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:hold\r\nDTSTART:20260310T140000Z\r\nDTEND:20260310T150000Z\r\nTRANSP:TRANSPARENT\r\nSUMMARY:Tentative hold\r\nEND:VEVENT\r\nEND:VCALENDAR"
      },
      {
        "calendar_id": "bob",
        "timezone": "Asia/Tokyo",
        "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:review\r\nDTSTART:20260309T130000Z\r\nDTEND:20260309T140000Z\r\nSUMMARY:Review\r\nEND:VEVENT\r\nEND:VCALENDAR"
      }
    ],
    "window_start": "2026-03-01T00:00:00Z",
    "window_end": "2026-03-16T00:00:00Z"
  }'
```

Alice's weekly 09:00 sync shows the DST shift (14:00Z before, 13:00Z after
the March 8 spring forward), her all-day offsite occupies the 23-hour
spring-forward local day (05:00Z–04:00Z), and the `TRANSP:TRANSPARENT`
hold blocks nothing; Bob contributes one UTC meeting:

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-03-16T00:00:00Z",
  "count": 2,
  "calendars": [
    {
      "calendar_id": "alice",
      "vfreebusy": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//freebusy-export//EN\r\nBEGIN:VFREEBUSY\r\nUID:alice\r\nDTSTAMP:20260918T000000Z\r\nDTSTART:20260301T000000Z\r\nDTEND:20260316T000000Z\r\nFREEBUSY:20260302T140000Z/20260302T150000Z\r\nFREEBUSY:20260308T050000Z/20260309T040000Z\r\nFREEBUSY:20260309T130000Z/20260309T140000Z\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    },
    {
      "calendar_id": "bob",
      "vfreebusy": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//freebusy-export//EN\r\nBEGIN:VFREEBUSY\r\nUID:bob\r\nDTSTAMP:20260918T000000Z\r\nDTSTART:20260301T000000Z\r\nDTEND:20260316T000000Z\r\nFREEBUSY:20260309T130000Z/20260309T140000Z\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    }
  ]
}
```

### `POST /fair-rotation`

Schedules a **rotating meeting series** across timezones: one meeting per
organizer-local date, chosen jointly so the burden of awkward hours is
shared fairly. Request body:

| Field                 | Type    | Description                                                  |
| --------------------- | ------- | ------------------------------------------------------------ |
| `organizer_timezone`  | string  | IANA timezone in which `dates` and their `candidates` are local, e.g. `America/New_York` |
| `duration_minutes`    | integer | Meeting length in minutes (must be > 0)                      |
| `dates`               | array   | Non-empty list of `{date, candidates}` (see below)           |
| `participants`        | array   | Objects with `name`, `timezone`, `busy_ics`, `preferred_periods` (see below); may be empty or omitted |

Each `dates` entry carries an organizer-local `date` (`YYYY-MM-DD`) and a
non-empty `candidates` list of local start times (`HH:MM`, optionally with
seconds; no timezone offset). Each `participants` entry carries:

| Field               | Type   | Description                                                   |
| ------------------- | ------ | ------------------------------------------------------------- |
| `name`              | string | Non-empty identifier, unique within the request               |
| `timezone`          | string | IANA timezone of the participant, e.g. `Europe/Berlin`        |
| `busy_ics`          | string | iCalendar text with the participant's busy events; empty or omitted means no busy time |
| `preferred_periods` | array  | Preferred local `{start, end}` time-of-day periods (naive, `end > start`, no overnight); empty or omitted means nothing is preferred |

Every candidate is materialized on its organizer-local date with
`zoneinfo` (DST-correct in every involved timezone) and converted to UTC.
A candidate is **feasible** when its meeting interval overlaps no
participant's busy time; busy time is expanded from each `busy_ics` with
the same recurrence engine as `/expand` (`STATUS:CANCELLED` and
zero-duration instances never block). Each meeting costs a participant the
minutes of its interval falling outside the participant's preferred local
periods (materialized per local date in the participant's own timezone),
and the costs accumulate over all dates. The plan minimizes, in order:

1. the largest per-participant cumulative deviation,
2. the total deviation over all participants,
3. candidate input order (earlier dates, then earlier candidates win).

The response echoes the objectives and lists the chosen meeting per date
(organizer-local `date`, `candidate_index` into the date's `candidates`,
UTC `start`/`end`, organizer-local `local_start`/`local_end`, and the
per-participant deviation of that meeting) plus each participant's
`cumulative_deviation_minutes`:

| Field                              | Type   | Description                                 |
| ---------------------------------- | ------ | ------------------------------------------- |
| `organizer_timezone`               | string | Echo of the request                         |
| `duration_minutes`                 | int    | Echo of the request                         |
| `meeting_count`                    | int    | Number of meetings (one per date)           |
| `max_cumulative_deviation_minutes` | number | Primary objective value                     |
| `total_deviation_minutes`          | number | Secondary objective value                   |
| `meetings`                         | array  | One entry per date, in request order        |
| `participants`                     | array  | One entry per participant, in request order |

### Example

```bash
curl -s http://localhost:8000/fair-rotation \
  -H 'Content-Type: application/json' \
  -d '{
    "organizer_timezone": "America/New_York",
    "duration_minutes": 60,
    "dates": [
      {"date": "2026-03-09", "candidates": ["09:00", "12:00", "16:00"]},
      {"date": "2026-03-10", "candidates": ["09:00", "12:00", "16:00"]}
    ],
    "participants": [
      {
        "name": "alice",
        "timezone": "Europe/Berlin",
        "busy_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART:20260309T133000Z\r\nDTEND:20260309T143000Z\r\nSUMMARY:Alice busy\r\nEND:VEVENT\r\nEND:VCALENDAR",
        "preferred_periods": [{"start": "09:00", "end": "18:00"}]
      },
      {
        "name": "bob",
        "timezone": "Asia/Tokyo",
        "preferred_periods": [{"start": "09:00", "end": "18:00"}]
      }
    ]
  }'
```

New York is already on EDT (UTC−4) while Berlin is still on CET (UTC+1).
On March 9 the 09:00 candidate (13:00–14:00Z) collides with Alice's busy
event, so 12:00 (17:00–18:00 in Berlin) is chosen; on March 10 the 09:00
candidate is free and preferred for Alice. Every candidate falls outside
Bob's 09:00–18:00 JST preference, so he accumulates 60 minutes per meeting
— the joint plan cannot do better for him, and among the plans that are
equally good for him it minimizes Alice's total:

```json
{
  "organizer_timezone": "America/New_York",
  "duration_minutes": 60,
  "meeting_count": 2,
  "max_cumulative_deviation_minutes": 120,
  "total_deviation_minutes": 120,
  "meetings": [
    {
      "date": "2026-03-09",
      "candidate_index": 1,
      "start": "2026-03-09T16:00:00Z",
      "end": "2026-03-09T17:00:00Z",
      "local_start": "2026-03-09T12:00:00-04:00",
      "local_end": "2026-03-09T13:00:00-04:00",
      "deviations": [
        {"participant": "alice", "deviation_minutes": 0},
        {"participant": "bob", "deviation_minutes": 60}
      ]
    },
    {
      "date": "2026-03-10",
      "candidate_index": 0,
      "start": "2026-03-10T13:00:00Z",
      "end": "2026-03-10T14:00:00Z",
      "local_start": "2026-03-10T09:00:00-04:00",
      "local_end": "2026-03-10T10:00:00-04:00",
      "deviations": [
        {"participant": "alice", "deviation_minutes": 0},
        {"participant": "bob", "deviation_minutes": 60}
      ]
    }
  ],
  "participants": [
    {"name": "alice", "timezone": "Europe/Berlin", "cumulative_deviation_minutes": 0},
    {"name": "bob", "timezone": "Asia/Tokyo", "cumulative_deviation_minutes": 120}
  ]
}
```

### Errors (`422 Unprocessable Entity`)

Returned with a descriptive `detail` message for: unparsable iCalendar
text, invalid `RRULE` (e.g. `FREQ=BOGUS`), unknown `TZID`
(non-IANA name), missing `DTSTART`/`UID`, `RECURRENCE-ID` with
`RANGE=THISANDFUTURE` (unsupported), `window_start >= window_end`, and
expansions exceeding 10 000 occurrences per UID within the window.

`/availability` additionally returns `422` for: an unknown `timezone`
(non-IANA name), a working period with `end <= start` or crossing
midnight, an offset-bearing working time, an unknown weekday key, and a
non-positive `minimum_minutes`.

`/conflicts` additionally returns `422` when `minimum_concurrency` is
missing or less than 2, and reuses the calendar and window validation of
the other endpoints (`window_start >= window_end`, unparsable ICS, invalid
recurrence, unknown `TZID`).

`/calendar-diff` reuses the same calendar and window validation, so it
returns `422` when `window_start >= window_end` or when either `old_ics` or
`new_ics` fails any of the `/expand` checks above.

`/provenance` reuses the calendar and window validation of `/expand`
(unparsable ICS, invalid recurrence, unknown `TZID`,
`window_start >= window_end`, the 10 000-occurrence cap) and additionally
returns `422` when `uid` is missing/empty or no `VEVENT` with that `UID`
exists in the supplied calendar.

`/agenda` additionally returns `422` for an unknown display `timezone`
(non-IANA name) and reuses the calendar and window validation of `/expand`
(unparsable ICS, invalid recurrence, unknown `TZID`,
`window_start >= window_end`).

`/alarms` additionally returns `422` for: an `ACTION` other than
`DISPLAY` (or a missing `ACTION`), a `DISPLAY` alarm without
`DESCRIPTION`, a missing or unsupported `TRIGGER` (only `DURATION` and
`DATE-TIME` values are supported; `RELATED` other than `START`/`END`, or
`RELATED` on an absolute trigger), exactly one of `REPEAT`/`DURATION`
present, a negative `REPEAT`, a non-positive alarm `DURATION`, a fan-out
beyond 1000 reminders, or an unknown `TZID` on an absolute trigger. All
`VALARM`s are validated eagerly, so an unsupported alarm yields `422`
even when its trigger lies outside the window; calendar-level failures
(unparsable ICS, bad window, invalid recurrence) reuse the `/expand`
checks.

`/attendance` additionally returns `422` when one effective attendee
list contains the same address more than once with conflicting
`PARTSTAT` or `ROLE` (addresses are compared after `mailto:` stripping
and lowercasing; explicit `NEEDS-ACTION`/`REQ-PARTICIPANT` agrees with an
omitted parameter). Every `VEVENT`'s own `ATTENDEE` list is validated
eagerly, so a conflict yields `422` even for events outside the window.
Calendar-level failures (unparsable ICS, bad window, invalid recurrence,
unknown `TZID`) reuse the `/expand` checks.

`/validate` is the deliberate exception: it never returns `422` for
calendar content. Unparsable text and malformed events are reported as
`200` responses with one or more issues (a `parse_error` issue has null
`event_index`/`uid`). The only `422` from `/validate` is a malformed
request body (e.g. a missing `ics` field).

`/compress-schedule` returns `422` for: an empty or missing `uid`, an
empty/missing `instances` list, an instance whose `start`/`end` is not
UTC-aware (naive or with a non-UTC offset), an `end` not strictly later
than its `start`, duplicate starts, overlapping intervals (touching
equal-length intervals are allowed), or instances of unequal duration.
The endpoints accepts unsorted input; instances are sorted internally.

`/freebusy-export` returns `422` for: `window_start >= window_end`, a
missing/empty `calendar_id` or `timezone`, a duplicate `calendar_id`, an
unknown `timezone` (non-IANA name), a `TRANSP` value other than
`OPAQUE`/`TRANSPARENT`, and every calendar-level failure of `/expand`
(unparsable ICS, invalid `RRULE`, unknown `TZID`, missing
`DTSTART`/`UID`, the 10 000-occurrence cap).

`/fair-rotation` returns `422` for: an unknown `organizer_timezone` or
participant `timezone` (non-IANA name), a duplicate participant `name`, a
non-positive `duration_minutes`, an empty `dates` list or a date with an
empty `candidates` list, an offset-bearing candidate or preferred-period
time, a preferred period with `end <= start` (overnight periods are not
supported), an unparsable participant `busy_ics` (including every
calendar-level failure of `/expand`), an organizer date on which **no
candidate is feasible** for every participant at once, and a joint search
space — the product of the per-date feasible candidate counts — exceeding
**50 000** combinations.

## Semantics and limitations

- **Window overlap**: an instance is included when
  `start < window_end and end > window_start`; zero-duration instances when
  `window_start <= start < window_end`.
- **Overrides** replace the occurrence identified by `RECURRENCE-ID` even
  when the original falls outside the expansion window (e.g. moved into the
  window from outside). A `STATUS:CANCELLED` override removes the instance.
  An override without `DTEND`/`DURATION` inherits the master's duration.
- **Floating times** (no `Z`, no `TZID`) are interpreted as UTC.
- **All-day events** (`DATE`-valued `DTSTART`) start at local midnight and
  last one day when no `DTEND` is given; `/agenda` renders them on their
  authored calendar dates regardless of the display timezone.
- `RDATE`/`EXDATE` support `DATE` and `DATE-TIME` values (not `PERIOD`);
  `RECURRENCE-ID` with `RANGE=THISANDFUTURE` is not supported.
- A master event with `STATUS:CANCELLED` suppresses its whole series.

### Provenance semantics

- **Same engine as `/expand`**: occurrence generation, the
  duration-padded window frame, floating-time/DST handling, `EXDATE`
  matching and the 10 000-occurrence cap are reused from the expander, so
  the scheduled candidates never drift from `/expand`. Occurrences removed
  by `EXDATE` are re-added to the candidate set from the authored
  `EXDATE` values inside the padded frame purely to report their chain;
  an `EXDATE` that names no generated slot is never a candidate.
- **Generation sources** list every authored property producing the slot,
  in the canonical order `RRULE` (rule text unfolded), `RDATE`, then
  `DTSTART` alone (the seed of an `RDATE`-only or non-recurring event).
  An `RDATE` duplicating an `RRULE` instance lists both; the `DTSTART`
  covered by an `RRULE` is not repeated. An orphan override's
  `RECURRENCE-ID` outside the padded frame is checked against each rule by
  a point membership lookup, so a generated slot moved into the window
  keeps its `RRULE` source even past the frame edge.
- **Overrides** mirror `/expand` keyed by `UID` + `RECURRENCE-ID` (the
  last component for one RID wins). A non-cancelled override is
  `RESCHEDULED` when its start differs from the scheduled slot and
  `MODIFIED` otherwise; a `STATUS:CANCELLED` override is `CANCELLED`. An
  override at an `EXDATEd` slot wins in `/expand` and here too, while the
  matching `EXDATE` stays visible in `excluded_by`.
- **Final values**: equal to the scheduled values for an untouched
  occurrence; all null for an unresurrected `EXDATE`;
  `final_status: "CANCELLED"` with null times for a cancelled master or
  override; the override's own start/end/status after a reschedule/modify.
- **Inclusion and order**: a candidate is returned when its scheduled slot
  *or* its final slot overlaps the half-open window (the same moved-in /
  moved-out judgment as `/expand`, applied per occurrence). Entries sort
  by effective instance time (final start, else scheduled start) and then
  `RECURRENCE-ID`, which is stable for equal times.

### Alarm semantics

- **Instance binding**: a master `VEVENT`'s `VALARM`s apply to every
  instance the recurrence engine expands from it; a `RECURRENCE-ID`
  override replaces the master's alarms for that instance with its own
  (an override with no `VALARM`s has no reminders). Cancelled instances
  and `EXDATE`d occurrences never produce reminders.
- **Triggers**: a duration-valued `TRIGGER` is added to the instance's
  DTSTART (default / `RELATED=START`) or DTEND (`RELATED=END`); signed
  durations point before (negative) or after (positive) the anchor. An
  absolute `TRIGGER;VALUE=DATE-TIME` is independent of the instance time
  and is normalized with its `Z`/`TZID` (floating = UTC).
- **Repeats**: `REPEAT:n` plus `DURATION:d` produces `n + 1` triggers at
  `base, base+d, …, base+n*d`. Reminders are filtered afterwards, so only
  the ones landing in the half-open window are returned.
- **Eager validation**: every `VALARM` in the supplied calendar is
  validated regardless of whether its event or trigger intersects the
  window; an unsupported or malformed alarm fails the whole request with
  `422`. Events without any `VALARM` simply contribute no reminders.

### Attendance semantics

- **Inheritance**: recurring instances use the master event's
  `ATTENDEE` list. A `RECURRENCE-ID` override that declares at least one
  `ATTENDEE` replaces that list wholesale for its instance; an override
  without `ATTENDEE` keeps inheriting the master's list. Inheritance
  never merges two lists, so a non-cancelled event with an empty
  effective list simply reports zero attendees.
- **Cancellations**: a `STATUS:CANCELLED` master or override, and an
  `EXDATE`d occurrence, produce no instance and therefore no attendance.
- **Address normalization**: the `mailto:` scheme prefix (matched
  case-insensitively) is stripped and the remainder lowercased; any
  other URI scheme is preserved exactly as written.
- **Defaults and casing**: missing `PARTSTAT` is `NEEDS-ACTION` and
  missing `ROLE` is `REQ-PARTICIPANT`; supplied values are uppercased.
  `counts` counts attendees per `PARTSTAT` in address order.
- **Duplicates within one effective list**: equal addresses with equal
  effective parameters collapse to a single attendee; conflicting
  `PARTSTAT` or `ROLE` fails the request with `422`. Attendees are
  sorted by normalized address for a stable response.

### Availability semantics

- **Working hours are local**: each weekday's periods are materialized per
  local date with `zoneinfo` and converted to UTC, so local times shift at
  DST transitions (the UTC timeline is built day by day — never by a fixed
  offset). Periods are clipped to the query window.
- **Busy intervals** are the expanded instances of every calendar,
  excluding `STATUS:CANCELLED` and non-positive-duration instances. Each is
  intersected with the working periods before merging, so events outside
  working time never remove free slots.
- **Half-open intervals** `[start, end)` are used throughout: two busy
  intervals that touch at an instant merge, and a busy event ending exactly
  when a free slot begins does not consume it.
- **Common free time** is the complement of the merged union of *all*
  calendars' busy intervals within the merged working periods; gaps between
  a day's working periods (e.g. lunch) and non-working weekdays are never
  returned.
- `minimum_minutes` is applied to slot length and is inclusive at the
  boundary; `minutes` is the integer UTC duration. Slots are sorted by
  `start` and serialized with the `Z` suffix.
- Working periods crossing local midnight are not supported (a range must
  start and end on the same local date).

### Conflict semantics

- **Participants** are the expanded instances of every named calendar,
  excluding `STATUS:CANCELLED` (master or override) and non-positive-duration
  instances. Each participating interval is clipped to the query window
  before the sweep, so an instance starting before `window_start` or ending
  after `window_end` contributes only the in-window part; an instance merely
  touching a boundary contributes nothing.
- **Half-open sweep**: the line visits all start/end instants sorted by time;
  at one instant every end is applied before every start, so back-to-back
  intervals `[a, b)` and `[b, c)` share no instant and never count as
  concurrent. The active set is constant between two consecutive instants.
- **Segments** are the non-empty maximal intervals whose active count is at
  least `minimum_concurrency` (which must be ≥ 2). Each segment carries its
  `count`, equal to the number of `participants`.
- **Merging** joins two qualifying segments only when they are adjacent
  *and* their participating instances are exactly the same; segments with
  the same participants separated by a gap (or by a below-threshold stretch)
  remain distinct. The result is sorted by `start`.
- **Participant order and shape**: participants are sorted by `calendar`,
  then `UID`, then `RECURRENCE-ID` with `null` first; each carries the
  `calendar` name plus the usual `/expand` fields (`uid`, `recurrence_id`,
  `summary`, `start`, `end`, `status`), with `start`/`end` clipped to the
  window.

### Agenda semantics

- **Expansion first**: `/agenda` runs the same `/expand` engine against the
  UTC window, so window overlap, overrides, `EXDATE`, floating-time handling
  and the 10 000-occurrence cap are identical; only non-cancelled instances
  are rendered.
- **Local-day splitting**: timed instances are converted to the display
  timezone with `astimezone` and intersected with each local date
  `[00:00, next 00:00)`. Day boundaries are constructed per date with
  `zoneinfo` (not by adding 24 hours), keeping fragments correct on DST
  transition days. A fragment ending exactly where the instance ends (or
  starting exactly where it starts) gets the matching continuation flag
  cleared; fragments never spill outside the instance.
- **All-day dates are timezone-free**: a `DATE` value denotes a calendar
  date, not a UTC midnight. The agenda recovers the authored dates (the
  expander stores DATE midnights as floating/UTC) and renders each as local
  midnight in the display timezone, so a DATE on Jan 5 appears on Jan 5 in
  Tokyo, Los Angeles and everywhere else. A `DTEND;VALUE=DATE` is exclusive,
  as in RFC 5545, and recurring all-day series expand day by day.
- **Ordering** is by `(date, all_day first, local start, uid)`; `uid` is the
  final tie-breaker for fragments that start at the same instant.
- A `RECURRENCE-ID` override with a `DATE` `DTSTART` turns that occurrence
  into an all-day entry, and vice versa — each instance is rendered
  according to its own effective `DTSTART` type.
- Zero-duration timed instances produce one fragment on their local date
  with both continuation flags `false`.

### Free/busy export semantics

- **Busy candidates** are the expanded instances of one calendar inside
  the shared UTC window, minus `STATUS:CANCELLED` instances (a cancelled
  master suppresses its whole series; a cancelled override removes only
  its occurrence) and minus non-positive-duration instances (an instant
  occupies no time).
- **`TRANSP` inheritance** mirrors the `ATTENDEE` rule used elsewhere: a
  recurring instance inherits the master event's `TRANSP`; a
  `RECURRENCE-ID` override that declares `TRANSP` explicitly replaces the
  inherited value for its instance, while an override without `TRANSP`
  keeps inheriting. `TRANSP:TRANSPARENT` (the default is `OPAQUE`) never
  blocks time. A `TRANSP` value other than `OPAQUE`/`TRANSPARENT` on an
  effective instance is rejected with `422`.
- **All-day `DATE` events** occupy their authored calendar dates as local
  full days in the calendar's own `timezone`: the UTC span runs from the
  first date's local midnight to the day after the last date's local
  midnight, computed per date with `zoneinfo`, so a spring-forward day
  occupies 23 UTC hours and a fall-back day 25. The expansion window is
  padded internally so an all-day event whose local span merely reaches
  into the window (up to the ±14h zone offset) is never missed.
- **Clipping and merging**: every busy interval is clipped to the
  half-open window `[window_start, window_end)`; overlapping *or
  touching* intervals then merge into one (`[a, b)` and `[b, c)` share no
  free instant), so back-to-back meetings yield a single `FREEBUSY` line.
- **Output**: one complete `VCALENDAR` text per calendar holding a single
  `VFREEBUSY` component with `UID` (the `calendar_id`, RFC 5545
  TEXT-escaped), `DTSTAMP` (generation time), `DTSTART`/`DTEND` (the UTC
  window) and one `FREEBUSY:<start>/<end>` line per merged period — UTC
  `Z` basic format, sorted by start. Calendars are returned sorted by
  `calendar_id`; an empty `calendars` list is allowed and yields
  `count: 0`.

### Compression semantics

- **Inputs** are equal-duration, non-overlapping instances with unique,
  UTC-aware starts and ends; `end` must be strictly after `start`. Input may
  arrive in any order — instances are sorted by start before compression.
- **Candidate grids**: the earliest start is `DTSTART` and the latest start
  is the rule's `UNTIL`. Each of the eight `DAILY`/`WEEKLY` × `INTERVAL`
  1–4 grids starts at `DTSTART` and steps in UTC (`n` days or `n` weeks) up
  to and including `UNTIL`.
- **Exceptions**: grid points not present in the input become `EXDATE`;
  input starts absent from the grid become `RDATE`. `DTSTART` is never
  listed in an `RDATE`. `exception_count` counts the remaining `EXDATE` and
  `RDATE` values.
- **Ranking** minimizes the UTF-8 byte length of the *complete* ICS
  (including folding), then breaks ties by fewer exceptions, an `RRULE`
  plan over the `RDATE` fallback, `DAILY` over `WEEKLY`, and smaller
  `INTERVAL`. The `DTSTART`+`RDATE` fallback (no rule, one `RDATE` per
  remaining instance) is always eligible.
- **Round-trip guarantee**: each candidate is expanded with the same engine
  as `/expand`; it only competes when the expanded start/end set equals the
  submitted set exactly, so the returned `ics` always re-expands faithfully.

### Fair rotation semantics

- **Candidates are organizer-local**: each candidate is the wall-clock time
  on its organizer-local `date` in `organizer_timezone`, materialized with
  `zoneinfo` and converted to UTC — so the same local time maps to
  different UTC instants across a DST transition, and every participant's
  view of the meeting follows their own zone's DST rules. The meeting
  occupies the half-open interval `[start, start + duration)` in UTC.
- **Feasibility reuses the expansion engine**: each participant's
  `busy_ics` is expanded with the exact `/expand` semantics (overrides,
  `EXDATE`, floating times, the 10 000-occurrence cap) over the span of all
  candidates; an instance blocks a candidate when it is not
  `STATUS:CANCELLED`, has a positive duration, and its half-open interval
  overlaps the meeting (a busy event merely touching the meeting's start
  or end does not). A blank `busy_ics` means the participant has no busy
  time. A candidate is feasible only when it is conflict-free for **every**
  participant; an organizer date left without any feasible candidate fails
  the request with `422`.
- **Deviation is measured per participant in local time**: a meeting costs
  a participant the minutes of its interval that fall outside the
  participant's `preferred_periods`, materialized per local date in the
  participant's own `timezone` (DST-correct, like `/availability` working
  hours; overlapping preferred periods count once). A meeting fully inside
  a preferred period costs 0, one fully outside costs its whole duration,
  and a participant with no preferred periods always deviates by the full
  duration. Costs accumulate over all dates into the participant's
  **cumulative deviation**.
- **Joint optimization**: exactly one candidate is chosen per date by
  enumerating the joint combinations (capped at 50 000; larger spaces are
  rejected with `422`). The plan lexicographically minimizes (1) the
  largest per-participant cumulative deviation, (2) the total deviation
  summed over all participants, and (3) candidate input order — earlier
  dates, then earlier candidates within their date, win remaining ties.
- **Response**: `meetings` lists one entry per date in request order with
  the chosen `candidate_index`, the UTC `start`/`end` (`Z`), the
  organizer-local `local_start`/`local_end` (offset-bearing), and the
  per-participant deviation of that meeting in participant request order;
  `participants` echoes each participant with their cumulative deviation.
  All minute values are integers when the inputs are minute-aligned
  (fractional only for sub-minute candidate or preference times).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v          # run the test suite
uvicorn app.main:app --reload       # run locally on port 8000
```

## Project layout

```
app/main.py          FastAPI app: POST /expand, POST /provenance,
                     POST /agenda, POST /alarms,
                     POST /attendance, POST /calendar-diff,
                     POST /availability, POST /conflicts, POST /validate,
                     POST /compress-schedule, POST /freebusy-export,
                     POST /fair-rotation, GET /health
app/expander.py      recurrence expansion, override merging, TZ/DST handling
app/provenance.py    per-occurrence provenance chains (RRULE/RDATE/DTSTART
                     generation, EXDATE, RECURRENCE-ID override, final values)
app/compressor.py    shortest ICS over DAILY/WEEKLY RRULE grids + RDATE fallback
app/agenda.py        local-day agenda fragments, midnight splitting, DATE handling
app/validation.py    structural VEVENT checks, stable issue codes, sorting
app/alarms.py        VALARM parsing, REPEAT/DURATION fan-out, window filtering
app/attendance.py    ATTENDEE inheritance, address normalization, PARTSTAT counts
app/diff.py          two-calendar instance diff by UID + RECURRENCE-ID
app/availability.py  working-period timeline, busy clipping/merge, free complement
app/conflicts.py     half-open sweep for minimum-concurrency segments
app/freebusy.py      TRANSP-aware busy periods, all-day local days, VFREEBUSY text
app/rotation.py      joint fair-rotation plan: DST-correct candidates, busy
                     filtering, deviation costs, lexicographic objectives
tests/               key tests for all endpoints (recurrences, DST, 422s)
Dockerfile           python:3.12-slim image running uvicorn on port 8000
docker-compose.yml   single-command startup
```

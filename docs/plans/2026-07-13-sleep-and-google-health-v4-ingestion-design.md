# Sleep Ingestion + Google Health API v4 Migration — Design

Date: 2026-07-13

## Problem

Two gaps, one root cause.

1. **Sleep is not ingested.** `sources/google_health.py` fetches intraday
   heart_rate/steps/distance/active_energy plus three *companion* sleep signals
   (`sleep_spo2`, `sleep_hrv`, `sleep_skin_temp`). Despite the module docstring
   listing `/1.2/user/-/sleep/date/{d}.json`, **nothing ever calls the sleep-log
   endpoint.** Sleep duration, sleep stages, and efficiency are absent from the
   raw and canonical layers entirely.

2. **The endpoints are on the wrong API.** The extractor targets
   `api.fitbit.com` on the assumption that "the Google Health migration delegates
   these endpoints under the same paths." That is no longer true. The **Fitbit
   Web API is being sunset (~Sept 2026)** and is superseded by the **Google
   Health API at `https://health.googleapis.com/v4`** — a full rebuild with a
   different request/response shape, different auth scopes, and a re-consent
   requirement (tokens do not transfer).

Since the whole `api.fitbit.com` surface is short-lived, the decision (below) is
to migrate the entire google_health extractor to v4 rather than bolt sleep onto
a dying API.

## Decisions taken

- **Scope: migrate the whole `google_health` extractor to the Health API v4.**
  All metrics (heart_rate, steps, distance, active_energy, sleep, spo2, hrv) move
  to `health.googleapis.com/v4`. No throwaway code on the Fitbit paths.
- **Sleep model: dedicated tables.** `raw.sleep_sessions` (session header +
  summary) and `raw.sleep_stages` (segment intervals), mirroring the existing
  `raw.activities` / `raw.activity_streams` pattern. Sleep is a *session*, not a
  scalar sample; it does not belong in `raw.samples`.
- **spo2/hrv: ingest both fine-grained samples AND overnight summaries.** v4
  exposes paired data types for each (confirmed below).
- **Also ingest resting heart rate and respiratory rate** (both daily scalars).
- **Daily summaries get their own grain: `raw.daily_metrics`, keyed on
  `local_date`.** The daily rollups (resting HR, respiratory rate, spo2 daily,
  hrv daily) are a distinct grain — one row per source per metric per *local
  calendar day* — so they get a dedicated table keyed on `(source, metric,
  local_date)` instead of being forced into the UTC-timestamped `raw.samples`.
  This bakes the timezone fix in from the start: the key is the source's local
  date, so travel across zones can't collide two "daily" rows into one UTC day
  or split one across two. Fine-grained spo2/hrv stay in `raw.samples`.
- **Sleep score: skipped.** Not exposed by the API (see findings); no derived
  proxy — we store `efficiency` and the raw stage minutes and stop there.
- **Pre-deployment: migrations edited freely in place.** Anduin isn't deployed,
  so there is no live-migration constraint — no append-only shadowing, no
  re-consent, no backfill. Existing migration files are edited directly.

## Findings from the v4 API docs

Sources:
- [Google Health API — about](https://developers.google.com/health/about)
- [Migration guide](https://developers.google.com/health/migration)
- [API specifications](https://developers.google.com/health/migration/api-specifications)
- [Data types catalog](https://developers.google.com/health/data-types)
- [dataPoints.list](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/list)
- [RPC message reference](https://developers.google.com/health/reference/rpc/google.devicesandservices.health.v4)

**Endpoint shape.** One consistent resource replaces ~120 per-metric Fitbit
paths:

```
GET https://health.googleapis.com/v4/users/me/dataTypes/{dataType}/dataPoints
    ?filter=<AIP-160 filter>&pageSize=<n>&pageToken=<tok>
```

- `list` → granular / fine-grained data points.
- `rollUp` / `dailyRollUp` → aggregated summaries over a date range.
- `pageSize`: default 1440, max 10000 — **except sleep & exercise, where default
  and max are both 25** (so long date ranges require pagination).
- `filter` follows AIP-160, e.g.
  `steps.interval.start_time >= "2026-07-01T00:00:00Z" AND steps.interval.start_time < "2026-07-02T00:00:00Z"`.
  Interval types filter on `interval.start_time`, sample types on sample
  observation time, daily types on the summary date. **Exact per-type filter
  field names must be confirmed against the catalog during implementation.**
- Response envelope: `{ "dataPoints": [ ... ], "nextPageToken": "..." }`.

**Confirmed data-type identifiers and scopes** (all pinned from the catalog):

| Concept | v4 dataType (endpoint) | Filter id | Record type | Ops | Scope |
|---|---|---|---|---|---|
| Sleep | `sleep` | `sleep` | Session | list, rollUp, dailyRollUp | `sleep` |
| Heart rate (intraday) | `heart-rate` | `heart_rate` | Sample | list, rollUp, dailyRollUp | `health_metrics_and_measurements` |
| Steps | `steps` | `steps` | Interval | list, rollUp, dailyRollUp | `activity_and_fitness` |
| Distance | `distance` | `distance` | Interval | list, rollUp, dailyRollUp | `activity_and_fitness` |
| Active energy | `active-energy-burned` | `active_energy_burned` | Interval | list, rollUp, dailyRollUp | `activity_and_fitness` |
| SpO2 fine-grained | `oxygen-saturation` | `oxygen_saturation` | Sample | list | `health_metrics_and_measurements` |
| SpO2 daily | `daily-oxygen-saturation` | `daily_oxygen_saturation` | Daily | list | `health_metrics_and_measurements` |
| HRV fine-grained | `heart-rate-variability` | `heart_rate_variability` | Sample | list | `health_metrics_and_measurements` |
| HRV daily | `daily-heart-rate-variability` | `daily_heart_rate_variability` | Daily | list | `health_metrics_and_measurements` |
| Resting heart rate | `daily-resting-heart-rate` | `daily_resting_heart_rate` | Daily | list | `health_metrics_and_measurements` |
| Respiratory rate | `daily-respiratory-rate` | `daily_respiratory_rate` | Daily | list | `health_metrics_and_measurements` |

Scope URLs take the form `https://www.googleapis.com/auth/googlehealth.{scope}.readonly`.
Three scopes total: `sleep`, `activity_and_fitness`, `health_metrics_and_measurements`.

**Daily message fields:**
- `daily-resting-heart-rate` → `beats_per_minute` (int), `date`; metadata carries
  the calc method (`WITH_SLEEP` vs `ONLY_WITH_AWAKE_DATA`) — worth keeping in `raw`.
- `daily-respiratory-rate` → `breaths_per_minute` (double), `date`. Single daily
  average only — **no per-sleep-stage breakdown** in this message.

**Sleep message fields** (`google.devicesandservices.health.v4`, Sleep):
- `interval` (start/end) — the session window.
- `sleep_type` — `STAGES` | `CLASSIC`.
- `sleep_stages[]` — each `{ start_time, end_time, stage_type }`, where
  `stage_type ∈ {DEEP, LIGHT, REM, AWAKE}`.
- `sleep_summary` — `minutes_asleep`, `minutes_awake`, `minutes_in_sleep_period`,
  `minutes_to_fall_asleep`, `minutes_after_wakeup`, `efficiency`,
  `stages_summary[]` (per-stage duration/count).

**SpO2 sample:** `{ percentage, sample_time }`.
**HRV sample:** `{ rmssd_milliseconds, sample_time }`.

**Sleep score is NOT available.** Neither the Fitbit Web API ("Sleep score is
not supported through the Web API") nor the Google Health API exposes Fitbit's
proprietary 0–100 sleep score. Per the decision above we **skip it entirely** —
store `efficiency` (which the API does give) and the raw stage minutes; no
derived proxy.

## Schema changes

Pre-deployment, so migrations are edited in place — no `CREATE OR REPLACE`
shadowing over `0007`, no new-file-only rule. Concretely: **retarget the
spo2/hrv views and add the daily/resting/respiratory views directly in `0007`**;
add the sleep tables and sleep views in new files after it (the sleep views
reference `raw.sleep_sessions`, so the tables must be created first). New raw
tables inherit `anduin_ro` read access automatically via the default privileges
in `0009_readonly_grants.sql`.

### `0010_raw_sleep.sql`

```sql
-- Session header. One row = one sleep session from one source.
CREATE TABLE IF NOT EXISTS raw.sleep_sessions (
    source                 text        NOT NULL,
    session_uid            text        NOT NULL,   -- v4 dataPoint name / logId
    device                 text,
    recording_method       text,
    started_at             timestamptz NOT NULL,   -- interval.start
    ended_at               timestamptz NOT NULL,   -- interval.end
    is_main_sleep          boolean,
    sleep_type             text,                   -- STAGES | CLASSIC
    minutes_asleep         integer,
    minutes_awake          integer,
    minutes_in_sleep_period integer,
    minutes_to_fall_asleep integer,
    minutes_after_wakeup   integer,
    efficiency             double precision,
    summary                jsonb,                  -- stages_summary + extras
    raw                    jsonb        NOT NULL,
    natural_key            text         NOT NULL,  -- for restatement trigger
    ingested_at            timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (source, session_uid),
    CONSTRAINT sleep_sessions_valid_range CHECK (ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS sleep_sessions_time
    ON raw.sleep_sessions (started_at DESC);

-- Reuse the generic restatement audit trigger (needs natural_key + raw).
DROP TRIGGER IF EXISTS sleep_sessions_restatement ON raw.sleep_sessions;
CREATE TRIGGER sleep_sessions_restatement
    BEFORE UPDATE ON raw.sleep_sessions
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();

-- Stage segments. Mirrors raw.activity_streams (hypertable on the interval start).
CREATE TABLE IF NOT EXISTS raw.sleep_stages (
    source       text        NOT NULL,
    session_uid  text        NOT NULL,
    stage        text        NOT NULL,   -- DEEP | LIGHT | REM | AWAKE
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sleep_stages_valid_range CHECK (ended_at >= started_at)
);
SELECT create_hypertable('raw.sleep_stages', 'started_at',
    chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS sleep_stages_uk
    ON raw.sleep_stages (source, session_uid, started_at);
ALTER TABLE raw.sleep_stages SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source, session_uid',
    timescaledb.compress_orderby = 'started_at DESC');
SELECT add_compression_policy('raw.sleep_stages', INTERVAL '30 days',
    if_not_exists => TRUE);
```

(`raw.sleep_sessions` stays a plain table — one row per night is tiny; no
hypertable needed. Compression/hypertable only on the segment table.)

### Fine-grained spo2 / hrv land in `raw.samples`

The two *sample* types are continuous point readings → they fit the existing
tall table. This replaces today's `sleep_spo2` / `sleep_hrv` companion metrics
with real fine-grained series:

| Metric | Source of value | Unit | valid_from / valid_to |
|---|---|---|---|
| `spo2` | `oxygen-saturation`.percentage | % | sample_time (point) |
| `hrv` | `heart-rate-variability`.rmssd_milliseconds | ms | sample_time (point) |

### Daily summaries land in `raw.daily_metrics` (new table, keyed on `local_date`)

Add this table to `0003_raw_samples.sql` — it sits beside `raw.samples` as the
other raw scalar grain, and must exist before the `0007` canonical views that
read it. `local_date` comes straight from the v4 daily message's `date` field,
which is already "in the user's timezone" — no computation, no UTC bucketing.

```sql
-- One row = one source's daily summary for one metric on one LOCAL calendar day.
-- Keyed on local_date (not a UTC timestamp) so timezone travel can't collide or
-- split "daily" rows. tz_offset_minutes is recorded when the source supplies it.
CREATE TABLE IF NOT EXISTS raw.daily_metrics (
    source            text             NOT NULL,
    device            text,
    recording_method  text,
    metric            text             NOT NULL,
    value             double precision NOT NULL,
    unit              text,
    local_date        date             NOT NULL,   -- from v4 message .date
    tz_offset_minutes integer,                      -- nullable; if source gives it
    raw               jsonb            NOT NULL,
    natural_key       text             NOT NULL,    -- for restatement trigger
    ingested_at       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (source, metric, local_date)
);
CREATE INDEX IF NOT EXISTS daily_metrics_metric_date
    ON raw.daily_metrics (metric, local_date DESC);

DROP TRIGGER IF EXISTS daily_metrics_restatement ON raw.daily_metrics;
CREATE TRIGGER daily_metrics_restatement
    BEFORE UPDATE ON raw.daily_metrics
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();
```

(No hypertable — one row per metric per day is tiny, same reasoning as
`raw.sleep_sessions`.)

| Metric | Source of value | Unit |
|---|---|---|
| `spo2_daily_avg/min/max` | `daily-oxygen-saturation` (avg/min/max) | % |
| `hrv_daily_rmssd` | `daily-heart-rate-variability` | ms |
| `resting_heart_rate` | `daily-resting-heart-rate`.beats_per_minute | bpm |
| `respiratory_rate` | `daily-respiratory-rate`.breaths_per_minute | br/min |

> Sleep summaries have the same latent local-day question (which night does a
> session belong to?), but sleep already keys on the session interval / `logId`,
> not a UTC day, so it doesn't collide. `canonical.sleep` still buckets nights
> with `date_trunc('day', started_at)` in UTC; giving `raw.sleep_sessions` a
> `date_of_sleep date` column is the parallel fix, deferred (the API doesn't
> hand us a local date for sleep the way the daily types do).

### Edit `0007_canonical_views.sql` in place — scalar views

The existing `canonical.spo2`/`hrv` views union the soon-to-be-removed
`sleep_spo2`/`sleep_hrv` metrics; retarget the fine-grained views to
`canonical.samples`, and point the daily/resting/respiratory views at the new
`raw.daily_metrics` table (exposing `local_date`, not a UTC window):

```sql
-- Fine-grained (continuous samples).
CREATE OR REPLACE VIEW canonical.spo2 AS
    SELECT * FROM canonical.samples WHERE metric = 'spo2';
CREATE OR REPLACE VIEW canonical.hrv AS
    SELECT * FROM canonical.samples WHERE metric = 'hrv';

-- Daily summaries (one row per local_date). Single source today; DISTINCT ON
-- keeps it one-row-per-day if a second source ever lands the same metric.
CREATE OR REPLACE VIEW canonical.daily_metrics AS
    SELECT DISTINCT ON (metric, local_date)
        metric, local_date, source, device, value, unit, tz_offset_minutes
    FROM raw.daily_metrics
    ORDER BY metric, local_date, ingested_at DESC;

CREATE OR REPLACE VIEW canonical.spo2_daily AS
    SELECT * FROM canonical.daily_metrics
    WHERE metric IN ('spo2_daily_avg','spo2_daily_min','spo2_daily_max');
CREATE OR REPLACE VIEW canonical.hrv_daily AS
    SELECT * FROM canonical.daily_metrics WHERE metric = 'hrv_daily_rmssd';
CREATE OR REPLACE VIEW canonical.resting_heart_rate AS
    SELECT * FROM canonical.daily_metrics WHERE metric = 'resting_heart_rate';
CREATE OR REPLACE VIEW canonical.respiratory_rate AS
    SELECT * FROM canonical.daily_metrics WHERE metric = 'respiratory_rate';
```

(The `skin_temp` view keeps its `sleep_skin_temp` union — that metric has no v4
replacement pinned yet and is out of scope for this change.)

### `0011_sleep_canonical.sql` — sleep views (need the new tables)

```sql
-- Canonical sleep: pick one session per night (main sleep wins), source
-- precedence-ready even though google_health is the only source today.
CREATE OR REPLACE VIEW canonical.sleep AS
SELECT DISTINCT ON (date_trunc('day', started_at))
    started_at, ended_at, source, device,
    is_main_sleep, sleep_type,
    minutes_asleep, minutes_awake, minutes_in_sleep_period,
    minutes_to_fall_asleep, minutes_after_wakeup, efficiency,
    summary
FROM raw.sleep_sessions
ORDER BY date_trunc('day', started_at), is_main_sleep DESC NULLS LAST,
         minutes_asleep DESC NULLS LAST;

CREATE OR REPLACE VIEW canonical.sleep_stages AS
SELECT st.* FROM raw.sleep_stages st;
```

## OAuth / scopes

The token machinery already targets Google (`oauth2.googleapis.com/token` in
`oauth.py`) and is provider-agnostic (load/refresh a saved token). One change:
request the three v4 scopes at authorize time (`oauth_flow.py`) —
`googlehealth.sleep.readonly`, `googlehealth.activity_and_fitness.readonly`,
`googlehealth.health_metrics_and_measurements.readonly`. Pre-deployment there is
no existing token, so the first `anduin auth google-health` simply consents to
these scopes — no re-consent migration to manage.

## Extractor rewrite (`sources/google_health.py`)

Replace the Fitbit-path helpers with a small v4 client and per-type emitters.

```
BASE = "https://health.googleapis.com/v4"

_list_datapoints(http, token, data_type, since, until, page_size) -> Iterator[dict]
    # builds the AIP-160 filter, loops on nextPageToken, yields dataPoints

_emit_sleep(dp)        -> (session_row: dict, stage_rows: list[dict])
_emit_spo2(dp)         -> raw.samples row       (metric='spo2')
_emit_hrv(dp)          -> raw.samples row       (metric='hrv')
_emit_spo2_daily(dp)   -> up to 3 daily_metrics rows (avg/min/max), local_date=dp.date
_emit_hrv_daily(dp)    -> daily_metrics row      (metric='hrv_daily_rmssd')
_emit_resting_hr(dp)   -> daily_metrics row      (metric='resting_heart_rate')
_emit_respiratory(dp)  -> daily_metrics row      (metric='respiratory_rate')
_emit_intraday(...)    -> heart_rate/steps/distance/active_energy (retargeted)

extract():
    token = access_token(..., GOOGLE_HEALTH, ...)
    for each data_type:
        buffer rows per day (keep the existing per-day durability guard)
        upsert via the matching helper
```

The daily emitters read `local_date` straight from the v4 message's `date`
field and pass it through — no UTC math.

New upsert helpers in `upsert.py`:

```python
def upsert_sleep(conn, session_row: dict, stage_rows: list[dict]) -> None:
    # header ON CONFLICT (source, session_uid) DO UPDATE ...
    # stages executemany ON CONFLICT (source, session_uid, started_at) DO UPDATE ...
    # single transaction, same shape as upsert_strength

def upsert_daily_metrics(conn, rows: Iterable[dict]) -> int:
    # executemany INSERT ... ON CONFLICT (source, metric, local_date) DO UPDATE
    #   SET value, unit, tz_offset_minutes, raw, ingested_at = now()
```

Fine-grained spo2/hrv reuse `upsert_samples` unchanged.

**Idempotency / natural keys:**
- sleep session: `session_uid` = v4 dataPoint `name` (or `logId`); `natural_key`
  = `f"sleep|{session_uid}"`.
- sleep stage: keyed by `(source, session_uid, started_at)` — no natural_key
  column (matches `activity_streams`).
- spo2/hrv sample: `f"spo2|{sample_time.isoformat()}"` / `f"hrv|..."`.
- daily: PK `(source, metric, local_date)` gives idempotency directly;
  `natural_key` (for the restatement trigger) = `f"{metric}|{local_date}"`, e.g.
  `f"resting_heart_rate|{local_date}"`, `f"spo2_daily_avg|{local_date}"`.

## Testing

- `tests/test_google_health.py` is rewritten against **recorded v4 fixtures**
  (the old Fitbit fixtures are obsolete). Cover: sleep session + stages emission,
  pagination via `nextPageToken` (important — sleep pageSize is 25), spo2/hrv
  fine-grained + daily, and idempotent re-pull (second run restates, count
  stable).
- `local_date` test: a daily message dated `2026-03-15` lands one
  `raw.daily_metrics` row keyed on that date regardless of the fetch's UTC
  window; a same-metric row for an adjacent local date does not collide.
- Add a migration smoke test asserting `raw.sleep_sessions`,
  `raw.sleep_stages`, and `raw.daily_metrics` exist and the canonical
  sleep/daily views resolve.

## Cutover & risks

- **Timezone / travel skew on daily records — addressed for daily_metrics.** The
  v4 daily types are dated in the user's local timezone; `raw.daily_metrics` now
  keys on that `local_date` directly (not a UTC window), so travel across zones
  can't collide or split daily rows. *Remaining gap (deferred):* sleep-session
  night bucketing in `canonical.sleep` still uses `date_trunc('day', started_at)`
  in UTC; the parallel fix is a `date_of_sleep` column on `raw.sleep_sessions`,
  not done here because the v4 sleep message doesn't hand us a local date the way
  the daily types do.
- **All identifiers pinned.** heart-rate/steps/distance/active-energy-burned and
  the daily resting-HR/respiratory-rate types are confirmed from the catalog. The
  only per-type detail still to verify at code time is the exact AIP-160 filter
  field for each record type (interval vs sample vs daily), which the list-method
  docs describe generically.
- **pageSize=25 for sleep** — `_list_datapoints` follows `nextPageToken` or long
  backfills silently truncate. Implemented; confirm on the live verification pass
  (HTTP glue isn't unit-tested here, per repo convention).

## Task list

- [x] 1. Edit `0003_raw_samples.sql` — `raw.daily_metrics` table (+ index,
   restatement trigger) beside `raw.samples`.
- [x] 2. `0010_raw_sleep.sql` — sleep tables, trigger, hypertable, compression.
- [x] 3. Edit `0007_canonical_views.sql` in place — retargeted spo2/hrv views,
   added `canonical.daily_metrics` + spo2_daily/hrv_daily/resting_heart_rate/
   respiratory_rate views over it.
- [x] 4. `0011_sleep_canonical.sql` — `canonical.sleep` + `canonical.sleep_stages`.
- [x] 5. `oauth_flow.py` — requests the three v4 scopes.
- [x] 6. `upsert.py` — `upsert_sleep`, `upsert_daily_metrics` (TDD, 5 tests);
   fine-grained spo2/hrv reuse `upsert_samples`.
- [x] 7. `sources/google_health.py` — rewritten around the v4 `dataPoints`
   client (pagination via `nextPageToken`) + per-type emitters (TDD, 9 tests).
   Covers: **sleep, spo2 (+daily), hrv (+daily), resting_heart_rate,
   respiratory_rate**.
- [x] 8. **Intraday `heart-rate` (sample) + `steps`/`distance`/
   `active-energy-burned` (interval)** — implemented into `raw.samples` (TDD, 4
   tests). Proto field names confirmed (`beatsPerMinute`/`count`/`meters`/`kcal`);
   JSON envelope inferred and flagged in-code. Metric names/units held to the
   pre-v4 values (`active_energy`, distance in km) so `canonical.neat_energy`/
   `total_energy`/`steps` keep working. **Needs live confirmation of the JSON
   envelope + filter fields** — a one-line-per-emitter fix if a key differs.
- [x] 9. **AIP-160 filter fields verified live.** All 11 types return 200 OK
   (`errors=0`) on a real dry-run. Two fields were corrected from live 400 bodies
   (TDD, 4 `_filter_expr` tests): sleep filters on `interval.end_time` (not
   start_time — the "special handling"), and daily types filter on `.date` with a
   **civil date** (`2026-07-06`, no time), not an RFC-3339 timestamp.
- [x] 10. Pagination + `extract()` orchestration exercised live; per-type status
   logging added; `http.py` now fails fast on 4xx and surfaces the response body
   (TDD, 4 tests) — this is what turned an opaque 400 into `ACCOUNT_NOT_LINKED` /
   `INVALID_ARGUMENT` diagnostics.
- [ ] 11. Web UI (`web/routes/metrics.py`, templates) — surface sleep + resting
   HR + respiratory rate once data lands (separate, follow-on).
- [~] 12. **Auth + full dry-run pull validated live: every endpoint, scope, and
   filter is correct (`errors=0`).** Remaining: (a) the account had 0 data points
   in every probed window (freshly linked — sync lag), so **payload field names
   are still inferred, not yet confirmed against a real dataPoint**; (b) canonical
   views need a real DB + `git add` of the new migrations to exercise.

### Status

Full extractor implemented, **78 tests pass, ruff clean**, and **live-validated
against the real v4 API: all 11 data types return 200 OK, `errors=0`.** Auth,
scopes, endpoints, and all filter fields are confirmed correct.

Two things outstanding:
1. **Payload field names still inferred.** Every probed window returned 0 data
   points (account freshly linked → sync lag), so the emitter field names
   (`dailyOxygenSaturation.average`, `heartRate.beatsPerMinute`, sleep nesting,
   etc.) haven't been confirmed against a real dataPoint. Emitters store `raw`
   unconditionally and skip rows with missing fields, so a wrong key degrades
   gracefully (no crash) — it's a one-line-per-emitter fix once data appears.
2. **DB path unexercised.** dry-run needs no DB; a real pull + canonical views
   need a TimescaleDB and `git add` of migrations `0010`/`0011` (untracked, so
   not yet in the nix build).
```

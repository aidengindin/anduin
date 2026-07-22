-- Real-data fixes surfaced by the first Fitbit-Air pull:
--   1. Sleep clock times were rendered in UTC because the wearer's local offset
--      lives in a sibling ``startUtcOffset`` field, not the 'Z' startTime. The
--      extractor now captures tz_offset_minutes; expose it (+ local wall-clock
--      timestamps) through the sleep views so the UI can show local times.
--   2. Google's daily HRV rollup is empty for the Air, so hrv_daily showed stale
--      values. Derive nightly HRV from the per-sample RMSSD stream (measured only
--      during sleep) averaged over each night, keyed to the local wake date.
--   3. Steps were shown as the latest 1-minute sample instead of a daily total.
--      Add canonical.steps_daily summing samples by their civil (local) date.

-- 1a. Surface tz_offset_minutes on canonical.sleep (appended column).
CREATE OR REPLACE VIEW canonical.sleep AS
SELECT DISTINCT ON (date_trunc('day', started_at))
    started_at,
    ended_at,
    source,
    device,
    is_main_sleep,
    sleep_type,
    minutes_asleep,
    minutes_awake,
    minutes_in_sleep_period,
    minutes_to_fall_asleep,
    minutes_after_wakeup,
    efficiency,
    summary,
    session_uid,
    tz_offset_minutes
FROM raw.sleep_sessions
ORDER BY date_trunc('day', started_at),
         is_main_sleep DESC NULLS LAST,
         minutes_asleep DESC NULLS LAST;

-- 1b. sleep_efficiency carries the offset + local wall-clock start/end so the UI
-- reads local times directly. ``*_local`` are naive timestamps (local wall clock)
-- built by shifting the UTC instant by the wearer's offset; when the offset is
-- unknown they fall back to the UTC wall clock (documented best-effort).
-- New columns are appended at the END of the select list so CREATE OR REPLACE
-- accepts the change (it may add trailing columns but not reorder existing ones).
CREATE OR REPLACE VIEW derived.sleep_efficiency AS
WITH e AS (
    SELECT
        started_at,
        ended_at,
        tz_offset_minutes,
        minutes_asleep,
        efficiency AS source_efficiency,
        extract(epoch FROM (ended_at - started_at)) / 60.0 AS time_in_bed_min
    FROM canonical.sleep
)
SELECT
    started_at,
    ended_at,
    time_in_bed_min,
    minutes_asleep,
    CASE WHEN time_in_bed_min > 0
         THEN 100.0 * minutes_asleep / time_in_bed_min END AS efficiency_pct,
    source_efficiency,
    avg(CASE WHEN time_in_bed_min > 0
             THEN 100.0 * minutes_asleep / time_in_bed_min END)
        OVER (ORDER BY started_at
              RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW)
        AS efficiency_pct_avg_7d,
    tz_offset_minutes,
    (started_at AT TIME ZONE 'UTC')
        + make_interval(mins => COALESCE(tz_offset_minutes, 0)) AS started_local,
    (ended_at AT TIME ZONE 'UTC')
        + make_interval(mins => COALESCE(tz_offset_minutes, 0)) AS ended_local
FROM e;

-- 2. Nightly HRV from the per-sample RMSSD stream. The Air only records HRV
-- during sleep, so every hrv sample falls inside a sleep session's window; we
-- average those and key the result to the night's local wake date. This replaces
-- the (empty-for-Fitbit) daily-HRV rollup that hrv_daily used to read.
DROP VIEW IF EXISTS canonical.hrv_daily;
CREATE VIEW canonical.hrv_daily AS
SELECT
    s.session_uid,
    ((s.ended_at AT TIME ZONE 'UTC')
        + make_interval(mins => COALESCE(s.tz_offset_minutes, 0)))::date AS local_date,
    s.source,
    avg(h.value)   AS value,
    'ms'::text     AS unit,
    count(h.value) AS sample_n
FROM canonical.sleep s
JOIN canonical.hrv h
  ON h.valid_from >= s.started_at
 AND h.valid_from <  s.ended_at
GROUP BY s.session_uid, local_date, s.source;

-- 2b. Repoint HRV status at the sample-derived nightly view.
CREATE OR REPLACE VIEW derived.hrv_status AS
WITH d AS (
    SELECT local_date, value FROM canonical.hrv_daily
),
w AS (
    SELECT local_date, value,
        avg(value)         OVER w7  AS hrv_7d,
        avg(value)         OVER w60 AS base_mean,
        stddev_samp(value) OVER w60 AS base_sd,
        count(*)           OVER w60 AS base_n
    FROM d
    WINDOW
        w7  AS (ORDER BY local_date RANGE BETWEEN INTERVAL '6 days'  PRECEDING AND CURRENT ROW),
        w60 AS (ORDER BY local_date RANGE BETWEEN INTERVAL '59 days' PRECEDING AND CURRENT ROW)
)
SELECT local_date, value, hrv_7d, base_mean, base_sd, base_n,
    base_mean - base_sd AS low_thresh,
    base_mean + base_sd AS high_thresh,
    CASE
        WHEN base_n < 14 OR base_sd IS NULL     THEN 'pending'
        WHEN hrv_7d < base_mean - base_sd       THEN 'low'
        WHEN hrv_7d > base_mean + base_sd       THEN 'high'
        ELSE 'balanced'
    END AS status
FROM w;

-- 3. Daily step totals. raw.samples has no local-date column, but Google stamps
-- each interval with a civil (local) date; use it directly so day boundaries
-- match the wearer's clock, falling back to the UTC date if civil is absent.
CREATE OR REPLACE VIEW canonical.steps_daily AS
SELECT
    CASE
        WHEN raw #> '{steps,interval,civilStartTime,date}' IS NOT NULL THEN
            make_date(
                (raw #>> '{steps,interval,civilStartTime,date,year}')::int,
                (raw #>> '{steps,interval,civilStartTime,date,month}')::int,
                (raw #>> '{steps,interval,civilStartTime,date,day}')::int)
        ELSE (valid_from AT TIME ZONE 'UTC')::date
    END            AS local_date,
    source,
    device,
    sum(value)     AS value,
    'count'::text  AS unit
FROM raw.samples
WHERE metric = 'steps'
GROUP BY 1, 2, 3;

-- Read-only role needs SELECT on the newly created views.
GRANT SELECT ON canonical.hrv_daily     TO anduin_ro;
GRANT SELECT ON canonical.steps_daily   TO anduin_ro;

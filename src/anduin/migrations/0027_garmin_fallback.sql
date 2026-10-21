-- Garmin Connect (Personal API) as a strictly whole-day fallback for the Air.
--
-- A Garmin value is used only for a day or night the Air has *nothing* for;
-- otherwise Garmin is ignored entirely. The extractor never writes raw.samples
-- (see sources/garmin.py), so every sample-grained view -- the HR union,
-- NEAT/total energy, workout calories, the CAGGs -- is untouched by
-- construction. What remains is arbitrating the daily/session-grained views,
-- each of which today either has no source preference or knows only the Air:
--
--   1. canonical.daily_metrics   latest-ingest-wins -> google_health first
--   2. canonical.sleep           drop Garmin nights an Air session overlaps
--   3. canonical.hrv_daily       + Garmin lastNightAvg for dates the Air lacks
--   4. canonical.spo2_daily      + Garmin sleep SpO2 for dates the Air lacks
--   5. canonical.activity_daily  Garmin daily steps for days with no Air steps
--
-- Garmin nights carry no stage timeline (the API exposes per-stage totals
-- only), so they show no hypnogram/composition and add nothing to SRI.

-- 1. Prefer the Air over a later Garmin ingest of the same daily metric. Only
-- resting_heart_rate shares a name across the two sources today; Garmin's other
-- daily metrics have names of their own.
CREATE OR REPLACE VIEW canonical.daily_metrics AS
SELECT DISTINCT ON (metric, local_date)
    metric,
    local_date,
    source,
    device,
    value,
    unit,
    tz_offset_minutes
FROM raw.daily_metrics
ORDER BY metric, local_date, (source = 'google_health') DESC, ingested_at DESC;

-- 2. A Garmin night is used only when no Air session overlaps it. Overlap, not
-- the DISTINCT ON day key, decides: the two devices can put the same night's
-- start on different UTC days, which the day key would read as two nights.
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
FROM raw.sleep_sessions ss
WHERE NOT (
    ss.source = 'garmin'
    AND EXISTS (
        SELECT 1 FROM raw.sleep_sessions g
        WHERE g.source = 'google_health'
          AND g.started_at < ss.ended_at
          AND g.ended_at   > ss.started_at
    )
)
ORDER BY date_trunc('day', started_at),
         is_main_sleep DESC NULLS LAST,
         minutes_asleep DESC NULLS LAST;

-- 3. Nightly HRV: the 0018 sample-derived arm unchanged, plus Garmin's
-- overnight average for any local date that arm has no row for. Columns and
-- types match 0018 so CREATE OR REPLACE accepts it.
CREATE OR REPLACE VIEW canonical.hrv_daily AS
WITH air AS (
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
    GROUP BY s.session_uid, local_date, s.source
)
SELECT session_uid, local_date, source, value, unit, sample_n FROM air
UNION ALL
SELECT
    NULL::text            AS session_uid,
    d.local_date          AS local_date,
    d.source              AS source,
    d.value               AS value,
    'ms'::text            AS unit,
    NULL::bigint          AS sample_n
FROM raw.daily_metrics d
WHERE d.source = 'garmin' AND d.metric = 'hrv_nightly'
  AND NOT EXISTS (SELECT 1 FROM air WHERE air.local_date = d.local_date);

-- 4. Nightly SpO2, same shape. The 70% floor is 0019's guard against the Air's
-- 50.0 dropped-read sentinel; it is applied to Garmin's min/avg too so a bad
-- reading can't trip the desaturation flag from either source.
CREATE OR REPLACE VIEW canonical.spo2_daily AS
WITH air AS (
    SELECT
        s.session_uid,
        ((s.ended_at AT TIME ZONE 'UTC')
            + make_interval(mins => COALESCE(s.tz_offset_minutes, 0)))::date AS local_date,
        s.source,
        avg(o.value)   AS spo2_avg,
        min(o.value)   AS spo2_min,
        max(o.value)   AS spo2_max,
        count(o.value) AS sample_n
    FROM canonical.sleep s
    JOIN canonical.spo2 o
      ON o.valid_from >= s.started_at
     AND o.valid_from <  s.ended_at
     AND o.value >= 70
    GROUP BY s.session_uid, local_date, s.source
),
garmin AS (
    SELECT
        local_date,
        max(value) FILTER (WHERE metric = 'spo2_nightly_avg') AS spo2_avg,
        max(value) FILTER (WHERE metric = 'spo2_nightly_min') AS spo2_min,
        max(value) FILTER (WHERE metric = 'spo2_nightly_max') AS spo2_max
    FROM raw.daily_metrics
    WHERE source = 'garmin'
      AND metric IN ('spo2_nightly_avg', 'spo2_nightly_min', 'spo2_nightly_max')
      AND value >= 70
    GROUP BY local_date
)
SELECT session_uid, local_date, source, spo2_avg, spo2_min, spo2_max, sample_n FROM air
UNION ALL
SELECT
    NULL::text      AS session_uid,
    g.local_date    AS local_date,
    'garmin'::text  AS source,
    g.spo2_avg      AS spo2_avg,
    g.spo2_min      AS spo2_min,
    g.spo2_max      AS spo2_max,
    NULL::bigint    AS sample_n
FROM garmin g
WHERE g.spo2_avg IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM air WHERE air.local_date = g.local_date);

-- 5. activity_daily: 0022's matview plus the steps fallback. On a civil day
-- with no wearable step samples at all, Garmin's daily total becomes the day's
-- total. Garmin has no intraday steps, so there is no NEAT/workout split to
-- read; NEAT is estimated as Garmin's total minus that day's workout steps
-- (floored at 0), which makes the GROUPING SETS total come out as Garmin's
-- number. The trigger is deliberately all-or-nothing: any Air steps on a day,
-- however few, and Garmin is ignored for it.
DROP MATERIALIZED VIEW IF EXISTS canonical.activity_daily;

CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.activity_daily AS
WITH step_samples AS (
    -- Every wearable step sample with its source-local civil date (0021/0022's
    -- resolution). Today google_health is the only source writing these.
    SELECT
        CASE
            WHEN s.raw #> '{steps,interval,civilStartTime,date}' IS NOT NULL THEN
                make_date(
                    (s.raw #>> '{steps,interval,civilStartTime,date,year}')::int,
                    (s.raw #>> '{steps,interval,civilStartTime,date,month}')::int,
                    (s.raw #>> '{steps,interval,civilStartTime,date,day}')::int)
            WHEN s.raw #>> '{steps,interval,startTime}' IS NOT NULL THEN
                left(s.raw #>> '{steps,interval,startTime}', 10)::date
            ELSE (s.valid_from AT TIME ZONE 'UTC')::date
        END AS local_date,
        s.condition,
        s.value
    FROM canonical.samples_with_condition s
    WHERE s.metric = 'steps'
),
workout_steps AS (
    SELECT
        CASE
            WHEN a.raw->>'start_date_local' IS NOT NULL THEN
                left(a.raw->>'start_date_local', 10)::date
            WHEN a.raw->>'text' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
                left(a.raw->>'text', 10)::date
            ELSE (w.started_at AT TIME ZONE 'UTC')::date
        END AS local_date,
        w.steps AS value
    FROM canonical.workout_steps w
    JOIN raw.activities a
      ON a.source = w.source AND a.activity_uid = w.activity_uid
    WHERE w.steps IS NOT NULL AND w.ended_at IS NOT NULL
),
garmin_fallback_steps AS (
    SELECT d.local_date, d.value
    FROM raw.daily_metrics d
    WHERE d.source = 'garmin' AND d.metric = 'steps_daily_total'
      AND NOT EXISTS (SELECT 1 FROM step_samples s WHERE s.local_date = d.local_date)
),
parts AS (
    SELECT local_date, 'steps'::text AS metric, 'neat'::text AS kind, value, 'count'::text AS unit
    FROM step_samples
    WHERE condition = 'outside_recorded_workout'
    UNION ALL
    SELECT local_date, 'steps', 'workout', value, 'count'
    FROM workout_steps
    UNION ALL
    SELECT g.local_date, 'steps', 'neat',
           greatest(g.value - COALESCE(
               (SELECT sum(w.value) FROM workout_steps w WHERE w.local_date = g.local_date),
               0), 0),
           'count'
    FROM garmin_fallback_steps g
    UNION ALL
    SELECT
        CASE
            WHEN s.raw #> '{activeEnergyBurned,interval,civilStartTime,date}' IS NOT NULL THEN
                make_date(
                    (s.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,year}')::int,
                    (s.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,month}')::int,
                    (s.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,day}')::int)
            WHEN s.raw #>> '{activeEnergyBurned,interval,startTime}' IS NOT NULL THEN
                left(s.raw #>> '{activeEnergyBurned,interval,startTime}', 10)::date
            ELSE (s.valid_from AT TIME ZONE 'UTC')::date
        END,
        'active_calories', 'neat', s.value, 'kcal'
    FROM canonical.samples_with_condition s
    WHERE s.metric = 'active_energy' AND s.condition = 'outside_recorded_workout'
    UNION ALL
    SELECT
        CASE
            WHEN a.raw->>'start_date_local' IS NOT NULL THEN
                left(a.raw->>'start_date_local', 10)::date
            WHEN a.raw->>'text' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
                left(a.raw->>'text', 10)::date
            ELSE (w.started_at AT TIME ZONE 'UTC')::date
        END,
        'active_calories', 'workout', w.calories, 'kcal'
    FROM canonical.workout_load w
    JOIN raw.activities a
      ON a.source = w.source AND a.activity_uid = w.activity_uid
    WHERE w.calories IS NOT NULL AND w.ended_at IS NOT NULL
)
SELECT local_date, metric,
       CASE WHEN GROUPING(kind) = 1 THEN 'total' ELSE kind END AS kind,
       sum(value) AS value, unit
FROM parts
GROUP BY GROUPING SETS ((local_date, metric, kind, unit), (local_date, metric, unit));

CREATE UNIQUE INDEX IF NOT EXISTS activity_daily_metric_kind_date
    ON canonical.activity_daily (metric, kind, local_date);

GRANT SELECT ON canonical.activity_daily TO anduin_ro;

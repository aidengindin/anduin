-- Daily activity totals split into non-exercise (NEAT) and recorded-workout
-- contributions. canonical.total_{steps,energy} already prevent double
-- counting; this view adds the source-local calendar date needed by the UI.
--
-- Google interval payloads retain civilStartTime. Intervals workouts retain
-- start_date_local. Read those historical raw payloads directly so this works
-- without re-ingestion; UTC is only a fallback for legacy/incomplete rows.
CREATE OR REPLACE VIEW canonical.activity_daily AS
WITH neat_steps AS (
    SELECT
        CASE
            WHEN r.raw #> '{steps,interval,civilStartTime,date}' IS NOT NULL THEN
                make_date(
                    (r.raw #>> '{steps,interval,civilStartTime,date,year}')::int,
                    (r.raw #>> '{steps,interval,civilStartTime,date,month}')::int,
                    (r.raw #>> '{steps,interval,civilStartTime,date,day}')::int)
            WHEN r.raw #>> '{steps,interval,startTime}' IS NOT NULL THEN
                left(r.raw #>> '{steps,interval,startTime}', 10)::date
            ELSE (n.valid_from AT TIME ZONE 'UTC')::date
        END AS local_date,
        n.steps AS value
    FROM canonical.neat_steps n
    LEFT JOIN LATERAL (
        SELECT s.raw
        FROM raw.samples s
        WHERE s.source = n.source
          AND s.metric = 'steps'
          AND s.valid_from = n.valid_from
        ORDER BY s.ingested_at DESC
        LIMIT 1
    ) r ON true
),
neat_energy AS (
    SELECT
        CASE
            WHEN r.raw #> '{activeEnergyBurned,interval,civilStartTime,date}' IS NOT NULL THEN
                make_date(
                    (r.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,year}')::int,
                    (r.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,month}')::int,
                    (r.raw #>> '{activeEnergyBurned,interval,civilStartTime,date,day}')::int)
            WHEN r.raw #>> '{activeEnergyBurned,interval,startTime}' IS NOT NULL THEN
                left(r.raw #>> '{activeEnergyBurned,interval,startTime}', 10)::date
            ELSE (n.valid_from AT TIME ZONE 'UTC')::date
        END AS local_date,
        n.kcal AS value
    FROM canonical.neat_energy n
    LEFT JOIN LATERAL (
        SELECT s.raw
        FROM raw.samples s
        WHERE s.source = n.source
          AND s.metric = 'active_energy'
          AND s.valid_from = n.valid_from
        ORDER BY s.ingested_at DESC
        LIMIT 1
    ) r ON true
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
    WHERE w.steps IS NOT NULL
      AND w.ended_at IS NOT NULL
),
workout_energy AS (
    SELECT
        CASE
            WHEN a.raw->>'start_date_local' IS NOT NULL THEN
                left(a.raw->>'start_date_local', 10)::date
            WHEN a.raw->>'text' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
                left(a.raw->>'text', 10)::date
            ELSE (w.started_at AT TIME ZONE 'UTC')::date
        END AS local_date,
        w.calories AS value
    FROM canonical.workout_load w
    JOIN raw.activities a
      ON a.source = w.source AND a.activity_uid = w.activity_uid
    WHERE w.calories IS NOT NULL
      AND w.ended_at IS NOT NULL
),
parts AS (
    SELECT local_date, 'steps'::text AS metric, 'neat'::text AS kind,
           value, 'count'::text AS unit
    FROM neat_steps
    UNION ALL
    SELECT local_date, 'steps', 'workout', value, 'count'
    FROM workout_steps
    UNION ALL
    SELECT local_date, 'active_calories', 'neat', value, 'kcal'
    FROM neat_energy
    UNION ALL
    SELECT local_date, 'active_calories', 'workout', value, 'kcal'
    FROM workout_energy
),
split AS (
    SELECT local_date, metric, kind, sum(value) AS value, unit
    FROM parts
    GROUP BY local_date, metric, kind, unit
)
SELECT local_date, metric, kind, value, unit
FROM split
UNION ALL
SELECT local_date, metric, 'total'::text AS kind, sum(value), unit
FROM split
GROUP BY local_date, metric, unit;

GRANT SELECT ON canonical.activity_daily TO anduin_ro;

-- Materialize canonical.activity_daily.
--
-- The 0021 plain view re-evaluated the full history on every query: its
-- doubly-referenced `split` CTE materialized both metrics regardless of the
-- caller's metric/kind filter, and a LATERAL probe re-fetched each NEAT
-- sample's raw payload from raw.samples (~8k index scans) just to read the
-- civil date the samples_with_condition row already carried. At ~400ms per
-- evaluation and up to a dozen evaluations per page, the web UI took seconds
-- per navigation — and the cost grows linearly with history.
--
-- The daily rollup only changes when an ingest lands, so it is materialized
-- here and refreshed at the end of every `anduin extract` run (see
-- db.refresh_activity_daily). The unique index below is what allows those
-- refreshes to run CONCURRENTLY, so web reads never block on one.
DROP VIEW IF EXISTS canonical.activity_daily;

CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.activity_daily AS
WITH parts AS (
    -- NEAT steps, keyed to the source-local calendar date. Same source-local
    -- date resolution as 0021: Google interval payloads retain civilStartTime;
    -- startTime and finally UTC are fallbacks for legacy/incomplete rows.
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
        'steps'::text AS metric, 'neat'::text AS kind, s.value, 'count'::text AS unit
    FROM canonical.samples_with_condition s
    WHERE s.metric = 'steps' AND s.condition = 'outside_recorded_workout'
    UNION ALL
    SELECT
        CASE
            WHEN a.raw->>'start_date_local' IS NOT NULL THEN
                left(a.raw->>'start_date_local', 10)::date
            WHEN a.raw->>'text' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
                left(a.raw->>'text', 10)::date
            ELSE (w.started_at AT TIME ZONE 'UTC')::date
        END,
        'steps', 'workout', w.steps, 'count'
    FROM canonical.workout_steps w
    JOIN raw.activities a
      ON a.source = w.source AND a.activity_uid = w.activity_uid
    WHERE w.steps IS NOT NULL AND w.ended_at IS NOT NULL
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
-- One pass over `parts`: the extra grouping set (without `kind`) emits the
-- neat+workout 'total' rows 0021 computed with a second scan of `split`.
SELECT local_date, metric,
       CASE WHEN GROUPING(kind) = 1 THEN 'total' ELSE kind END AS kind,
       sum(value) AS value, unit
FROM parts
GROUP BY GROUPING SETS ((local_date, metric, kind, unit), (local_date, metric, unit));

-- Required by REFRESH ... CONCURRENTLY; unit is functionally dependent on
-- metric, so (metric, kind, local_date) identifies a row.
CREATE UNIQUE INDEX IF NOT EXISTS activity_daily_metric_kind_date
    ON canonical.activity_daily (metric, kind, local_date);

GRANT SELECT ON canonical.activity_daily TO anduin_ro;

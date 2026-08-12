-- Workout energy: a four-step fallback chain, plus the numbers we didn't pick.
--
-- Two problems with the 0007 definition:
--
-- 1. Strength workouts silently ate energy. Liftosaur records carry a duration,
--    so they set ended_at and therefore carve a window in
--    canonical.samples_with_condition — the Air's active_energy samples inside
--    that window drop out of canonical.neat_energy. But liftosaur stores no
--    summary, so summary->>'calories' is NULL and no workout row was emitted
--    either. The energy fell through both branches of canonical.total_energy:
--    55-180 kcal per session, missing from the daily total entirely.
--
-- 2. The work-derived branch was ~4x low. `icu_joules / 4184` converts joules
--    to kcal of *mechanical work*, not metabolic cost; it omits gross muscular
--    efficiency (~24%). Measured against the calories intervals supplies for
--    the same rides, the old formula came in at 1/3.99 - 1/4.16 of the real
--    number. Dividing by 1000 instead is the standard cycling identity (1 kJ of
--    work ~= 1 kcal burned) and lands within 5% on every ride on file.
--
-- Precedence, highest first:
--   source        the source computed it (intervals). Trust it.
--   work          power meter work, for rides missing calories. Beats a wrist
--                 estimate: it measures the actual mechanical output.
--   met_strength  the MET model below, for lifting.
--   fitbit_window sum of the Air's active_energy samples inside the window.
--
-- The MET model outranks Fitbit for strength because a wrist accelerometer
-- cannot see isometric load and Fitbit cannot tell that a session is
-- resistance training at all. Its numbers here are both low and erratic —
-- 1.56-3.67 kcal/min across structurally identical sessions of the same
-- program. We know the session is lifting, how long it ran, how dense it was,
-- and what the athlete weighs, which is enough for a steadier estimate.
--
-- All candidate values are kept as columns, so the models can be compared on
-- real data before anything is thrown away:
--
--   SELECT started_at, sport, calories, calories_method,
--          calories_met, calories_fitbit
--   FROM canonical.workout_load WHERE calories_met IS NOT NULL;
--
-- calories_work is exposed for the same reason: it is dormant while intervals
-- supplies calories, so publishing it is the only way to notice it drifting.
--
-- Both the Air's active_energy and the MET model are NET of resting metabolism
-- (Health Connect's ActiveCaloriesBurned is energy above BMR; the model
-- subtracts 1 MET), so the two columns are directly comparable and neither
-- double-counts BMR when summed into canonical.activity_daily.
CREATE OR REPLACE VIEW canonical.workout_load AS
WITH fitbit_energy AS (
    -- Sum of in-window active_energy. Read from raw.samples rather than
    -- canonical.samples: the latter is a DISTINCT ON over the whole sample
    -- table, which this view cannot afford to materialize per evaluation. The
    -- LATERAL keeps the scan inside the workout window via the
    -- (metric, valid_from) index, and DISTINCT ON (valid_from) applies the same
    -- one-row-per-instant rule a re-ingest would otherwise double-count.
    -- google_health is the only source of active_energy (see the single
    -- precedence rule in 0006), so no cross-source resolution is needed.
    SELECT a.source, a.activity_uid, sum(s.value) AS kcal
    FROM raw.activities a
    JOIN LATERAL (
        SELECT DISTINCT ON (r.valid_from) r.value
        FROM raw.samples r
        WHERE r.metric = 'active_energy'
          AND r.valid_from >= a.started_at
          AND r.valid_from <  a.ended_at
        ORDER BY r.valid_from, r.ingested_at DESC
    ) s ON true
    WHERE a.ended_at IS NOT NULL
    GROUP BY a.source, a.activity_uid
),
working_sets AS (
    SELECT e.source, e.activity_uid,
           count(*) FILTER (WHERE NOT s.is_warmup) AS n
    FROM raw.strength_exercises e
    JOIN raw.strength_sets s
      ON s.source = e.source
     AND s.activity_uid = e.activity_uid
     AND s.exercise_uid = e.exercise_uid
    GROUP BY e.source, e.activity_uid
),
base AS (
    SELECT
        a.source,
        a.activity_uid,
        a.started_at,
        a.ended_at,
        a.sport,
        extract(epoch FROM (a.ended_at - a.started_at)) / 3600.0 AS hours,
        ws.n AS working_sets,
        -- Most recent body mass at or before the workout. Withings stores kg.
        (SELECT r.value
           FROM raw.samples r
          WHERE r.metric = 'body_weight'
            AND r.valid_from <= a.started_at
          ORDER BY r.valid_from DESC
          LIMIT 1) AS mass_kg,
        fe.kcal AS calories_fitbit
    FROM raw.activities a
    LEFT JOIN fitbit_energy fe
           ON fe.source = a.source AND fe.activity_uid = a.activity_uid
    LEFT JOIN working_sets ws
           ON ws.source = a.source AND ws.activity_uid = a.activity_uid
),
candidates AS (
    SELECT
        b.*,
        (a.summary->>'icu_training_load')::double precision AS training_load,
        (a.summary->>'calories')::double precision AS calories_source,
        -- 1 kJ of mechanical work ~= 1 kcal expended at ~24% gross efficiency.
        CASE WHEN (b.sport ILIKE '%ride%' OR b.sport ILIKE '%cycl%')
             THEN (a.summary->>'icu_joules')::double precision / 1000.0
        END AS calories_work,
        -- MET model, net of resting metabolism:
        --   kcal = (MET - 1) x 1.05 x mass_kg x hours
        -- MET scales 3.5 (long rests, sparse work) to 6.0 (dense circuit) on
        -- working-set density, the Compendium's range for multi-set resistance
        -- training. Density is clamped to [0.20, 0.50] working sets/min, which
        -- brackets everything from a heavy low-rep day to a dense accessory
        -- session. Requires a duration, a set count and a body mass; without
        -- all three the chain falls through to Fitbit rather than inventing an
        -- input.
        CASE WHEN (b.sport ILIKE '%strength%' OR b.sport ILIKE '%weight%')
              AND b.hours > 0
              AND b.working_sets > 0
              AND b.mass_kg IS NOT NULL
             THEN (
                 3.5 + 2.5 * least(greatest(
                     ((b.working_sets / (b.hours * 60.0)) - 0.20) / 0.30, 0.0), 1.0)
                 - 1.0
             ) * 1.05 * b.mass_kg * b.hours
        END AS calories_met
    FROM base b
    JOIN raw.activities a
      ON a.source = b.source AND a.activity_uid = b.activity_uid
)
SELECT
    source,
    activity_uid,
    started_at,
    ended_at,
    sport,
    training_load,
    COALESCE(calories_source, calories_work, calories_met, calories_fitbit) AS calories,
    calories_source IS NULL
        AND COALESCE(calories_work, calories_met, calories_fitbit) IS NOT NULL
        AS calories_is_derived,
    CASE
        WHEN calories_source  IS NOT NULL THEN 'source'
        WHEN calories_work    IS NOT NULL THEN 'work'
        WHEN calories_met     IS NOT NULL THEN 'met_strength'
        WHEN calories_fitbit  IS NOT NULL THEN 'fitbit_window'
    END AS calories_method,
    calories_met,
    calories_fitbit,
    calories_work
FROM candidates;

GRANT SELECT ON canonical.workout_load TO anduin_ro;

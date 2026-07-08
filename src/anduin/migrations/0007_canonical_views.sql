-- Canonical reconciliation views. Compute per-sample condition (inside vs
-- outside a recorded workout) and pick the winning source via
-- canonical.precedence_rules. No persisted canonical sample tables — views
-- only, so changing precedence rules immediately changes the canonical answer.

-- Per-sample condition tag: does this sample fall inside any recorded workout?
-- Only activities with a non-NULL ended_at define a window (a strength workout
-- with no end owns no continuous-sample window, which is correct).
CREATE OR REPLACE VIEW canonical.samples_with_condition AS
SELECT
    s.*,
    CASE WHEN EXISTS (
        SELECT 1
        FROM raw.activities a
        WHERE a.ended_at IS NOT NULL
          AND s.valid_from >= a.started_at
          AND s.valid_from <  a.ended_at
    ) THEN 'inside_recorded_workout'
      ELSE 'outside_recorded_workout'
    END AS condition
FROM raw.samples s;

-- For each (metric, valid_from) take the row whose precedence rule has the
-- lowest rank, where the rule's (metric, condition, effective window, source,
-- device) matches.
--
-- LEFT JOIN (not INNER): a sample whose (metric, source, device, condition,
-- effective window) matches NO precedence rule must STILL appear here rather
-- than being silently dropped from the canonical layer. Matched rules order
-- ahead of unmatched (r.rank ASC NULLS LAST), so when several raw rows share a
-- (metric, valid_from) the lowest-rank matched one wins; if none match, the
-- lone unmatched row survives with a NULL precedence_rank. DISTINCT ON still
-- yields exactly one row per (metric, valid_from).
CREATE OR REPLACE VIEW canonical.samples AS
SELECT DISTINCT ON (s.metric, s.valid_from)
    s.metric,
    s.valid_from,
    s.valid_to,
    s.source,
    s.device,
    s.recording_method,
    s.value,
    s.unit,
    r.rank AS precedence_rank,
    r.id   AS precedence_rule_id
FROM canonical.samples_with_condition s
LEFT JOIN canonical.precedence_rules r
  ON r.metric = s.metric
 AND (r.condition = s.condition OR r.condition = 'always')
 AND r.source = s.source
 AND (r.device IS NULL OR r.device = s.device)
 AND r.effective_from <= s.valid_from
 AND (r.effective_to IS NULL OR r.effective_to > s.valid_from)
ORDER BY s.metric, s.valid_from, r.rank ASC NULLS LAST, s.ingested_at DESC;

-- Per-metric convenience views (heart_rate, steps, etc.).
CREATE OR REPLACE VIEW canonical.heart_rate AS
SELECT * FROM canonical.samples WHERE metric = 'heart_rate';

CREATE OR REPLACE VIEW canonical.steps AS
SELECT * FROM canonical.samples WHERE metric = 'steps';

CREATE OR REPLACE VIEW canonical.spo2 AS
SELECT * FROM canonical.samples WHERE metric IN ('spo2', 'sleep_spo2');

CREATE OR REPLACE VIEW canonical.hrv AS
SELECT * FROM canonical.samples WHERE metric IN ('hrv', 'sleep_hrv');

CREATE OR REPLACE VIEW canonical.skin_temp AS
SELECT * FROM canonical.samples WHERE metric IN ('skin_temp', 'sleep_skin_temp');

CREATE OR REPLACE VIEW canonical.body_weight AS
SELECT * FROM canonical.samples WHERE metric = 'body_weight';

-- Per-workout energy. Calories come from the source-supplied summary for every
-- sport. The kJ->kcal path (icu_joules / 4184) is ONLY meaningful for cycling
-- (metabolic efficiency ~24% x 4.184 J/cal ~= 1.0, a coincidence, not physics),
-- so it is a cycling-only fallback used when calories is absent, and flagged as
-- derived. Non-cycling with no supplied calories => NULL, never fabricated.
CREATE OR REPLACE VIEW canonical.workout_load AS
SELECT
    source,
    activity_uid,
    started_at,
    ended_at,
    sport,
    (summary->>'icu_training_load')::double precision AS training_load,
    COALESCE(
        (summary->>'calories')::double precision,
        CASE WHEN (sport ILIKE '%ride%' OR sport ILIKE '%cycl%')
             THEN (summary->>'icu_joules')::double precision / 4184.0
        END
    ) AS calories,
    (
        (summary->>'calories') IS NULL
        AND (sport ILIKE '%ride%' OR sport ILIKE '%cycl%')
        AND (summary->>'icu_joules') IS NOT NULL
    ) AS calories_is_derived
FROM raw.activities;

-- Per-workout steps. Source-supplied summary.steps wins; otherwise derived from
-- the cadence stream for FOOT sports only (cycling cadence is crank RPM, not
-- steps). steps = sum(cadence)/60 * stride_factor, where cadence is per-second
-- samples in units/min and stride_factor calibrates per-leg vs. total counting.
-- A lifting workout has no cadence and no supplied steps => NULL.
CREATE OR REPLACE VIEW canonical.workout_steps AS
WITH cadence_steps AS (
    SELECT source, activity_uid, sum(value) / 60.0 AS step_units
    FROM raw.activity_streams
    WHERE metric = 'cadence'
    GROUP BY source, activity_uid
)
SELECT
    a.source,
    a.activity_uid,
    a.started_at,
    a.ended_at,
    a.sport,
    COALESCE(
        (a.summary->>'steps')::double precision,
        CASE WHEN (a.sport ILIKE '%run%' OR a.sport ILIKE '%walk%' OR a.sport ILIKE '%hike%')
             THEN cs.step_units * COALESCE(sf.factor, 1.0)
        END
    ) AS steps,
    (
        (a.summary->>'steps') IS NULL
        AND (a.sport ILIKE '%run%' OR a.sport ILIKE '%walk%' OR a.sport ILIKE '%hike%')
        AND cs.step_units IS NOT NULL
    ) AS steps_is_derived
FROM raw.activities a
LEFT JOIN cadence_steps cs
       ON cs.source = a.source AND cs.activity_uid = a.activity_uid
LEFT JOIN LATERAL (
    SELECT factor FROM canonical.stride_factors f
    WHERE f.source = a.source
      AND (f.sport IS NULL OR f.sport = a.sport)
    ORDER BY (f.sport IS NOT NULL) DESC   -- sport-specific beats source-wide default
    LIMIT 1
) sf ON true;

-- NEAT energy = non-workout active_energy from the Air. Excludes any sample
-- whose timestamp falls inside a recorded workout window.
CREATE OR REPLACE VIEW canonical.neat_energy AS
SELECT s.valid_from, s.valid_to, s.value AS kcal, s.source, s.device
FROM canonical.samples_with_condition s
WHERE s.metric = 'active_energy'
  AND s.condition = 'outside_recorded_workout';

-- NEAT steps = Air step samples OUTSIDE workout windows. (canonical.steps above
-- is the raw resolved metric and still includes workout-time wrist steps, which
-- are junk during cycling/lifting; prefer neat_steps / total_steps.)
CREATE OR REPLACE VIEW canonical.neat_steps AS
SELECT s.valid_from, s.valid_to, s.value AS steps, s.source, s.device
FROM canonical.samples_with_condition s
WHERE s.metric = 'steps'
  AND s.condition = 'outside_recorded_workout';

-- Total energy = NEAT (outside workouts) UNIONed with one calorie contribution
-- per workout (inside them). Sum over any window for the true total with no
-- double-counting; filter on `kind` for just-NEAT or just-workout.
CREATE OR REPLACE VIEW canonical.total_energy AS
SELECT valid_from AS started_at, valid_to AS ended_at, kcal, 'neat' AS kind, source
FROM canonical.neat_energy
UNION ALL
-- Only workouts that define a window (ended_at IS NOT NULL) contribute a
-- workout row. This matches canonical.samples_with_condition's window guard: a
-- NULL-ended workout carves no window, so its in-workout active_energy stays
-- tagged 'outside_recorded_workout' and is already counted via NEAT. Emitting
-- its summary.calories here too would double-count that energy.
SELECT started_at, ended_at, calories AS kcal, 'workout' AS kind, source
FROM canonical.workout_load
WHERE calories IS NOT NULL
  AND ended_at IS NOT NULL;

-- Total steps = NEAT steps UNIONed with per-workout steps. Same pattern.
CREATE OR REPLACE VIEW canonical.total_steps AS
SELECT valid_from AS started_at, valid_to AS ended_at, steps, 'neat' AS kind, source
FROM canonical.neat_steps
UNION ALL
-- Same window invariant as total_energy: only a workout with a non-NULL
-- ended_at carves a window. A NULL-ended workout's in-workout step samples stay
-- tagged 'outside_recorded_workout' and are counted via NEAT steps, so emitting
-- its summary.steps here as well would double-count them.
SELECT started_at, ended_at, steps, 'workout' AS kind, source
FROM canonical.workout_steps
WHERE steps IS NOT NULL
  AND ended_at IS NOT NULL;

-- Reconcile heart rate across the continuous (Air) sample stream and the
-- per-workout activity stream.
--
-- Unlike steps/energy, HR is NOT split into neat/workout/total (see CLAUDE.md):
-- the NEAT distinction is meaningful for volitional movement, not heart rate.
-- Instead canonical.heart_rate is a single unioned timeline in which a workout's
-- own HR stream owns that workout's window, and the continuous stream fills
-- everything else -- including workouts that carry no HR stream.
--
-- This replaces the old passthrough (`SELECT * FROM canonical.samples WHERE
-- metric='heart_rate'`), which was always empty for workout data because of two
-- mismatches:
--   * table: workout HR lives in raw.activity_streams, not raw.samples.
--   * name : streams store metric 'heartrate'; samples/canonical use 'heart_rate'.
--
-- Columns keep valid_from/value so web.queries.metric_series reads it unchanged.
-- The heart_rate rows in canonical.precedence_rules are now inert for the
-- workout/Air split (that reconciliation lives here); they still resolve
-- precedence *among* continuous sample sources inside canonical.samples, which
-- this view's first arm reads.
--
-- DROP + CREATE (not CREATE OR REPLACE): the column list changes from the 0007
-- passthrough, which REPLACE forbids. The drop loses 0009's grant, so it is
-- re-granted at the end.

DROP VIEW IF EXISTS canonical.heart_rate;

CREATE VIEW canonical.heart_rate AS
WITH hr_workouts AS (
    -- Only workout windows that actually carry an HR stream override continuous
    -- HR; a streamless workout must not blank out the Air's coverage.
    SELECT a.source, a.activity_uid, a.started_at, a.ended_at
    FROM raw.activities a
    WHERE a.ended_at IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM raw.activity_streams s
          WHERE s.source = a.source
            AND s.activity_uid = a.activity_uid
            AND s.metric = 'heartrate'
      )
)
-- Continuous (Air) HR, precedence-resolved via canonical.samples, kept only
-- where no HR-bearing workout window covers it.
SELECT
    cs.valid_from,
    cs.valid_to,
    cs.value,
    'bpm'::text            AS unit,
    cs.source,
    cs.device,
    'continuous'::text     AS origin
FROM canonical.samples cs
WHERE cs.metric = 'heart_rate'
  AND NOT EXISTS (
      SELECT 1
      FROM hr_workouts w
      WHERE cs.valid_from >= w.started_at
        AND cs.valid_from <  w.ended_at
  )
UNION ALL
-- Per-second workout HR from the activity stream; owns its window.
SELECT
    st.t                   AS valid_from,
    st.t                   AS valid_to,
    st.value,
    'bpm'::text            AS unit,
    st.source,
    NULL::text             AS device,
    'workout_stream'::text AS origin
FROM raw.activity_streams st
WHERE st.metric = 'heartrate';

GRANT SELECT ON canonical.heart_rate TO anduin_ro;

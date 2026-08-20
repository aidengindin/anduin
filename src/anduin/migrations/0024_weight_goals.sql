-- Weight goals: a phase history the UI can edit, plus the smoothed trend math
-- the verdict and the chart corridor read.
-- See docs/plans/2026-08-19-weight-goal-tracking-design.md.

-- Goal phases. Append-only: the phase in effect on a date is the latest row
-- whose started_on is on or before it, so switching bulk -> cut never rewrites
-- what a past period was aiming at. `none` is a tombstone -- it ends a phase
-- without starting a targeted one ("done bulking, not yet cutting"); the
-- absence of any row means no goal was ever set, which is the default state and
-- needs no seeding.
--
-- target_lb_per_week is SIGNED (negative for a cut) so every comparison
-- downstream is plain arithmetic, and is stored in lb/week rather than kg: it
-- is a number typed in display units, and round-tripping through kg would make
-- 0.4 read back as 0.39999. user_id leads the natural key, as everywhere else.
CREATE TABLE IF NOT EXISTS identity.goals (
    id                  bigserial PRIMARY KEY,
    user_id             int  NOT NULL REFERENCES identity.users(id),
    started_on          date NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('bulk', 'cut', 'maintain', 'none')),
    target_lb_per_week  numeric,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, started_on),
    -- A bulk or cut without a rate is meaningless; a maintain or none with one
    -- is a lie. Enforced here so no write path can get it wrong.
    CHECK ((kind IN ('bulk', 'cut')) = (target_lb_per_week IS NOT NULL))
);

GRANT SELECT ON identity.goals TO anduin_ro;

-- Body-composition trend, extended. The 28d OLS over *raw* readings (0013) is
-- too noisy to act on: day-to-day swings of 1-2 lb sit on top of a bulk signal
-- near 0.3-0.5 lb/wk, so the slope's standard error is comparable to the signal
-- and every weigh-in lurches it. Fitting the *smoothed* series instead is the
-- fix -- the variance is gone before the regression sees it.
--   avg_7d                  trailing 7-day mean (the chart's fast overlay)
--   avg_30d                 trailing 30-day mean (the slow overlay)
--   slope_per_week          trailing 28d OLS on raw values -- the old number,
--                           kept only so the two can be compared on real data
--   smoothed_slope_per_week trailing 28d OLS on avg_7d -- the number the UI shows.
--                           28d beats 21d on accuracy, stability and time to
--                           settle after a real rate change: a shorter window
--                           is too noisy to converge. Measured, not assumed.
--   n_28d                   readings in the 28d window (confidence)
--
-- Note the grain is one row per *reading*, not per day, so two weigh-ins in one
-- day double-weight it. Harmless with Withings' one-a-morning pattern; this is
-- not a properly daily-resampled series and should not be described as one.
CREATE OR REPLACE VIEW derived.body_composition_trend AS
WITH s AS (
    SELECT metric, valid_from, value
    FROM canonical.samples
    WHERE metric IN (
        'body_weight', 'fat_mass', 'fat_free_mass', 'muscle_mass', 'body_fat_ratio'
    )
), smoothed AS (
    SELECT
        metric,
        valid_from,
        value,
        avg(value) OVER w7  AS avg_7d,
        avg(value) OVER w30 AS avg_30d,
        count(*)   OVER w28 AS n_28d,
        regr_slope(value, extract(epoch FROM valid_from)) OVER w28 * 604800.0
            AS slope_per_week
    FROM s
    WINDOW
        w7  AS (PARTITION BY metric ORDER BY valid_from
                RANGE BETWEEN INTERVAL '7 days'  PRECEDING AND CURRENT ROW),
        w30 AS (PARTITION BY metric ORDER BY valid_from
                RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW),
        w28 AS (PARTITION BY metric ORDER BY valid_from
                RANGE BETWEEN INTERVAL '28 days' PRECEDING AND CURRENT ROW)
)
SELECT
    metric,
    valid_from,
    value,
    avg_7d,
    avg_30d,
    n_28d,
    slope_per_week,
    regr_slope(avg_7d, extract(epoch FROM valid_from)) OVER w_smooth * 604800.0
        AS smoothed_slope_per_week
FROM smoothed
WINDOW
    w_smooth AS (PARTITION BY metric ORDER BY valid_from
                 RANGE BETWEEN INTERVAL '28 days' PRECEDING AND CURRENT ROW);

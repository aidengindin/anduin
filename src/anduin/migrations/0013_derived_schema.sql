-- Derived / analytical layer: rolling + baseline-relative metrics on top of the
-- reconciled `canonical` views. Views only, same philosophy as canonical. See
-- docs/plans/2026-07-14-derived-metrics-design.md.

CREATE SCHEMA IF NOT EXISTS derived;

-- Read-only grants, mirroring 0009. ALTER DEFAULT PRIVILEGES first so every view
-- created below (and in later migrations) is readable by anduin_ro automatically.
GRANT USAGE ON SCHEMA derived TO anduin_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA derived GRANT SELECT ON TABLES TO anduin_ro;

-- Body-composition trend. Day-to-day body metrics are noisy (hydration, meal
-- timing); the trailing average is the real signal and the 28d slope is the
-- direction/rate. One row per reading, per metric, as-of that reading.
--   avg_7d          trailing 7-day mean (smooths noise)
--   slope_per_week  trailing 28-day OLS slope in units/week (kg/week, %/week)
--   n_28d           readings in the 28d window (slope needs >= 2; confidence)
CREATE OR REPLACE VIEW derived.body_composition_trend AS
WITH s AS (
    SELECT metric, valid_from, value
    FROM canonical.samples
    WHERE metric IN (
        'body_weight', 'fat_mass', 'fat_free_mass', 'muscle_mass', 'body_fat_ratio'
    )
)
SELECT
    metric,
    valid_from,
    value,
    avg(value) OVER w7 AS avg_7d,
    count(*)   OVER w28 AS n_28d,
    regr_slope(value, extract(epoch FROM valid_from)) OVER w28 * 604800.0
        AS slope_per_week
FROM s
WINDOW
    w7  AS (PARTITION BY metric ORDER BY valid_from
            RANGE BETWEEN INTERVAL '7 days'  PRECEDING AND CURRENT ROW),
    w28 AS (PARTITION BY metric ORDER BY valid_from
            RANGE BETWEEN INTERVAL '28 days' PRECEDING AND CURRENT ROW);

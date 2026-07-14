-- Derived sleep metrics (phase B): efficiency + stage composition.
-- See docs/plans/2026-07-14-derived-metrics-design.md.

-- Expose session_uid on canonical.sleep so consumers (and the composition view
-- below) can fetch the chosen night's stage segments. Appended at the end of the
-- select list so CREATE OR REPLACE accepts the column addition.
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
    session_uid
FROM raw.sleep_sessions
ORDER BY date_trunc('day', started_at),
         is_main_sleep DESC NULLS LAST,
         minutes_asleep DESC NULLS LAST;

-- Sleep efficiency: fraction of time in bed actually asleep. time_in_bed is the
-- session interval; efficiency_pct is ours, source_efficiency is what the device
-- reported (different denominators, kept for comparison).
CREATE OR REPLACE VIEW derived.sleep_efficiency AS
WITH e AS (
    SELECT
        started_at,
        ended_at,
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
        AS efficiency_pct_avg_7d
FROM e;

-- Stage composition: minutes and % per stage for the chosen night. Percentages
-- are over the total staged period (incl. brief awakenings), so deep+rem+light+
-- awake ~= 100. restorative_pct = deep+rem (the recovery fractions).
CREATE OR REPLACE VIEW derived.sleep_stage_composition AS
WITH per_stage AS (
    SELECT
        s.session_uid,
        s.started_at,
        upper(st.stage) AS stage,
        sum(extract(epoch FROM (st.ended_at - st.started_at)) / 60.0) AS minutes
    FROM canonical.sleep s
    JOIN raw.sleep_stages st
      ON st.source = s.source AND st.session_uid = s.session_uid
    GROUP BY s.session_uid, s.started_at, upper(st.stage)
),
agg AS (
    SELECT
        session_uid,
        started_at,
        sum(minutes) AS total_min,
        sum(minutes) FILTER (WHERE stage = 'DEEP')             AS deep_min,
        sum(minutes) FILTER (WHERE stage = 'REM')              AS rem_min,
        sum(minutes) FILTER (WHERE stage = 'LIGHT')            AS light_min,
        sum(minutes) FILTER (WHERE stage IN ('AWAKE', 'WAKE')) AS awake_min
    FROM per_stage
    GROUP BY session_uid, started_at
)
SELECT
    session_uid,
    started_at,
    total_min,
    deep_min, rem_min, light_min, awake_min,
    100.0 * deep_min  / NULLIF(total_min, 0) AS pct_deep,
    100.0 * rem_min   / NULLIF(total_min, 0) AS pct_rem,
    100.0 * light_min / NULLIF(total_min, 0) AS pct_light,
    100.0 * awake_min / NULLIF(total_min, 0) AS pct_awake,
    100.0 * (COALESCE(deep_min, 0) + COALESCE(rem_min, 0))
          / NULLIF(total_min, 0) AS restorative_pct,
    avg(100.0 * (COALESCE(deep_min, 0) + COALESCE(rem_min, 0))
              / NULLIF(total_min, 0))
        OVER (ORDER BY started_at
              RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW)
        AS restorative_pct_avg_7d
FROM agg;

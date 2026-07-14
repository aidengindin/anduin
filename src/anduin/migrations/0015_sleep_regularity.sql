-- Sleep regularity (phase C): the Sleep Regularity Index (Phillips et al. 2017).
-- See docs/plans/2026-07-14-derived-metrics-design.md.

-- Local offset for each session, so SRI aligns clock time across nights. NULL
-- when the source timestamp gave no offset (SRI then falls back to UTC).
ALTER TABLE raw.sleep_sessions ADD COLUMN IF NOT EXISTS tz_offset_minutes integer;

-- Per-minute asleep/awake state in LOCAL wall-clock time across the recording.
-- Asleep = the minute lies inside a non-AWAKE stage segment; every other minute
-- (daytime, gaps) is awake -- the standard SRI assumption.
--
-- Built by expanding asleep segments to their minutes and left-joining a dense
-- grid on minute equality, rather than testing each grid minute against every
-- segment (which is quadratic). If the recording ever grows large enough for
-- this view to drag, promote it to a daily-refreshed materialized view.
CREATE OR REPLACE VIEW derived._sleep_minute_state AS
WITH seg AS (
    SELECT
        (st.started_at AT TIME ZONE 'UTC')
            + make_interval(mins => COALESCE(ss.tz_offset_minutes, 0)) AS s,
        (st.ended_at   AT TIME ZONE 'UTC')
            + make_interval(mins => COALESCE(ss.tz_offset_minutes, 0)) AS e
    FROM raw.sleep_stages st
    JOIN raw.sleep_sessions ss
      ON ss.source = st.source AND ss.session_uid = st.session_uid
    WHERE upper(st.stage) NOT IN ('AWAKE', 'WAKE')
),
asleep_minutes AS (
    SELECT DISTINCT gs AS m
    FROM seg
    CROSS JOIN LATERAL generate_series(
        date_trunc('minute', seg.s), seg.e - interval '1 minute', interval '1 minute'
    ) AS gs
),
bounds AS (SELECT min(m) AS lo, max(m) AS hi FROM asleep_minutes),
grid AS (
    SELECT generate_series(lo, hi, interval '1 minute') AS m
    FROM bounds WHERE lo IS NOT NULL
)
SELECT g.m, (am.m IS NOT NULL) AS asleep
FROM grid g
LEFT JOIN asleep_minutes am ON am.m = g.m;

-- Rolling Sleep Regularity Index per as-of date over a trailing 14-night window:
-- the fraction of minutes in the same state as the same clock-minute 24h earlier,
-- rescaled to [-100, +100]. +100 = identical schedule every night, 0 = random,
-- negative = anti-phase. Requires >= 7 nights of data in the window else NULL.
CREATE OR REPLACE VIEW derived.sleep_regularity AS
WITH st AS (SELECT m, asleep FROM derived._sleep_minute_state),
paired AS (
    SELECT s.m, (s.asleep = p.asleep) AS agree
    FROM st s
    JOIN st p ON p.m = s.m - interval '24 hours'
),
days AS (SELECT DISTINCT date_trunc('day', m)::date AS d FROM st)
SELECT
    days.d AS as_of_date,
    count(*) AS epoch_pairs,
    count(DISTINCT date_trunc('day', pr.m)::date) AS nights,
    CASE WHEN count(DISTINCT date_trunc('day', pr.m)::date) >= 7
         THEN -100.0 + 200.0 * avg(pr.agree::int)
    END AS sri
FROM days
JOIN paired pr
  ON pr.m >= (days.d - interval '13 days')
 AND pr.m <  (days.d + interval '1 day')
GROUP BY days.d;

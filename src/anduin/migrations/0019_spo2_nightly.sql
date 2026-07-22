-- SpO2, same class of fix as HRV (migration 0018): Google's daily SpO2 rollup is
-- empty for the Fitbit Air, but the per-sample oxygen-saturation stream is
-- populated (measured only during sleep). Derive a nightly avg/min/max from those
-- samples, keyed to the local wake date, and repoint spo2_status at it.

DROP VIEW IF EXISTS canonical.spo2_daily;
CREATE VIEW canonical.spo2_daily AS
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
 -- Fitbit emits a 50.0 sentinel for dropped/invalid pulse-ox readings; drop
 -- anything below a physiological floor so it can't corrupt avg/min or trip the
 -- desaturation flag.
 AND o.value >= 70
GROUP BY s.session_uid, local_date, s.source;

-- Overnight desaturation status, now sourced from the nightly sample view: an
-- absolute low (min < 90%) OR a personal deviation (avg below the 30-day band).
CREATE OR REPLACE VIEW derived.spo2_status AS
WITH j AS (
    SELECT local_date, spo2_min, spo2_avg FROM canonical.spo2_daily
),
w AS (
    SELECT local_date, spo2_min, spo2_avg,
        avg(spo2_avg)         OVER w30 AS base_mean,
        stddev_samp(spo2_avg) OVER w30 AS base_sd,
        count(spo2_avg)       OVER w30 AS base_n
    FROM j
    WINDOW w30 AS (ORDER BY local_date RANGE BETWEEN INTERVAL '29 days' PRECEDING AND CURRENT ROW)
)
SELECT local_date, spo2_min, spo2_avg, base_mean, base_sd, base_n,
    (spo2_min < 90) AS low_absolute,
    (base_n >= 14 AND base_sd IS NOT NULL AND spo2_avg < base_mean - base_sd) AS low_relative,
    (spo2_min < 90
        OR (base_n >= 14 AND base_sd IS NOT NULL AND spo2_avg < base_mean - base_sd)) AS flagged
FROM w;

GRANT SELECT ON canonical.spo2_daily TO anduin_ro;

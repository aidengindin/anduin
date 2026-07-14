-- Baseline-relative daily status metrics (phase D): HRV status, RHR trend,
-- respiratory-rate and SpO2 flags. Each compares a short-term value to the
-- wearer's own trailing baseline (mean +/- 1 SD). See the design doc.

-- HRV status (Garmin-style): 7-day average vs a 60-day personal baseline band.
CREATE OR REPLACE VIEW derived.hrv_status AS
WITH d AS (
    SELECT local_date, value FROM canonical.daily_metrics WHERE metric = 'hrv_daily_rmssd'
),
w AS (
    SELECT local_date, value,
        avg(value)         OVER w7  AS hrv_7d,
        avg(value)         OVER w60 AS base_mean,
        stddev_samp(value) OVER w60 AS base_sd,
        count(*)           OVER w60 AS base_n
    FROM d
    WINDOW
        w7  AS (ORDER BY local_date RANGE BETWEEN INTERVAL '6 days'  PRECEDING AND CURRENT ROW),
        w60 AS (ORDER BY local_date RANGE BETWEEN INTERVAL '59 days' PRECEDING AND CURRENT ROW)
)
SELECT local_date, value, hrv_7d, base_mean, base_sd, base_n,
    base_mean - base_sd AS low_thresh,
    base_mean + base_sd AS high_thresh,
    CASE
        WHEN base_n < 14 OR base_sd IS NULL     THEN 'pending'
        WHEN hrv_7d < base_mean - base_sd       THEN 'low'
        WHEN hrv_7d > base_mean + base_sd       THEN 'high'
        ELSE 'balanced'
    END AS status
FROM w;

-- Resting HR trend: 7-day average vs 60-day baseline. Only elevation is a
-- concern (fatigue / illness / overtraining), so a low reading stays 'normal'.
CREATE OR REPLACE VIEW derived.rhr_status AS
WITH d AS (
    SELECT local_date, value FROM canonical.daily_metrics WHERE metric = 'resting_heart_rate'
),
w AS (
    SELECT local_date, value,
        avg(value)         OVER w7  AS rhr_7d,
        avg(value)         OVER w60 AS base_mean,
        stddev_samp(value) OVER w60 AS base_sd,
        count(*)           OVER w60 AS base_n
    FROM d
    WINDOW
        w7  AS (ORDER BY local_date RANGE BETWEEN INTERVAL '6 days'  PRECEDING AND CURRENT ROW),
        w60 AS (ORDER BY local_date RANGE BETWEEN INTERVAL '59 days' PRECEDING AND CURRENT ROW)
)
SELECT local_date, value, rhr_7d, base_mean, base_sd, base_n,
    base_mean + base_sd AS elevated_thresh,
    CASE
        WHEN base_n < 14 OR base_sd IS NULL   THEN 'pending'
        WHEN rhr_7d > base_mean + base_sd     THEN 'elevated'
        ELSE 'normal'
    END AS status
FROM w;

-- Respiratory-rate elevation: today vs a 30-day baseline. Elevated overnight RR
-- is an early illness signal.
CREATE OR REPLACE VIEW derived.respiratory_rate_status AS
WITH d AS (
    SELECT local_date, value FROM canonical.daily_metrics WHERE metric = 'respiratory_rate'
),
w AS (
    SELECT local_date, value,
        avg(value)         OVER w30 AS base_mean,
        stddev_samp(value) OVER w30 AS base_sd,
        count(*)           OVER w30 AS base_n
    FROM d
    WINDOW w30 AS (ORDER BY local_date RANGE BETWEEN INTERVAL '29 days' PRECEDING AND CURRENT ROW)
)
SELECT local_date, value, base_mean, base_sd, base_n,
    (base_n >= 14 AND base_sd IS NOT NULL AND value > base_mean + base_sd) AS elevated
FROM w;

-- Overnight SpO2 desaturation: an absolute low (min < 90%) OR a personal
-- deviation (avg below the 30-day baseline band). Joins the daily min + avg.
CREATE OR REPLACE VIEW derived.spo2_status AS
WITH mn  AS (SELECT local_date, value AS spo2_min FROM canonical.daily_metrics WHERE metric = 'spo2_daily_min'),
     av  AS (SELECT local_date, value AS spo2_avg FROM canonical.daily_metrics WHERE metric = 'spo2_daily_avg'),
     j   AS (
        SELECT COALESCE(mn.local_date, av.local_date) AS local_date, spo2_min, spo2_avg
        FROM mn FULL JOIN av USING (local_date)
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

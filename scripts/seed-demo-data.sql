-- Synthetic device data for local UI development (there is no Fitbit Air yet, so
-- sleep / HRV / RHR / respiratory / SpO2 / steps are otherwise empty). Withings
-- (weight, body comp, BP), intervals (workouts, streams, PMC) and Liftosaur are
-- real -- this only fills the device-backed gaps. Run via: dev-db.sh seed.
-- Idempotent: keyed rows upsert; safe to re-run. Anchored at 2026-07-14.

-- 45 nights of sleep, one main session each, with a repeating hypnogram.
INSERT INTO raw.sleep_sessions
    (source, session_uid, device, recording_method, started_at, ended_at,
     tz_offset_minutes, is_main_sleep, sleep_type, minutes_asleep, minutes_awake,
     minutes_in_sleep_period, efficiency, summary, raw, natural_key)
SELECT 'google_health', 'demo-sleep-'||d,
       'fitbit_air', 'device',
       (d + time '23:00') + make_interval(mins => ((g*17) % 41) - 20),
       (d + time '23:00') + make_interval(mins => ((g*17) % 41) - 20) + interval '8 hours',
       -240, true, 'STAGES',
       400 + ((g*11) % 80), 25 + ((g*7) % 20), 480, 84 + ((g*13) % 13),
       '{}', '{}', 'sleep|demo-sleep-'||d
FROM generate_series(0,44) g, LATERAL (SELECT (DATE '2026-07-14' - g) AS d) x
ON CONFLICT (source, session_uid) DO NOTHING;

-- Hypnogram: a fixed per-night pattern of stage segments (minutes from bedtime).
INSERT INTO raw.sleep_stages (source, session_uid, stage, started_at, ended_at)
SELECT 'google_health', 'demo-sleep-'||d, p.stage,
       base + make_interval(mins => p.s), base + make_interval(mins => p.e)
FROM generate_series(0,44) g,
     LATERAL (SELECT (DATE '2026-07-14' - g) AS d) x,
     LATERAL (SELECT ((DATE '2026-07-14' - g) + time '23:00')
                     + make_interval(mins => ((g*17) % 41) - 20) AS base) b,
     (VALUES ('LIGHT',0,40),('DEEP',40,100),('LIGHT',100,180),('REM',180,225),
             ('LIGHT',225,300),('DEEP',300,340),('LIGHT',340,420),('REM',420,460),
             ('AWAKE',460,470),('LIGHT',470,480)) AS p(stage,s,e)
ON CONFLICT (source, session_uid, started_at) DO NOTHING;

-- Daily scalar metrics (HRV, RHR, respiratory rate, SpO2 avg/min).
INSERT INTO raw.daily_metrics (source, device, recording_method, metric, value, unit, local_date, tz_offset_minutes, natural_key, raw)
SELECT 'google_health','fitbit_air','device', m.metric, m.value, m.unit, d, -240, m.metric||'|'||d, '{}'
FROM generate_series(0,44) g,
     LATERAL (SELECT (DATE '2026-07-14' - g) AS d) x,
     LATERAL (VALUES
        ('hrv_daily_rmssd',    (45 + ((g*7) % 13) - 6)::float,  'ms'),
        ('resting_heart_rate', (52 + ((g*5) % 7) - 3)::float,   'bpm'),
        ('respiratory_rate',   (14 + ((g*3) % 3) * 0.5)::float, 'br/min'),
        ('spo2_daily_avg',     (97 - ((g*2) % 2))::float,       '%'),
        ('spo2_daily_min',     (93 - ((g*3) % 4))::float,       '%')
     ) AS m(metric,value,unit)
ON CONFLICT (source, metric, local_date) DO NOTHING;

-- Daily step totals + skin temperature, as continuous samples (one/day).
INSERT INTO raw.samples (source, device, recording_method, metric, value, unit, valid_from, valid_to, natural_key, raw)
SELECT 'google_health','fitbit_air','device', s.metric, s.value, s.unit,
       (d + time '12:00'), (d + time '12:00'), s.metric||'|demo|'||d, '{}'
FROM generate_series(0,44) g,
     LATERAL (SELECT (DATE '2026-07-14' - g) AS d) x,
     LATERAL (VALUES
        ('steps',     (8000 + ((g*137) % 4200))::float, 'count'),
        ('skin_temp', (33.4 + ((g*3) % 9) * 0.1)::float, 'C')
     ) AS s(metric,value,unit)
ON CONFLICT (source, metric, natural_key, valid_from) DO NOTHING;

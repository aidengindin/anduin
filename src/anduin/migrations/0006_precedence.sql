-- Precedence rules. Append-only: superseding a rule = new row with new
-- effective_from and setting the prior row's effective_to. Lower rank wins.
--
-- `condition`:
--   'inside_recorded_workout'  : sample falls inside any raw.activities window
--   'outside_recorded_workout' : sample falls outside every workout window
--   'always'                   : matches regardless

CREATE TABLE IF NOT EXISTS canonical.precedence_rules (
    id              bigserial   PRIMARY KEY,
    metric          text        NOT NULL,
    condition       text        NOT NULL CHECK (
        condition IN ('inside_recorded_workout', 'outside_recorded_workout', 'always')
    ),
    source          text        NOT NULL,
    device          text,
    rank            int         NOT NULL,
    effective_from  timestamptz NOT NULL,
    effective_to    timestamptz,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS precedence_lookup
    ON canonical.precedence_rules (metric, condition, effective_from DESC);

-- Seed: documented policy from the project brief.
--   Inside a recorded workout: activity-stream source (intervals) wins for HR.
--   Outside: the Fitbit Air wins.
-- Add steps / active_energy / spo2 / hrv / skin_temp with sensible defaults.

INSERT INTO canonical.precedence_rules
    (metric, condition, source, device, rank, effective_from, notes)
VALUES
    ('heart_rate', 'inside_recorded_workout', 'intervals',     NULL,         10,  '-infinity', 'workout stream wins'),
    ('heart_rate', 'inside_recorded_workout', 'google_health', 'fitbit_air', 90,  '-infinity', 'air fallback'),
    ('heart_rate', 'outside_recorded_workout','google_health', 'fitbit_air', 10,  '-infinity', 'air owns waking HR'),
    ('heart_rate', 'outside_recorded_workout','intervals',     NULL,         90,  '-infinity', 'fallback'),

    ('steps',          'always', 'google_health', 'fitbit_air', 10, '-infinity', 'air owns steps'),
    ('distance',       'always', 'google_health', 'fitbit_air', 10, '-infinity', 'air owns daily distance; activity streams handled separately'),
    ('active_energy',  'always', 'google_health', 'fitbit_air', 10, '-infinity', 'NEAT lives in raw.samples; workout load via canonical.workout_load view'),
    ('spo2',           'always', 'google_health', 'fitbit_air', 10, '-infinity', 'air'),
    ('hrv',            'always', 'google_health', 'fitbit_air', 10, '-infinity', 'sleep-bound on the air'),
    ('skin_temp',      'always', 'google_health', 'fitbit_air', 10, '-infinity', 'sleep-bound on the air'),
    ('sleep_spo2',     'always', 'google_health', 'fitbit_air', 10, '-infinity', 'sleep-bound metric'),
    ('sleep_hrv',      'always', 'google_health', 'fitbit_air', 10, '-infinity', 'sleep-bound metric'),
    ('sleep_skin_temp','always', 'google_health', 'fitbit_air', 10, '-infinity', 'sleep-bound metric'),

    ('body_weight',    'always', 'withings',      NULL,         10, '-infinity', 'withings scale owns body weight'),

    -- Withings owns body composition + blood pressure (sole source for these).
    ('body_fat_ratio',            'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('fat_mass',                  'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('fat_free_mass',             'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('muscle_mass',               'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('bone_mass',                 'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('hydration',                 'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('visceral_fat',              'always', 'withings', NULL, 10, '-infinity', 'withings scale'),
    ('blood_pressure_systolic',   'always', 'withings', NULL, 10, '-infinity', 'withings BP monitor'),
    ('blood_pressure_diastolic',  'always', 'withings', NULL, 10, '-infinity', 'withings BP monitor')
ON CONFLICT DO NOTHING;

-- Stride factors. Deriving in-workout steps from a cadence stream requires
-- knowing the cadence units, which differ across sources: total steps/min
-- (~170-180) vs. strides/min per leg (~85-90), off by exactly 2x. Rather than
-- hardcode, canonical.workout_steps (see 0007) multiplies the cadence integral
-- by a calibratable factor looked up here.
--
-- Match is (source, sport); a NULL sport applies to ALL foot sports of that
-- source. Sport-specific rows win over the source-wide default. Absent match =>
-- factor 1.0.
CREATE TABLE IF NOT EXISTS canonical.stride_factors (
    source   text             NOT NULL,
    sport    text,             -- NULL = all foot sports for this source
    factor   double precision NOT NULL DEFAULT 1.0,
    notes    text,
    created_at timestamptz    NOT NULL DEFAULT now()
);

-- (source, sport) unique, treating NULL sport as the source-wide default.
CREATE UNIQUE INDEX IF NOT EXISTS stride_factors_uk
    ON canonical.stride_factors (source, COALESCE(sport, ''));

-- Today every non-Liftosaur workout flows through intervals.icu, which reports
-- running cadence per-leg (strides/min) -> x2 to get steps. CONFIRMED against
-- real data: run cadence streams read ~83/s (per-leg, not ~166), and
-- cadence*time*2 matches both distance/stride and the speed=2*cadence*stride
-- identity (factor 1 would be off by exactly 2x).
INSERT INTO canonical.stride_factors (source, sport, factor, notes)
VALUES ('intervals', NULL, 2.0, 'intervals.icu reports cadence per-leg; x2 -> steps (confirmed vs distance/stride)')
ON CONFLICT DO NOTHING;

-- Structured strength: exercise -> set hierarchy hanging off raw.activities.
-- The workout header lives in raw.activities (unified with cardio); there is no
-- separate strength_workouts table. A strength activity is just a row in
-- raw.activities with sport = 'strength' (and possibly streams too, if the
-- source records HR/etc. per-second). Liftosaur is the only source today;
-- `source` is kept for future-proofing.

CREATE TABLE IF NOT EXISTS raw.strength_exercises (
    source        text         NOT NULL,
    activity_uid  text         NOT NULL,
    exercise_uid  text         NOT NULL,
    exercise_name text         NOT NULL,
    exercise_idx  int          NOT NULL,
    is_unilateral boolean      NOT NULL DEFAULT false,
    raw           jsonb        NOT NULL,
    ingested_at   timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (source, activity_uid, exercise_uid),
    FOREIGN KEY (source, activity_uid) REFERENCES raw.activities (source, activity_uid)
        ON DELETE CASCADE
);

-- Sets. For unilateral exercises Liftosaur records both sides; its notation is
-- `right|left` (e.g. `8|7` = 8 right, 7 left). `reps` holds the primary/first
-- (right) side and `left_reps` the optional left side. A bilateral set leaves
-- `left_reps` NULL and puts the single value in `reps`.
CREATE TABLE IF NOT EXISTS raw.strength_sets (
    source        text             NOT NULL,
    activity_uid  text             NOT NULL,
    exercise_uid  text             NOT NULL,
    set_index     int              NOT NULL,
    completed_at  timestamptz,
    weight_kg     double precision,
    reps          int,
    left_reps     int,
    rpe           double precision,
    is_warmup     boolean          NOT NULL DEFAULT false,
    raw           jsonb            NOT NULL,
    ingested_at   timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (source, activity_uid, exercise_uid, set_index),
    FOREIGN KEY (source, activity_uid, exercise_uid)
        REFERENCES raw.strength_exercises (source, activity_uid, exercise_uid)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS strength_sets_completed
    ON raw.strength_sets (completed_at DESC);

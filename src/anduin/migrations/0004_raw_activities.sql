-- Unified activities header + per-second streams.
-- One activity = ONE workout as recorded by ONE source, regardless of modality
-- (cardio, strength, or both). Streams hold the per-second metric series;
-- strength exercises/sets (see 0005) hang off the same header. A Garmin
-- strength session carries both streams and sets under one row; a Liftosaur
-- session carries sets only. Cross-source overlap is resolved in the canonical
-- layer, not merged here.
--
-- Used to define "recorded workout windows" for reconciliation. `ended_at` is
-- nullable (a strength workout may lack an end); windows are only defined by
-- activities with a non-NULL `ended_at` (see 0007).

CREATE TABLE IF NOT EXISTS raw.activities (
    user_id           int          NOT NULL REFERENCES identity.users(id),
    source            text         NOT NULL,
    activity_uid      text         NOT NULL,
    device            text,
    recording_method  text,
    sport             text,                    -- 'strength' is just another value
    program           text,                    -- strength program name; nullable
    started_at        timestamptz  NOT NULL,
    ended_at          timestamptz,             -- nullable; strength may lack an end
    summary           jsonb,                   -- nullable; source-supplied rollups
    raw               jsonb        NOT NULL,
    natural_key       text         NOT NULL,
    ingested_at       timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, source, activity_uid),
    CONSTRAINT activities_valid_range CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS activities_time
    ON raw.activities (started_at DESC);

DROP TRIGGER IF EXISTS activities_restatement ON raw.activities;
CREATE TRIGGER activities_restatement
    BEFORE UPDATE ON raw.activities
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();

CREATE TABLE IF NOT EXISTS raw.activity_streams (
    user_id       int              NOT NULL REFERENCES identity.users(id),
    source        text             NOT NULL,
    activity_uid  text             NOT NULL,
    t             timestamptz      NOT NULL,
    metric        text             NOT NULL,
    value         double precision NOT NULL,
    ingested_at   timestamptz      NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'raw.activity_streams',
    't',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS activity_streams_uk
    ON raw.activity_streams (user_id, source, activity_uid, metric, t);

CREATE INDEX IF NOT EXISTS activity_streams_metric_time
    ON raw.activity_streams (metric, t DESC);

ALTER TABLE raw.activity_streams SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'user_id, source, activity_uid, metric',
    timescaledb.compress_orderby = 't DESC'
);

SELECT add_compression_policy('raw.activity_streams', INTERVAL '14 days', if_not_exists => TRUE);

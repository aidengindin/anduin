-- Sleep is a session, not a scalar sample: one row = one sleep session from one
-- source, with a header + summary here and the stage segments in the child
-- table below. Mirrors the raw.activities / raw.activity_streams split.
--
-- The v4 Google Health `sleep` data type gives an interval, a STAGES|CLASSIC
-- type, a per-stage segment list (DEEP/LIGHT/REM/AWAKE), and a summary of minute
-- totals plus efficiency. Fitbit's proprietary 0-100 sleep score is NOT exposed
-- by the API, so it is intentionally absent.

CREATE TABLE IF NOT EXISTS raw.sleep_sessions (
    user_id                 int         NOT NULL REFERENCES identity.users(id),
    source                  text        NOT NULL,
    session_uid             text        NOT NULL,   -- v4 dataPoint name / logId
    device                  text,
    recording_method        text,
    started_at              timestamptz NOT NULL,   -- interval start
    ended_at                timestamptz NOT NULL,   -- interval end
    is_main_sleep           boolean,
    sleep_type              text,                   -- STAGES | CLASSIC
    minutes_asleep          integer,
    minutes_awake           integer,
    minutes_in_sleep_period integer,
    minutes_to_fall_asleep  integer,
    minutes_after_wakeup    integer,
    efficiency              double precision,
    summary                 jsonb,                  -- stages_summary + extras
    raw                     jsonb       NOT NULL,
    natural_key             text        NOT NULL,   -- for the restatement trigger
    ingested_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, source, session_uid),
    CONSTRAINT sleep_sessions_valid_range CHECK (ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS sleep_sessions_time
    ON raw.sleep_sessions (started_at DESC);

-- Reuse the generic restatement audit trigger (needs natural_key + raw).
DROP TRIGGER IF EXISTS sleep_sessions_restatement ON raw.sleep_sessions;
CREATE TRIGGER sleep_sessions_restatement
    BEFORE UPDATE ON raw.sleep_sessions
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();

-- Stage segments. One row = one contiguous stage span within a session. Modelled
-- like raw.activity_streams: a hypertable on the interval start, compressed.
CREATE TABLE IF NOT EXISTS raw.sleep_stages (
    user_id      int         NOT NULL REFERENCES identity.users(id),
    source       text        NOT NULL,
    session_uid  text        NOT NULL,
    stage        text        NOT NULL,   -- DEEP | LIGHT | REM | AWAKE
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sleep_stages_valid_range CHECK (ended_at >= started_at)
);

SELECT create_hypertable(
    'raw.sleep_stages',
    'started_at',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS sleep_stages_uk
    ON raw.sleep_stages (user_id, source, session_uid, started_at);

ALTER TABLE raw.sleep_stages SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'user_id, source, session_uid',
    timescaledb.compress_orderby = 'started_at DESC'
);

SELECT add_compression_policy('raw.sleep_stages', INTERVAL '30 days', if_not_exists => TRUE);

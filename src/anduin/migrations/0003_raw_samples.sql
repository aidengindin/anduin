-- Tall timeseries of continuous health-metric samples.
-- One row = one immutable observation as received from a source.
-- Restatement = upsert on (source, metric, natural_key); shadow audit table
-- captures payload diffs.

CREATE TABLE IF NOT EXISTS raw.samples (
    source            text          NOT NULL,
    device            text,
    recording_method  text,
    metric            text          NOT NULL,
    value             double precision NOT NULL,
    unit              text,
    valid_from        timestamptz   NOT NULL,
    valid_to          timestamptz   NOT NULL,
    natural_key       text          NOT NULL,
    raw               jsonb         NOT NULL,
    ingested_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT samples_valid_range CHECK (valid_to >= valid_from)
);

SELECT create_hypertable(
    'raw.samples',
    'valid_from',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Idempotency: re-pulls land on the same row.
CREATE UNIQUE INDEX IF NOT EXISTS samples_natural_uk
    ON raw.samples (source, metric, natural_key, valid_from);

CREATE INDEX IF NOT EXISTS samples_metric_time
    ON raw.samples (metric, valid_from DESC);

CREATE TABLE IF NOT EXISTS raw.restatements (
    id          bigserial PRIMARY KEY,
    table_name  text        NOT NULL,
    natural_key text        NOT NULL,
    old_raw     jsonb       NOT NULL,
    new_raw     jsonb       NOT NULL,
    restated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS restatements_table_time
    ON raw.restatements (table_name, restated_at DESC);

CREATE OR REPLACE FUNCTION raw.log_restatement() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.raw IS DISTINCT FROM OLD.raw THEN
        INSERT INTO raw.restatements (table_name, natural_key, old_raw, new_raw)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, NEW.natural_key, OLD.raw, NEW.raw);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS samples_restatement ON raw.samples;
CREATE TRIGGER samples_restatement
    BEFORE UPDATE ON raw.samples
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();

-- Compression for older chunks. ~0.6 GB/yr expected.
ALTER TABLE raw.samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source, metric',
    timescaledb.compress_orderby = 'valid_from DESC'
);

SELECT add_compression_policy('raw.samples', INTERVAL '14 days', if_not_exists => TRUE);


-- Daily summaries are a distinct grain from the continuous sample stream above:
-- one row = one source's summary for one metric on one LOCAL calendar day. They
-- are keyed on local_date (the source's own local date, taken verbatim from the
-- upstream daily message) rather than a UTC timestamp, so travel across time
-- zones can never collide two "daily" rows into one UTC day nor split one across
-- two. tz_offset_minutes records the offset when the source supplies it.
--
-- No hypertable: one row per metric per day is tiny (same reasoning as
-- raw.sleep_sessions), and a plain PK gives idempotent re-pulls directly.
CREATE TABLE IF NOT EXISTS raw.daily_metrics (
    source            text             NOT NULL,
    device            text,
    recording_method  text,
    metric            text             NOT NULL,
    value             double precision NOT NULL,
    unit              text,
    local_date        date             NOT NULL,
    tz_offset_minutes integer,
    raw               jsonb            NOT NULL,
    natural_key       text             NOT NULL,
    ingested_at       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (source, metric, local_date)
);

CREATE INDEX IF NOT EXISTS daily_metrics_metric_date
    ON raw.daily_metrics (metric, local_date DESC);

-- Same generic restatement audit as raw.samples (needs natural_key + raw).
DROP TRIGGER IF EXISTS daily_metrics_restatement ON raw.daily_metrics;
CREATE TRIGGER daily_metrics_restatement
    BEFORE UPDATE ON raw.daily_metrics
    FOR EACH ROW EXECUTE FUNCTION raw.log_restatement();

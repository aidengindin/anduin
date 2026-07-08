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

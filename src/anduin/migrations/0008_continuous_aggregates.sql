-- Continuous aggregates over raw.samples. The canonical layer is computed via
-- views; CAGGs are rollups of the raw layer for cheap range queries by the
-- future analyzer. The analyzer can join CAGG buckets against precedence rules
-- at read time if it needs canonical-resolved rollups.

CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.samples_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', valid_from) AS bucket,
    source,
    metric,
    avg(value)  AS avg_value,
    min(value)  AS min_value,
    max(value)  AS max_value,
    count(*)    AS n
FROM raw.samples
GROUP BY bucket, source, metric
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'canonical.samples_1m',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists => TRUE
);

CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.samples_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', valid_from) AS bucket,
    source,
    metric,
    avg(value) AS avg_value,
    min(value) AS min_value,
    max(value) AS max_value,
    sum(value) AS sum_value,
    count(*)   AS n
FROM raw.samples
GROUP BY bucket, source, metric
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'canonical.samples_1h',
    start_offset => INTERVAL '30 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.samples_1d
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', valid_from) AS bucket,
    source,
    metric,
    avg(value) AS avg_value,
    min(value) AS min_value,
    max(value) AS max_value,
    sum(value) AS sum_value,
    count(*)   AS n
FROM raw.samples
GROUP BY bucket, source, metric
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'canonical.samples_1d',
    start_offset => INTERVAL '365 days',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

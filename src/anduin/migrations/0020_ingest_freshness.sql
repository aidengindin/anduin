-- Ingest freshness for monitoring. Exposes, per source, the most recent
-- ingested_at across all raw tables, plus the lag since. The web app serves this
-- to Prometheus at /-/metrics; Grafana alerts on the lag.
--
-- This is the piece that complements the systemd-unit-failed alert already in
-- the monitoring stack. Because the extract CLI exits non-zero on error, a failed
-- pull turns its oneshot unit `failed` and that alert fires -- covering the
-- "ran and errored" case. This view covers the two cases that miss it:
--   * the timer silently stopped firing (nothing ran, unit never failed), and
--   * a pull ran clean but the upstream API returned nothing (no rows landed).
-- In both, no new rows arrive, so the source's lag grows and trips the alert.
--
-- Scan cost: the three compressed hypertables (samples, activity_streams,
-- sleep_stages) are bounded to recent chunks via their partition column, so the
-- max() stays a cheap recent-chunk scan as history grows -- a source stale beyond
-- the window contributes nothing here, which only makes its lag larger (exactly
-- the signal we want). The remaining tables are small plain tables, scanned whole.
--
-- Note on cadence: lag is meaningful-as-health only for sources that should
-- produce data every day (google_health; intervals wellness). withings/liftosaur
-- lag just reflects when you last weighed in / lifted, so alert on those sources'
-- *run* success (the systemd alert), not on this lag.
CREATE OR REPLACE VIEW derived.ingest_freshness AS
WITH per_table AS (
    SELECT source, max(ingested_at) AS last_ingest_at FROM raw.samples
        WHERE valid_from > now() - interval '30 days' GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.activity_streams
        WHERE t > now() - interval '30 days' GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.sleep_stages
        WHERE started_at > now() - interval '30 days' GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.daily_metrics GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.activities GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.sleep_sessions GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.strength_exercises GROUP BY source
    UNION ALL
    SELECT source, max(ingested_at) FROM raw.strength_sets GROUP BY source
)
SELECT
    source,
    max(last_ingest_at)                                   AS last_ingest_at,
    extract(epoch FROM (now() - max(last_ingest_at)))::bigint AS lag_seconds
FROM per_table
WHERE last_ingest_at IS NOT NULL
GROUP BY source;

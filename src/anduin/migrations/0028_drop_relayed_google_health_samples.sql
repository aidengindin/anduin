-- Purge raw.samples rows the Google Health extractor ingested from devices
-- other than the Fitbit Air.
--
-- The v4 API returns every data point in the account. With Apple Health import
-- on, that included the iPhone pedometer and everything synced into HealthKit
-- (Garmin Connect, Wahoo, Liftosaur), all tagged dataSource.platform =
-- 'HEALTH_KIT' but stored as source 'google_health' / device 'fitbit_air'.
-- Their intervals never share a start time with the Air's minute buckets, so
-- the natural key never collided and they stacked on top: steps ran 2-3x high
-- from 2026-09-15 on, with active_energy and heart_rate contaminated too.
--
-- The extractor now drops non-FITBIT points at the list client
-- (sources/google_health.py `_from_air`). This removes what already landed.
-- Rows with no dataSource are kept, matching the extractor's rule.
--
-- Side effect, intended: days where HealthKit steps were the only "wearable"
-- steps (the Air was off) no longer block the Garmin whole-day fallback (0027).

-- The platform lives in jsonb, which compressed chunks can't filter on, so the
-- DELETE decompresses every google_health segment of these metrics (~2.3M rows,
-- mostly heart_rate) and blows the default 100k DML decompression limit. Lift
-- it for this transaction only; the compression policy recompresses later.
-- `metric` is a segmentby column, so naming them skips the spo2/hrv segments.
SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0;

DELETE FROM raw.samples
WHERE source = 'google_health'
  AND metric IN ('steps', 'active_energy', 'heart_rate', 'distance')
  AND raw -> 'dataSource' ->> 'platform' IS NOT NULL
  AND raw -> 'dataSource' ->> 'platform' <> 'FITBIT';

-- Ingest refreshes this after every run; do it here so the fix is visible on
-- deploy. (Not CONCURRENTLY: the migration runs inside a transaction.)
REFRESH MATERIALIZED VIEW canonical.activity_daily;

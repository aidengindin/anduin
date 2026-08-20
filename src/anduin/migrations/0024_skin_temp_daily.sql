-- Nightly skin temperature, from the v4 daily-sleep-temperature-derivations
-- type.
--
-- Google Health v4 has no `skin-temperature` data type at all: the Fitbit-era
-- metric is exposed only as a nightly *derivation* — the mean of the wrist skin
-- samples taken during sleep, plus the trailing 30-day median it is compared
-- against. That pair is what the Fitbit app plots as "skin temperature
-- variation". The extractor stores both faithfully as daily metrics
-- (skin_temp_nightly / skin_temp_baseline / skin_temp_rel_stddev_30d) and the
-- subtraction happens here, so the absolute nightly reading is not lost.
--
-- `variation_c` is NULL until a baseline exists (the first ~30 nights on a new
-- device), which is correct: no baseline means no deviation to report, and the
-- UI's "no reading" path is the honest rendering of that.
--
-- The older sample-grained canonical.skin_temp view (metric = 'skin_temp' /
-- 'sleep_skin_temp') is left in place for pre-v4 history; nothing writes those
-- metrics any more.
CREATE OR REPLACE VIEW canonical.skin_temp_daily AS
SELECT
    n.local_date,
    n.source,
    n.device,
    n.tz_offset_minutes,
    n.value                AS nightly_c,
    b.value                AS baseline_c,
    n.value - b.value      AS variation_c,
    sd.value               AS rel_stddev_30d_c
FROM canonical.daily_metrics n
LEFT JOIN canonical.daily_metrics b
       ON b.metric = 'skin_temp_baseline' AND b.local_date = n.local_date
LEFT JOIN canonical.daily_metrics sd
       ON sd.metric = 'skin_temp_rel_stddev_30d' AND sd.local_date = n.local_date
WHERE n.metric = 'skin_temp_nightly';

GRANT SELECT ON canonical.skin_temp_daily TO anduin_ro;

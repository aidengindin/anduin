-- Canonical sleep views over raw.sleep_sessions / raw.sleep_stages (0010).
--
-- One night can carry several sessions (a main sleep plus naps); canonical.sleep
-- picks one per calendar day, preferring the main sleep and then the longest.
-- This is precedence-ready for multiple sources even though google_health is the
-- only one today.
--
-- NOTE (known limitation): the day bucket uses date_trunc('day', started_at) in
-- UTC. Unlike raw.daily_metrics, sleep has no source-supplied local date, so a
-- session that starts late enough to cross the UTC midnight after travel could
-- land in an adjacent bucket. The parallel fix is a date_of_sleep column on
-- raw.sleep_sessions; deferred.
CREATE OR REPLACE VIEW canonical.sleep AS
SELECT DISTINCT ON (date_trunc('day', started_at))
    started_at,
    ended_at,
    source,
    device,
    is_main_sleep,
    sleep_type,
    minutes_asleep,
    minutes_awake,
    minutes_in_sleep_period,
    minutes_to_fall_asleep,
    minutes_after_wakeup,
    efficiency,
    summary
FROM raw.sleep_sessions
ORDER BY date_trunc('day', started_at),
         is_main_sleep DESC NULLS LAST,
         minutes_asleep DESC NULLS LAST;

CREATE OR REPLACE VIEW canonical.sleep_stages AS
SELECT source, session_uid, stage, started_at, ended_at
FROM raw.sleep_stages;

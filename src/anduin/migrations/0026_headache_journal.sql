-- Headache journal: user-entered check-ins, per-day context, and the daily
-- rollup the UI reads. See docs/plans/2026-09-01-headache-log-design.md.
--
-- `journal` holds observations the owner types in, as opposed to `raw` (what a
-- device or API said) and `identity` (who the owner is and what they are aiming
-- at). Same read-only grant pair as every other schema (0009 / 0013).
CREATE SCHEMA IF NOT EXISTS journal;
GRANT USAGE ON SCHEMA journal TO anduin_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA journal GRANT SELECT ON TABLES TO anduin_ro;

-- A check-in is "how is my head right now": a timestamp and a 0-10 intensity,
-- with optional symptom detail. It is NOT an attack with a start and end --
-- the owner's headaches are low-grade all day with short flare-ups, so a day is
-- a small series of points and peak / mean / hours are *derived* from them.
--
-- local_date is stamped by the write path from the browser's UTC offset
-- (tz_offset_minutes), falling back to the server's zone for check-ins that
-- arrive from the ntfy "No headache" button with no browser attached. It is
-- stored rather than computed so a day's check-ins are a plain index lookup and
-- so the civil date is fixed at the moment of logging, not re-derived later
-- under a different session timezone.
--
-- The DB enforces every range and enum because the ntfy action posts straight
-- to the API; the form is not the only way in. user_id leads the natural key.
CREATE TABLE IF NOT EXISTS journal.headache_checkins (
    id                bigserial   PRIMARY KEY,
    user_id           int         NOT NULL REFERENCES identity.users(id),
    logged_at         timestamptz NOT NULL,
    tz_offset_minutes smallint    NOT NULL,
    local_date        date        NOT NULL,
    intensity         smallint    NOT NULL CHECK (intensity BETWEEN 0 AND 10),
    nausea            smallint    NOT NULL DEFAULT 0 CHECK (nausea BETWEEN 0 AND 3),
    light_sensitivity boolean     NOT NULL DEFAULT false,
    noise_sensitivity boolean     NOT NULL DEFAULT false,
    -- "qualities", not "character": the latter is a SQL type name.
    qualities         text[]      NOT NULL DEFAULT '{}'
        CHECK (qualities <@ ARRAY['pressure','throbbing','sharp','icepick','unilateral']),
    note              text,
    -- Where the row came from. 'ntfy' rows are one-tap "no headache" answers
    -- to a reminder; keeping the distinction lets compliance be measured.
    source            text        NOT NULL CHECK (source IN ('app','ntfy')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, logged_at)
);
CREATE INDEX IF NOT EXISTS headache_checkins_user_date
    ON journal.headache_checkins (user_id, local_date);

-- Per-day context, one optional row per day.
--   fluorescent_exposure  the one suspected trigger worth a daily field
--   peak_intensity        an override for a flare that came and went between
--                         check-ins, so it need not be faked as a backdated
--                         check-in. NULL means "the check-ins tell the story".
--   coffee_cups /         daily intake counts (the Airtable fields). NULL is
--   alcohol_drinks        "not recorded", which is not the same as 0.
CREATE TABLE IF NOT EXISTS journal.headache_days (
    id                   bigserial   PRIMARY KEY,
    user_id              int         NOT NULL REFERENCES identity.users(id),
    local_date           date        NOT NULL,
    fluorescent_exposure text        CHECK (fluorescent_exposure IN ('none','brief','hours')),
    peak_intensity       smallint    CHECK (peak_intensity BETWEEN 0 AND 10),
    coffee_cups          smallint    CHECK (coffee_cups BETWEEN 0 AND 20),
    alcohol_drinks       smallint    CHECK (alcohol_drinks BETWEEN 0 AND 20),
    note                 text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, local_date)
);

GRANT SELECT ON journal.headache_checkins, journal.headache_days TO anduin_ro;

-- Daily rollup: one row per (user, day) that has a check-in or a context row.
--   checkin_peak  max intensity seen at a check-in
--   day_peak      the headache_days override
--   peak          the greater of the two -- a remembered flare outranks what
--                 the check-ins happened to catch. NULL when the day has
--                 neither: a fluorescent-only row is not a headache-free day.
--                 This is the metric column, and it is deliberately NOT
--                 zero-filled anywhere: a day with no check-ins is unknown.
-- Both peaks are exposed so later analysis can tell "seen at a check-in"
-- from "remembered at the end of the day".
CREATE OR REPLACE VIEW derived.headache_daily AS
WITH c AS (
    SELECT
        user_id,
        local_date,
        count(*)                              AS n_checkins,
        max(intensity)                        AS checkin_peak,
        avg(intensity)                        AS mean_intensity,
        max(nausea)                           AS max_nausea,
        bool_or(light_sensitivity)            AS light_any,
        bool_or(noise_sensitivity)            AS noise_any
    FROM journal.headache_checkins
    GROUP BY user_id, local_date
), q AS (
    -- Union of qualities seen that day. A separate unnest rather than
    -- array_agg(qualities): aggregating text[] of differing lengths errors.
    SELECT x.user_id, x.local_date, array_agg(DISTINCT u.q ORDER BY u.q) AS qualities
    FROM journal.headache_checkins AS x
    CROSS JOIN LATERAL unnest(x.qualities) AS u(q)
    GROUP BY x.user_id, x.local_date
), d AS (
    SELECT user_id, local_date, fluorescent_exposure, peak_intensity,
           coffee_cups, alcohol_drinks, note
    FROM journal.headache_days
)
SELECT
    coalesce(c.user_id, d.user_id)          AS user_id,
    coalesce(c.local_date, d.local_date)    AS local_date,
    coalesce(c.n_checkins, 0)               AS n_checkins,
    c.checkin_peak                          AS checkin_peak,
    d.peak_intensity                        AS day_peak,
    greatest(c.checkin_peak, d.peak_intensity) AS peak,
    c.mean_intensity                        AS mean_intensity,
    (greatest(c.checkin_peak, d.peak_intensity) > 0) AS any_headache,
    c.max_nausea                            AS max_nausea,
    coalesce(q.qualities, '{}'::text[])     AS qualities,
    coalesce(c.light_any, false)            AS light_any,
    coalesce(c.noise_any, false)            AS noise_any,
    d.fluorescent_exposure                  AS fluorescent_exposure,
    d.coffee_cups                           AS coffee_cups,
    d.alcohol_drinks                        AS alcohol_drinks,
    d.note                                  AS day_note
FROM c
FULL OUTER JOIN d ON d.user_id = c.user_id AND d.local_date = c.local_date
LEFT JOIN q ON q.user_id = coalesce(c.user_id, d.user_id)
           AND q.local_date = coalesce(c.local_date, d.local_date);

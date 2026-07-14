-- PMC / Performance Management Chart (phase E). CTL (fitness) and ATL (fatigue)
-- are ingested from the intervals wellness feed into daily_metrics; Form (TSB) is
-- the derived CTL - ATL. We surface, not recompute (intervals owns the model).
CREATE OR REPLACE VIEW derived.pmc AS
WITH c AS (SELECT local_date, value AS ctl FROM canonical.daily_metrics WHERE metric = 'ctl'),
     a AS (SELECT local_date, value AS atl FROM canonical.daily_metrics WHERE metric = 'atl')
SELECT
    COALESCE(c.local_date, a.local_date) AS local_date,
    c.ctl,
    a.atl,
    c.ctl - a.atl AS form
FROM c FULL JOIN a USING (local_date);

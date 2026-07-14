# Derived metrics design (2026-07-14)

Analytical / rolling metrics computed on top of the reconciled `canonical`
layer. These answer "how am I trending / is this normal for me", not "what is
the value" — so they live in a new **`derived`** schema (SQL views only, same
views-not-tables philosophy as `canonical`).

Decisions are settled (see conversation 2026-07-14). **Build everything now** —
seeded/unit validation where the source device (Fitbit Air) has no data yet;
live validation now for the intervals- and Withings-backed metrics.

## Scope

| Metric | Source grain | Live data now? |
|---|---|---|
| Sleep efficiency (+ trailing avg) | `sleep_sessions` | ❌ device |
| Sleep stage composition (%deep/REM/light/awake + trailing) | `sleep_stages` | ❌ device |
| Sleep regularity — **SRI** | `sleep_stages` (local time) | ❌ device |
| HRV status (7d vs personal baseline band) | `daily_metrics` | ❌ device |
| RHR range/trend (7d vs baseline) | `daily_metrics` | ❌ device |
| Respiratory-rate elevation flag | `daily_metrics` | ❌ device |
| Overnight SpO₂ desaturation flag | `daily_metrics` | ❌ device |
| Body weight + composition trend (avg + slope) | `raw.samples` | ✅ Withings |
| PMC — CTL / ATL / Form | **ingested from intervals** | ✅ intervals |

Explicitly **out**: sleep debt (no agreed target), readiness composite, BP
classification.

## Schema & conventions

- New `derived` schema; migration mirrors 0009's grant pair (USAGE + SELECT +
  ALTER DEFAULT PRIVILEGES for `anduin_ro`).
- Plain views with window functions. Data is personal-scale; promote to
  continuous aggregates only if perf ever bites.
- Tunable constants (window lengths, baseline SD multiplier, thresholds) inlined
  in the views for v1, documented at each site. Promote to a `derived.params`
  table only if retuning-without-migration becomes a real need.
- Rolling windows are **trailing, as-of each day**, so every view is queryable at
  any date, not just "today".

## Per-metric spec

### Sleep efficiency — `derived.sleep_efficiency`
Per night: `efficiency_pct = minutes_asleep / time_in_bed_min * 100`, where
`time_in_bed_min = (ended_at - started_at) in minutes`. Also surface the source's
own `efficiency` for comparison, and a trailing 7-night average of ours.

### Sleep stage composition — `derived.sleep_stage_composition`
Per night from `sleep_stages`: minutes and % per stage (deep/rem/light/awake),
`total_min`, plus trailing 7-night average of each %. `restorative_pct` =
%deep + %rem as a convenience column.

### Sleep regularity index — `derived.sleep_regularity`
The Phillips et al. (2017) SRI: over a trailing window, the fraction of clock
epochs in the same state (asleep/awake) as the same clock-minute the day before,
rescaled to [-100, +100]:
`SRI = -100 + 200 * mean_over_epochs( state(t) == state(t-24h) )`.
- Epoch = 1 minute, in **local** time (needs `tz_offset_minutes`).
- State = asleep if the minute falls inside any sleep-stage segment that is not
  WAKE; else awake (gaps and non-sleep count as awake — the standard assumption).
- Build in two views: `derived._sleep_minute_state` (per-minute local state) and
  `derived.sleep_regularity(as_of_date, sri, nights)`.
- Window default **14 nights** (tunable). `nights < ~7` → SRI NULL (insufficient).

### HRV status — `derived.hrv_status`
Over `hrv_daily_rmssd`:
- `hrv_7d` = trailing-7d mean.
- baseline over trailing **60d**: `base_mean`, `base_sd` (require ≥14 days present
  else `status='pending'`).
- band = `base_mean ± 1*base_sd`. `status` = balanced (in band) / low (below) /
  high (above) / pending.

### RHR range — `derived.rhr_status`
Same construction over `resting_heart_rate`. Elevated flag when
`rhr_7d > base_mean + base_sd` (rising RHR = fatigue/illness signal).

### Respiratory-rate flag — `derived.respiratory_rate_status`
Baseline 30d mean+SD over `respiratory_rate`; `elevated = today > mean + 1*sd`.

### SpO₂ flag — `derived.spo2_status`
Over `spo2_daily_min` / `spo2_daily_avg`: `low_night = min < 90` (absolute) OR
`avg < base_mean - base_sd` (personal deviation). Baseline 30d.

### Body-composition trend — `derived.body_composition_trend`
Per metric in {body_weight, fat_mass, fat_free_mass, muscle_mass, body_fat_ratio}:
trailing-7d avg (smooths daily noise) and trailing-28d slope via `regr_slope`
expressed per week (kg/week or %/week). Weight is real Withings data → validate
now.

### PMC (CTL/ATL/Form) — ingest, then `derived.pmc`
intervals.icu already computes these; do not recompute. Pull the athlete
**wellness** feed (`GET /api/v1/athlete/{id}/wellness`), which carries daily
`ctl`, `atl` (and rampRate). Emit `daily_metrics` rows `ctl`, `atl` keyed on the
wellness date; `derived.pmc(local_date, ctl, atl, form)` with `form = ctl - atl`.
(Field names to be confirmed live against the wellness endpoint during impl.)

## Extractor changes

1. **`google_health` sleep**: extract each session's UTC offset from the v4 sleep
   payload into a new `raw.sleep_sessions.tz_offset_minutes` column (mirrors
   `daily_metrics`). Field name inferred until device data confirms it; degrade
   to NULL if absent (SRI then falls back to UTC with a documented caveat).
2. **`intervals` wellness**: new pull of the wellness feed → `daily_metrics`
   (ctl, atl). Additive; guarded like the streams pull.

## Validation

- Pure math (SRI concordance, efficiency, slope) unit-tested against seeded
  fixtures; SQL views checked with seeded-transaction assertions on the dev DB
  (`scripts/dev-db.sh`), same approach as the HR union.
- Live now: body-composition trend (Withings), PMC (intervals).
- Deferred to device: sleep/HRV/RHR/RR/SpO₂ derivations (build + seed-test now,
  live-validate when the Air arrives).

## Implementation phases (each a commit, validated on the dev DB)

- **A.** `derived` schema + grants; body-composition trend (real data).
- **B.** sleep efficiency + stage composition.
- **C.** `tz_offset_minutes` column + extractor change; SRI views.
- **D.** hrv_status, rhr_status, respiratory_rate_status, spo2_status.
- **E.** intervals wellness ingestion + `derived.pmc`.

## Status — all phases complete (2026-07-14)

All five phases implemented, migrated (0013–0017) and validated on a local
TimescaleDB (`scripts/dev-db.sh`). Live-validated on real data: body-composition
trend (Withings), PMC (intervals, Form coherent with the load). Seeded/unit
validated pending device: sleep efficiency + stage composition (math matches by
hand), SRI (+100 identical / ~29 for 4h-alternating), HRV/RHR/RR/SpO₂ status
(classification triggers correctly). 82 Python tests pass, ruff clean.

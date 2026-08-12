# anduin

Personal health data pipeline: source extractors (Google Health / Withings /
intervals.icu / Liftosaur) → TimescaleDB, with a `raw` layer and a reconciled
`canonical` view layer.

## Design rationale (the "why" behind the schema)

### NEAT vs. total — why steps and energy are split three ways

For steps and active energy the canonical layer deliberately keeps three views
(`neat_*` outside workouts, `workout_*` per workout, `total_*` = union). This is
not incidental factoring — it serves two distinct questions the owner cares
about:

1. **Total for the day** — what every consumer health app shows. `total_steps` /
   `total_energy` answer this with no double-counting (NEAT samples outside
   workout windows + one per-workout contribution inside them).
2. **Movement *outside* of exercise** — the `neat_*` views in isolation. The
   goal is to stay active throughout the day and not be sedentary between
   workouts (a hard workout followed by 15 sedentary hours is a real failure
   mode). Watching NEAT on its own surfaces that; the daily total hides it.

Keep both readouts available. Do not collapse `neat_*` into `total_*`.

### Heart rate is unioned directly (not split like steps/energy)

HR does **not** get the neat/workout/total trio. The NEAT distinction is
meaningful for *volitional movement* (steps, calories burned moving around) but
not for heart rate — "resting HR outside a workout" isn't a couch-potato signal
in the same way. So `canonical.heart_rate` is a single view that unions the
continuous (Air) sample stream with the per-workout HR stream from
`raw.activity_streams`, with the workout stream owning its own time window.

### user_id on every row — multi-user without a future migration

anduin is single-user today, but every measurement table carries a
`user_id int NOT NULL REFERENCES identity.users(id)` **folded in as the leading
column of its PK / unique index** (and into each compressed hypertable's
`compress_segmentby` and each continuous aggregate's `GROUP BY`). There is one
seeded user (`identity.users` id = 1); the write path stamps the configured owner
id (`FileConfig.user_id`, default 1) on every row — never a DB-level default,
which would silently mislabel rows once a second user exists.

The point is that the expensive-to-change parts of the schema (hypertable
columns/segmentby, unique indexes, CAGG grouping) are already user-partitioned,
so adding multi-user support after deployment is a **read-path change only** — no
data migration, no re-keying. That remaining work: propagate `user_id` through
the `canonical.*` / `derived.*` views and add `WHERE user_id = :current_user` in
`web/queries.py`, plus a way to resolve the current user. All those are
`CREATE OR REPLACE VIEW` / query edits — cheap and reversible. Folding `user_id`
into the natural keys also makes cross-user collisions on an upstream
`natural_key` / `activity_uid` / `session_uid` structurally impossible.

## Canonical precedence

Inside a recorded workout the activity-stream source (intervals) wins; outside,
the Fitbit Air wins. For sample-grained metrics this runs through
`canonical.precedence_rules` (see migration 0006). Note: the `heart_rate`
precedence rows predate the union view and are inert for the workout/Air split —
that reconciliation now lives in the `canonical.heart_rate` union itself.

### Workout calories: a fallback chain, with the losers kept

`canonical.workout_load.calories` picks the best available number in this order
(migration 0023), recording which one won in `calories_method`:

1. **`source`** — intervals computed it. Trust it.
2. **`work`** — `icu_joules / 1000` for rides missing calories. That constant is
   the ~24% gross-efficiency identity (1 kJ of mechanical work ≈ 1 kcal burned),
   *not* a unit conversion — `/ 4184` would give kcal of work and undercount the
   metabolic cost ~4×. Verified against rides where intervals supplies both.
3. **`met_strength`** — MET model for lifting: `(MET − 1) × 1.05 × mass_kg ×
   hours`, MET scaling 3.5→6.0 on working-set density (clamped to 0.20–0.50
   sets/min), body mass from the latest Withings `body_weight`.
4. **`fitbit_window`** — sum of in-window `active_energy` samples.

The MET model outranks Fitbit for strength deliberately: a wrist accelerometer
can't see isometric load and can't tell the session is resistance training at
all, and its output is erratic (1.56–3.67 kcal/min across near-identical
sessions of the same program). **There is no ground truth for either** — no
metabolic cart, no logged intake — so this buys consistency, not verified
accuracy. `calories_met`, `calories_fitbit` and `calories_work` are all exposed
as columns so the models can be compared on accumulated data before anything is
discarded.

Everything here is net of resting metabolism (Health Connect's
`ActiveCaloriesBurned` is above-BMR; the MET model subtracts 1 MET), so the
columns are comparable and summing into `activity_daily` never double-counts
BMR.

Note the interaction with the window guard: any workout with a non-NULL
`ended_at` carves a window that removes its samples from `neat_energy`, so a
workout that carves a window **must** contribute a calorie number or that energy
vanishes from `total_energy` — which is exactly what happened to every liftosaur
session before 0023 (55–180 kcal each).

## Units / naming gotchas

- intervals.icu activity streams store HR under metric **`heartrate`** (no
  underscore); `raw.samples` and canonical use **`heart_rate`**.
- intervals stream type for speed is **`velocity_smooth`** (`speed` 422s).
- Per-workout energy: **calories** is the primary metric; `workout_load` surfaces
  **TSS (training load)**, a separate thing — don't conflate them.

## Fitbit Air / Google Health v4 quirks (learned from real data)

- **Daily HRV/SpO2 rollups are empty for the Air.** Google's `daily-heart-rate-
  variability` and `daily-oxygen-saturation` endpoints return nothing; only the
  per-*sample* streams are populated (both measured only during sleep). So
  `canonical.hrv_daily` and `canonical.spo2_daily` are derived by averaging the
  sample stream inside each `canonical.sleep` window, keyed to the local **wake**
  date (see migrations 0018/0019). `derived.hrv_status` / `derived.spo2_status`
  read those nightly views, not `raw.daily_metrics`. `resting_heart_rate` and
  `respiratory_rate` daily rollups *are* populated, so those stay as-is.
- **SpO2 has a `50.0` sentinel** for dropped/invalid pulse-ox reads. Filter
  `value >= 70` before aggregating or it corrupts the avg/min and false-trips the
  desaturation flag.
- **Local timezone is a sibling field, not in the timestamp.** Sleep/steps
  intervals keep `startTime` as a `Z` UTC stamp and carry the wearer's offset in
  `startUtcOffset` ("-14400s") plus a `civilStartTime` local date. The extractor
  reads `startUtcOffset` for sleep `tz_offset_minutes`; `canonical.steps_daily`
  buckets by the civil date. UI shows local via `sleep_efficiency.started_local`/
  `ended_local`.
- **RHR: the API value ≠ the app value.** Google's API returns e.g. 52 while the
  Fitbit app shows 53 (different aggregation). We store the API value faithfully.

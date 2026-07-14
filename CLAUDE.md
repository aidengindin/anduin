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

## Canonical precedence

Inside a recorded workout the activity-stream source (intervals) wins; outside,
the Fitbit Air wins. For sample-grained metrics this runs through
`canonical.precedence_rules` (see migration 0006). Note: the `heart_rate`
precedence rows predate the union view and are inert for the workout/Air split —
that reconciliation now lives in the `canonical.heart_rate` union itself.

## Units / naming gotchas

- intervals.icu activity streams store HR under metric **`heartrate`** (no
  underscore); `raw.samples` and canonical use **`heart_rate`**.
- intervals stream type for speed is **`velocity_smooth`** (`speed` 422s).
- Per-workout energy: **calories** is the primary metric; `workout_load` surfaces
  **TSS (training load)**, a separate thing — don't conflate them.

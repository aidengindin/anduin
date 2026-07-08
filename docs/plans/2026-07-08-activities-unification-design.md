# Activities Unification & Energy/Steps Reconciliation — Design

Date: 2026-07-08

Addresses four pieces of feedback on the original prototype schema:

1. The cardio-vs-strength table split is artificial and won't survive
   multi-modal sources (Garmin strength with per-set + 1 Hz HR; Liftosaur
   adding HR; app switches).
2. Liftosaur unilateral exercises record left/right reps independently.
3. Steps and calories are treated as purely NEAT; there's no total view, and
   workout-supplied calories aren't used.
4. Deriving activity calories from kJ is only valid for cycling.

Design philosophy is unchanged: `raw.*` holds immutable per-source
observations; `canonical.*` resolves conflicts via views + precedence rules at
read time.

---

## 1. Unified activities table (raw-layer, one row per source-workout)

Replace `raw.activities` + `raw.strength_workouts` with a single
`raw.activities`. **One row = one workout as recorded by one source.** Streams,
exercises, and sets all reference it.

- A Garmin strength session carries both `activity_streams` *and*
  `strength_exercises`/`strength_sets` under one row.
- A Liftosaur session carries sets only. If Liftosaur later adds HR, its row
  simply gains streams — no schema change.
- Cross-source overlap (e.g. Liftosaur sets + separately-recorded Fitbit HR at
  the same wall-clock time) is **not** merged into one raw row. That stitching
  is a canonical-layer concern, resolved by the existing precedence machinery at
  read time. Raw fidelity is preserved: one row per source.

### `raw.activities` (unified)

```
source            text        NOT NULL
activity_uid      text        NOT NULL
device            text
recording_method  text
sport             text                      -- 'strength' is just another value
program           text                      -- was strength_workouts.program; nullable
started_at        timestamptz NOT NULL
ended_at          timestamptz               -- NULLABLE (strength may lack an end)
summary           jsonb                     -- nullable; source-supplied rollups
raw               jsonb       NOT NULL
natural_key       text        NOT NULL
ingested_at       timestamptz NOT NULL DEFAULT now()
PRIMARY KEY (source, activity_uid)
CHECK (ended_at IS NULL OR ended_at >= started_at)
```

The old `raw.activities` required `ended_at NOT NULL` and `summary NOT NULL`;
the old `raw.strength_workouts` allowed neither. Unified table relaxes both to
serve strength. Restatement trigger stays.

Children re-point from `workout_uid` to `activity_uid`:

- `raw.activity_streams` — FK `(source, activity_uid)` → `raw.activities`.
  Unchanged otherwise.
- `raw.strength_exercises` — FK `(source, activity_uid)` → `raw.activities`
  (was `strength_workouts`). PK becomes
  `(source, activity_uid, exercise_uid)`.
- `raw.strength_sets` — FK to `strength_exercises` on the new key.

### Reconciliation window guard

`canonical.samples_with_condition` tags a sample as inside a workout via
`s.valid_from >= a.started_at AND s.valid_from < a.ended_at`. With `ended_at`
now nullable, a strength workout with no end would break the comparison. Guard
it: only activities with a non-NULL `ended_at` define a stream/sample window
(`a.ended_at IS NOT NULL AND s.valid_from < a.ended_at`). Strength-only
workouts without an end don't own any continuous-sample window, which is
correct — they have no streams to win.

---

## 2. Unilateral exercises

Confirmed against real records: unilateral notation is `right|left` (`3x8|7` =
8 right, 7 left; the user's own data has `2x6|6`, `2x10|10`, etc.). So the
first/primary value maps to the existing `reps` column and the *left* side is
the optional add-on (the original guess was reversed).

- `raw.strength_exercises`: add `is_unilateral boolean NOT NULL DEFAULT false`.
- `raw.strength_sets`: add `left_reps int` (nullable). For a bilateral set it's
  NULL; `reps` holds the single value. For a unilateral set, `reps` = right,
  `left_reps` = left.

### Liftosaur ingestion rewrite (discovered during this work)

The prototype's `liftosaur.py` fetched `GET /api/storage?apikey=` and parsed a
nested JSON shape (`entries[].sets[].completedReps`). That endpoint returns `{}`
for the live key — the ingestion was non-functional. The working API is
`GET /api/v1/history` (Bearer auth), which returns workout records as
**Liftohistory text**, not JSON. Rewrote ingestion accordingly:

- New pure parser `anduin.sources.liftohistory` for the text format: exercise
  lines, set groups `NxR[|L][+] Wunit [@RPE[+]]`, `warmup:`/`target:` sections
  (target = prescribed, skipped), lb/kg, AMRAP, and **negative weights** for
  assisted exercises (e.g. `2x7 -10lb`). Fully unit-tested against real records.
- `liftosaur.py` now pages `/api/v1/history` with cursor/limit and maps each
  record via the parser. Validated end-to-end against the live API into a real
  Postgres: idempotent re-ingest (0 spurious restatements), `left_reps` and
  `is_unilateral` populated, assisted weights stored negative.

---

## 3. Calories model

Kill the general kJ→kcal path. `icu_joules / 4184` only approximates energy
expenditure for cycling (metabolic efficiency ~24% × 4.184 J/cal ≈ 1.0, a
coincidence, not physics). It is meaningless for other sports.

**Per-workout calories** (`canonical.workout_load`):

- Primary: source-supplied `summary.calories` for every sport.
- Cycling-only fallback: if `calories` is absent but power-derived
  `icu_joules` exists, use `joules / 4184`, and expose a flag
  (`calories_is_derived`) so derived values are distinguishable from measured
  ones.
- Otherwise NULL — never a fabricated number.

**NEAT vs. total — keep both, don't collapse:**

- `canonical.neat_energy` — unchanged (outside-workout `active_energy`).
- New `canonical.total_energy` — a view that **unions** NEAT samples (outside
  workout windows) with one calorie contribution per workout (inside them).
  Summing over any window gives the true total with no double-counting, while
  still allowing filters to just-NEAT or just-workout.

---

## 4. Steps model (symmetric with heart rate)

Inside a workout the workout source owns steps; outside, the Air owns them —
mirroring HR precedence.

- `canonical.neat_steps` — Air step samples **outside** workout windows. Fixes
  today's `canonical.steps`, which is `'always' → Air` and silently includes
  junk wrist-step counts during cycling/lifting.
- **Workout steps** — per workout:
  `COALESCE(summary.steps, cadence_derived)`.
  - `cadence_derived` applies **only to foot sports** (run/walk/hike). Cycling
    cadence is crank RPM, unrelated to steps → no derivation; cycling
    contributes no derived steps.
  - `cadence_derived = cadence_integral × stride_factor`.
- `canonical.total_steps` — union of NEAT steps + workout steps, same pattern as
  `total_energy`. A lifting workout has no cadence and no supplied steps →
  contributes NULL, which is correct.

### `stride_factor`

Cadence units are inconsistent across sources: total steps/min (~170–180) vs.
strides/min per leg (~85–90), off by exactly 2×. Do **not** hardcode. Store a
calibratable `stride_factor` keyed by `(source, sport)`, default 1.0.

- Today every non-Liftosaur workout flows through intervals.icu, so the table is
  effectively one meaningful row: `('intervals', 'running') → ~2.0` (intervals
  is believed to report per-leg; **to be verified**).
- Calibration is empirical: take one run with a known Garmin step count,
  integrate the intervals cadence stream over the same window, and the ratio is
  the factor. No guessing, and correctable later without touching raw data.

---

## Migration notes

- New migrations, not edits to existing `0004`/`0005` (append-only style).
- `raw.strength_workouts` → folded into `raw.activities`; children re-keyed to
  `activity_uid`.
- Precedence seed (`0006`) gains inside/outside rules for `steps` (workout
  source wins inside, Air wins outside), mirroring `heart_rate`.
- `stride_factor` lives in a small `canonical.stride_factors` table.

## Resolved during implementation

- Unilateral storage shape confirmed via real data (`right|left` text notation).
- Liftosaur ingestion endpoint bug found and fixed (dead `/api/storage` →
  `/api/v1/history` text parser).
- All hand-written SQL (0005 DDL, 0006/0007 views) applied and functionally
  checked against a real Postgres; calorie/steps arithmetic verified with seeded
  data; Liftosaur ingestion verified end-to-end against the live API.
- **`stride_factor = 2.0` for intervals confirmed empirically.** Real run
  cadence streams read ~83/s (per-leg, not ~166). On a sample run,
  `cadence × time × 2` (~2313 steps) matches both `distance ÷ stride` (~2261)
  and the `speed = 2·cadence·stride` identity; factor 1 is off by exactly 2×.
  intervals does not expose a `summary.steps` field for runs, so the
  cadence-derivation is the primary (not fallback) path — which is why it
  matters that the factor is right.

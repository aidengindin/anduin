# Weight goal tracking design (2026-08-19)

Make the body-weight readout answer the question the owner actually has while
bulking: **am I gaining at the rate I intended — too fast, too slow, or on
target?** Today the page shows a per-week rate that is too noisy to act on, and
no notion of a target at all.

Decisions are settled (see conversation 2026-08-19). Build everything now; all
of it is backed by live Withings data.

## Why the current number is noisy

`derived.body_composition_trend.slope_per_week` (migration 0013) is an OLS
regression of **raw** weight against time over a trailing **28 days**, evaluated
at the latest reading. Day-to-day swings of 1–2 lb (hydration, gut content, meal
timing) sit on top of a real bulk signal near 0.3–0.5 lb/wk, so the standard
error of that slope is comparable to the signal. It is also endpoint-sensitive:
every weigh-in adds one point and drops another.

The fix is to **smooth first, then fit**. The view already computes `avg_7d` and
nothing reads it.

## Scope

1. `identity.goals` — append-only phase history, edited from the UI.
2. Smoothed trend math in `derived.body_composition_trend`.
3. Daily readings + 7d/30d rolling averages + a rolling goal corridor on
   `/metrics/body_weight`.
4. A no-JavaScript goal editor on that same page.

Explicitly **out**: calorie-intake tracking, goal-aware anything on the home
screen, goals for metrics other than body weight, editing or deleting past
phases.

## 1. Schema (migration 0024)

```sql
CREATE TABLE IF NOT EXISTS identity.goals (
    id                  bigserial PRIMARY KEY,
    user_id             int  NOT NULL REFERENCES identity.users(id),
    started_on          date NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('bulk','cut','maintain','none')),
    target_lb_per_week  numeric,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, started_on),
    CHECK ((kind IN ('bulk','cut')) = (target_lb_per_week IS NOT NULL))
);
```

The phase in effect on date *D* is the latest row with `started_on <= D`. Saving
inserts a row at `current_date`; the `UNIQUE` constraint plus
`ON CONFLICT (user_id, started_on) DO UPDATE` means correcting a typo the same
day rewrites that row instead of accumulating junk.

Three choices worth recording:

- **No rows means no goal.** The default state needs no seeding and renders
  nothing. `kind = 'none'` exists as a *tombstone* so "done bulking, not yet
  cutting" is expressible — it ends a phase without starting a targeted one.
- **`target_lb_per_week` is signed** — negative for a cut, positive for a bulk —
  so downstream comparisons are plain arithmetic with no sign-juggling by
  `kind`. The form accepts a positive magnitude and negates it for cuts.
- **Stored in lb/week, not kg.** This breaks the convention that the DB is SI,
  deliberately: it is a number typed in display units, and round-tripping
  through kg would make `0.4` come back as `0.39999`. The single kg→lb
  conversion moves to the comparison step instead.

`identity` already carries `ALTER DEFAULT PRIVILEGES ... GRANT SELECT ... TO
anduin_ro` (migration 0009), so read grants are inherited.

## 2. Trend math

`derived.body_composition_trend` gains two columns via a second window pass over
the existing one:

| Column | Definition |
|---|---|
| `avg_30d` | trailing 30-day mean — the chart's slower overlay |
| `smoothed_slope_per_week` | `regr_slope(avg_7d, epoch)` over a trailing **28 days**, × 604800 |

Fitting the **smoothed** series rather than raw readings is the whole fix: the
day-to-day variance is gone before the regression sees it, so the number stops
lurching with each weigh-in.

The raw-value `slope_per_week` stays. Nothing else reads it, but keeping it
costs one expression and lets the old and new numbers be compared on the same
rows for a few weeks before either is discarded — the same "keep the losers"
posture as the workout-calorie fallback chain.

`_body_slope_per_week` in `web/queries.py` switches to the smoothed column, so
muscle mass and fat-free mass quiet down at no extra cost.

### Phase clipping lives in the query, not the view

The verdict window depends on a goal row's `started_on`, which a view cannot
know. So the weight page's rate comes from a query function fitting over
`[greatest(now() - 28 days, phase_start), now()]`. With no goal in effect, that
degenerates to exactly the view's value.

### The window was measured, not guessed

This was first written with a 21-day window on the reasoning that a shorter
window reacts faster. That reasoning was wrong. Over 40 synthetic bulks at a
known +0.4 lb/wk with ±1.2 lb of daily noise:

| estimator | mean abs. error | day-to-day jump | days to reliably report a rate change |
|---|---|---|---|
| raw 28d OLS (the old number) | 0.153 | 0.069 | — |
| smoothed 21d | 0.183 | 0.039 | 53 (converged in 26/40 runs) |
| **smoothed 28d** | **0.125** | **0.024** | **37 (35/40)** |
| smoothed 35d | 0.088 | 0.016 | 32 (39/40) |

21 days is dominated on every axis, *including* responsiveness: too noisy to
settle, it frequently never converges at all. 28 days was chosen over the even
steadier 35 because it matches the corridor's four-week anchor — both readouts
then describe the same span — and because a longer window makes a new phase wait
longer before its number can be trusted.

**Guards.** The fit requires ≥5 readings spanning ≥10 days; otherwise the chip
reads `pending` with "needs ~2 weeks in this phase" rather than reporting a wild
rate from four days of data.

**Known limitation.** The view's grain is one row per *reading*, so two weigh-ins
in one day double-weight that day. Harmless with Withings' one-a-morning
pattern, but this is not a properly daily-resampled series and should not be
described as one.

### Verdict

Tolerance is **±50% of the target**, a constant in code and not user-tunable:

| Smoothed rate vs target | Verdict |
|---|---|
| within ±50% | on target |
| above | too fast |
| below | too slow |

A `maintain` phase has target 0, which would collapse the tolerance to zero
width, so maintain uses a fixed **±0.25 lb/wk** instead.

## 3. Chart

`/api/metrics/body_weight.json` grows two things.

**Rolling averages.** For any metric carrying `trend: body_composition_trend`,
`metric_series` pulls `avg_7d` / `avg_30d` from the trend view at daily grain,
aligned to the same `t` array as the raw points. That alignment needs the main
series bucketed daily too — `_bucket_for_range` currently picks `1 hour` for any
range under 60 days, which is meaningless for a once-a-morning weigh-in. Rather
than change that shared function for every metric, the registry gains a
`min_bucket: "1 day"` key that body-composition metrics set.

**Goal corridor.** The target is drawn as a **rolling 4-week corridor**, not a
number. For each day *t*:

```
lo(t) = avg_7d(t - 28d) + (target - tolerance) * 4
hi(t) = avg_7d(t - 28d) + (target + tolerance) * 4
```

That is a constant-width ribbon — ±0.8 lb at a 0.4 lb/wk target — tracking the
weight curve four weeks behind, and it reads as *"where should I be today, given
where I was a month ago"*. Is the 7-day average inside the ribbon, riding its
top edge, or dropping out of the bottom?

The rejected alternative was a wedge anchored at the phase start, fanning
forward at the target rate. It fails twice over. Its width grows without bound —
8 lb tall by week 20 of a 0.4 lb/wk bulk, far too coarse to act on — and because
it never re-anchors, an overshoot in week two pushes the curve outside it
permanently even after intake is corrected. It is also a *cumulative* readout,
which is precisely the verdict basis rejected above in favour of recent rate;
the corridor and the chip would have been measuring different things.

The overlay query reaches back `CORRIDOR_WEEKS` *before* the requested range,
because each corridor day anchors four weeks behind itself. Anchoring on the
visible points alone drew only the last three days of a one-month chart — the
rest could not find an anchor inside the range.

Because the anchor is a single day's `avg_7d`, the ribbon inherits that day's
wobble: a dip in the 7-day average four weeks ago shows up as a dip in today's
corridor. That is the readout being honest about what it is measuring, not a
rendering fault.

The rolling anchor is blank for a phase's first four weeks, for the same reason
the verdict chip reads `pending`: there is nothing honest to draw yet.

Serialized as `goal: {kind, target, lo: [...], hi: [...]}`, null-padded where
undefined, and omitted entirely when no goal is in effect *or* when every day
would be null — a phase younger than four weeks has no ribbon to draw, and the
chart key stays quiet rather than labelling empty space.

Both render through machinery that already exists: `renderMetric` in
`static/charts.js` draws HRV's normal band as two transparent-stroke series plus
a `bands` fill entry, and the corridor is that same shape. The rolling averages
are ordinary extra series — 7-day solid and prominent, 30-day thinner, raw
readings dropped to a faint scatter so the smoothed lines carry the eye.

## 4. Write path

This is the first write in the web UI, which until now was read-only by design.
`queries.py` opens by declaring itself read-only and that stays literally true:
writes go in a new `web/goals.py` exposing `current_goal(conn, user_id)` and
`set_goal(conn, user_id, kind, target)`.

**No JavaScript.** htmx is vendored under `static/` but `base.html` never loads
it and nothing in the app uses it. The goal card is a plain
`<form method="post">` posting to `/metrics/body_weight/goal`, which inserts and
returns `303` back to the page. Server-rendered like the rest of the UI, with no
new client dependency.

The card sits inline under the chart on `/metrics/body_weight`: collapsed, it
shows the current phase and target; expanded, a `kind` radio group and a target
number field.

**Validation** in the route:

- `kind` against the four-value enum.
- target parsed as float and bounded to `0 < t <= 3` lb/wk, so a typo'd `40`
  cannot silently become the goal.
- target required for `bulk`/`cut`, forced NULL for `maintain`/`none`.
- failure re-renders the card with an inline error, not a bare 422.

**Security posture.** The app has no auth of any kind, and this POST is
therefore writable by anything that can reach the port — including cross-site,
as there is no CSRF token. This is accepted for now: the app is reachable only
on the owner's tailnet, and the worst case is a bogus goal row that the next
save overwrites. Revisit if the app is ever exposed more widely.

## Testing

Matching the patterns already in `tests/`:

- `test_web_queries.py` — `FakeCursor` covers slope/verdict/corridor shaping,
  the sign convention for cuts, the maintain tolerance floor, and both `pending`
  guards.
- `test_web_routes.py` — POST validation (bad kind, out-of-range target, missing
  target for a bulk), the 303 redirect, and the inline-error re-render, with
  `get_conn` overridden.
- `test_migrations.py` — already enforces `IF NOT EXISTS` on new DDL.
- The SQL itself gets the manual verification pass against the real database,
  as `test_web_queries.py`'s module docstring describes.


## One rate per page

The header stat and the goal card must not quote different numbers. The header
chip therefore reads from `weight_goal_status` too — the same phase-clipped rate,
coloured by the verdict — and the card states the phase, the target and the
on-target band without repeating the figure. When the phase is too young to
judge, the header says `trend pending` and no rate is quoted anywhere; a number
that isn't trustworthy shouldn't be on screen at all.

The verdict rides that same chip — `↑ 0.49 lb/wk · on target` — rather than
sitting in its own badge further down, so rate and judgement are read together.

Other body-composition metrics keep their own unclipped `per_week` chip: they
have no goal to clip to.

## The editor is last, and closed

The goal is set rarely and read constantly, so the editor is a `<details>` at the
foot of the page, collapsed, with the current phase in its summary
(`Goal · Bulk · 0.40 lb/wk`). Nothing above it changed position.

The weekly-target field applies only to bulk and cut. It hides itself for the
untargeted kinds through `form:has(input[value="maintain"]:checked) .goal-target`
rather than JavaScript — the rule keys off live radio state, so it reacts on
click with no round trip, and degrades to a harmlessly visible field on a browser
without `:has()`.

## Verified

Against a scratch PostgreSQL 16 (TimescaleDB's `time_bucket` shimmed) seeded
with 120 days of synthetic morning weigh-ins:

- Migration 0024 applies, and re-applies, cleanly.
- The phase-clipped query agrees with the view exactly once the window is full
  (both +0.787 lb/wk on the same data), confirming the clipping adds no skew.
- The corridor's width is constant to floating-point across every drawn day, and
  blank for the first four weeks — the property the rolling anchor exists for.
- Chip and corridor agree: with `avg_7d` above the ribbon, the verdict reads
  `too fast`. This is what the phase-start wedge would have got wrong.
- A cut targeting −1.0 lb/wk while weight is rising reads `too slow`, not
  `too fast` — the direction handling holds.
- End to end in the browser: the form POSTs, appends a *new* phase row with the
  sign applied (−0.75 for a cut), leaves the prior bulk row intact, redirects
  back, and the fresh phase correctly reports `needs ~2 weeks in this phase`.

Residual noise is real and not fixed by any of this: on one 120-day sample the
smoothed estimate read +0.787 lb/wk against a true +0.4. The estimator is
steadier than what it replaces, not clairvoyant.

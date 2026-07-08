# anduin

Personal health data pipeline. Polls Google Health (Fitbit Air), Withings (weight),
intervals.icu (activities + streams), and Liftosaur (structured strength sets);
lands everything raw and immutable in a TimescaleDB cluster on `osgiliath`;
derives a canonical reconciled layer via SQL views and continuous aggregates.

Deployment lives in `nixos-config` (see `services/anduin.nix` and
`services/anduin-postgres.nix`). One systemd oneshot+timer per source. OAuth
client credentials are agenix-managed; refresh tokens persist under
`/var/lib/anduin/state/<source>/`.

## CLI

```
anduin extract <source> [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dry-run]
anduin auth <source>          # interactive OAuth seed (google-health, withings)
anduin db migrate
```

## Schema

Two grains: continuous samples (`raw.samples`) and workouts. A workout is one
unified `raw.activities` row per source, regardless of modality — cardio streams
(`raw.activity_streams`) and the strength hierarchy (`raw.strength_exercises` →
`raw.strength_sets`) both hang off it via `activity_uid`. A single row can carry
both (e.g. a strength session with per-set data and 1 Hz HR).

Precedence rules live as data in `canonical.precedence_rules`. Canonical views
reconcile on read using those rules. No persisted canonical sample table — views
+ continuous aggregates only. Energy and steps expose separable NEAT vs.
per-workout contributions plus `canonical.total_energy` / `canonical.total_steps`
union views; workout calories come from the source (kJ→kcal only as a cycling
fallback), and in-workout steps derive from cadence via `canonical.stride_factors`.

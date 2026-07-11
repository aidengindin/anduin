# Write-Path Extensibility — Pre-Deployment Assessment

Date: 2026-07-10

Question answered here: can the deployed schema grow to support (1) writes
from the web UI itself (medication logging, whoop-style journal) and (2)
outbound sync to external APIs (e.g. Google Health data → intervals.icu),
**without messy migrations on a live deployment?**

Answer: yes, with one gap fixed pre-deployment (grants, migration `0009`).
Everything else the two features need is purely additive — new tables in new
append-only migration files, new precedence *rows*, new code. Nothing requires
re-keying, rewriting, decompressing, or backfilling existing hypertables.

## What "messy" would mean, and why none of it is triggered

On a live TimescaleDB deployment the genuinely painful operations are:

| Operation | Needed by these features? |
|---|---|
| Changing a hypertable's unique index / partition column (table rewrite) | No — new grains get new tables |
| Changing `compress_segmentby` / `orderby` (decompress every chunk) | No |
| Rebuilding continuous aggregates | No — CAGGs read only `raw.samples`, which is untouched |
| Backfilling a NOT NULL column on a large table | No |
| Moving rows between tables | No |
| Retrofitting grants across accumulated objects | **Was a latent risk — closed by `0009_readonly_grants.sql`** |

Escape hatch worth knowing: if a raw table ever *does* need a new column
(e.g. an `origin` tag on `raw.samples`), `ALTER TABLE ... ADD COLUMN` of a
nullable column is metadata-only and works on compressed hypertables on the
shipped TimescaleDB (2.23).

## Feature 1: web UI writes (medications, journal)

**Pattern: the UI is just another source.** The raw-layer contract — one row =
one immutable observation from one source — doesn't care that the source is a
human with a form instead of an HTTP poller. Reserve `source = 'ui'` (or
`'manual'`).

New grains, new tables (sketch, not a commitment):

```
raw.medication_events (source, natural_key, med, dose, unit, taken_at,
                       note, raw jsonb, ingested_at, deleted_at)
raw.journal_entries   (source, natural_key, local_date, key,
                       value_bool, value_num, value_text,
                       raw jsonb, ingested_at, deleted_at)
```

Conventions that carry over for free:

- **Restatement audit**: `raw.log_restatement()` is generic — any table with
  `natural_key` and `raw` columns can attach the same trigger, so UI *edits*
  get the same diff audit trail external restatements do.
- **Idempotent upsert**: UI writes mint a `natural_key` (UUID at creation) and
  go through the same `ON CONFLICT ... DO UPDATE` shape as extractors.
- **Deletes**: the raw layer has no delete story (extractors never delete).
  User-entered data does need one — give the *new* tables a `deleted_at`
  tombstone from day one. No existing table needs it.
- **Reconciliation**: if some manual entry should participate in canonical
  resolution (e.g. a manually logged weight), it lands in `raw.samples` with
  `source = 'manual'` and wins via new `canonical.precedence_rules` *rows* —
  data, not schema.

Non-schema changes when the time comes (code only): the web pool becomes
writable (today it's `autocommit` read-only by construction, and connects as
the owning `anduin` role, which already has write privileges), plus POST
routes/forms. If least-privilege matters later, an `anduin_web` role with
INSERT/UPDATE on just the UI tables is one additive migration following the
`0009` pattern.

## Feature 2: outbound sync (push to external APIs)

**Pattern: a push ledger + per-destination push jobs**, mirroring the
extractor architecture in reverse. All additive:

```
sync.pushes (
    destination   text,         -- 'intervals'
    kind          text,         -- 'wellness_weight', ...
    record_key    text,         -- stable identity of the pushed record
    content_hash  text,         -- re-push when the source data restates
    external_id   text,         -- id assigned by the destination, if any
    pushed_at     timestamptz,
    PRIMARY KEY (destination, kind, record_key)
)
```

- New `sync` schema + table in a future migration (that migration also repeats
  the `0009` grant/default-privilege pair for the new schema).
- Push jobs read from the **canonical views**, whose row identities
  (`metric, valid_from`) are stable because the raw layer is
  idempotent-upsert-keyed — so the ledger has something durable to key on.
  `content_hash` catches restatements and triggers re-push.
- Deployment shape extends untouched: `anduin push <destination>` as another
  systemd oneshot+timer per INTEGRATION.md's `mkExtractor` pattern.
- Credentials: intervals.icu's API key is already read-write, so
  Google Health → intervals needs no new auth machinery. Pushing *to* an OAuth
  source someday means re-seeding that token with write scopes — a token-file
  concern (`/var/lib/anduin/state/`), never a DB migration.

**The echo problem** (theoretical for now, and it's code, not schema): a push
destination that is also an ingest source could feed pushed data back as if it
were independent. The planned sync doesn't hit this — it writes *health* data
to intervals.icu wellness, while the intervals extractor reads only
activities/streams, so pushed data is never read back. It only becomes real if
a future sync pushes into a category some extractor ingests. The fix then is
ingest-time: consult `sync.pushes` for the destination's `external_id`s and
skip (or tag) matching records. No existing table is touched either way.

## Changed now (pre-deployment)

- **`0009_readonly_grants.sql`** — creates `anduin_ro` if absent, grants
  USAGE + SELECT on `raw`/`canonical`, and sets `ALTER DEFAULT PRIVILEGES` so
  every future table in those schemas is readable automatically. Without this,
  `anduin_ro` (promised to future consumers by INTEGRATION.md) had zero
  privileges, and grants would have had to be retrofitted object-by-object on
  the live cluster.

Everything else is intentionally deferred: pre-creating medication/journal/
sync tables now would be speculative schema (YAGNI) with no migration-cost
benefit, since adding them later is exactly one clean append-only file each.

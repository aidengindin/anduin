-- Read-only role grants. INTEGRATION.md has the postgres container create an
-- `anduin_ro` role for future read-only consumers, but nothing ever granted it
-- privileges. Grants live here (not in NixOS) because new tables appear via
-- migrations in this repo, and ALTER DEFAULT PRIVILEGES makes every future
-- table readable by anduin_ro automatically -- no per-table grant to remember.
--
-- The role is created here if absent so migrations also run in dev/test
-- databases that never went through the NixOS ensureUsers path. In prod,
-- ensureUsers creates it first and this DO block is a no-op (and vice versa:
-- ensureUsers skips roles that already exist). The CREATE ROLE branch needs
-- CREATEROLE/superuser -- fine in dev (superuser); never reached in prod,
-- where the role already exists by the time migrations run as `anduin`.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'anduin_ro') THEN
        CREATE ROLE anduin_ro LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA raw, canonical TO anduin_ro;

-- ON ALL TABLES covers plain tables, views, and materialized views (the
-- continuous aggregates); reads through views run with view-owner privileges,
-- so SELECT on the canonical views is all a consumer needs.
GRANT SELECT ON ALL TABLES IN SCHEMA raw, canonical TO anduin_ro;

-- Future objects: default privileges are recorded per (grantor, schema) and
-- migrations always run as the owning `anduin` role, so tables added by later
-- migrations in these schemas are covered. A migration that creates a NEW
-- schema must repeat this pair for that schema.
ALTER DEFAULT PRIVILEGES IN SCHEMA raw GRANT SELECT ON TABLES TO anduin_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA canonical GRANT SELECT ON TABLES TO anduin_ro;

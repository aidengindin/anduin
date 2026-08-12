"""Sanity tests for the migration files (presence, ordering, non-empty)."""

from __future__ import annotations

from importlib import resources


def test_migrations_present_and_ordered():
    files = resources.files("anduin.migrations")
    names = sorted(p.name for p in files.iterdir() if p.name.endswith(".sql"))
    assert names, "no migration .sql files found"
    # Lex sort matches numeric order.
    assert names == sorted(names)
    for n in names:
        body = (files / n).read_text(encoding="utf-8").strip()
        assert body, f"migration {n} is empty"


def test_migrations_idempotent_markers():
    """All schema-creating statements should use IF NOT EXISTS / OR REPLACE."""
    files = resources.files("anduin.migrations")
    for p in files.iterdir():
        if not p.name.endswith(".sql"):
            continue
        text = (files / p.name).read_text(encoding="utf-8").lower()
        for stmt in ("create table ", "create index ", "create unique index "):
            for line in text.splitlines():
                if stmt in line and "if not exists" not in line:
                    raise AssertionError(
                        f"{p.name}: `{stmt.strip()}` without IF NOT EXISTS: {line.strip()!r}"
                    )


def test_activity_daily_uses_source_local_dates():
    files = resources.files("anduin.migrations")
    for name in ("0021_activity_daily.sql", "0022_activity_daily_materialized.sql"):
        text = (files / name).read_text(encoding="utf-8")
        assert "civilStartTime,date" in text
        assert "start_date_local" in text


def test_activity_daily_is_materialized_with_concurrent_refresh_index():
    """0022 must replace the 0021 view with a matview and keep the unique index
    REFRESH ... CONCURRENTLY depends on."""
    files = resources.files("anduin.migrations")
    text = (files / "0022_activity_daily_materialized.sql").read_text(encoding="utf-8")
    assert "DROP VIEW IF EXISTS canonical.activity_daily" in text
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS canonical.activity_daily" in text
    assert "CREATE UNIQUE INDEX IF NOT EXISTS activity_daily_metric_kind_date" in text
    assert "GRANT SELECT ON canonical.activity_daily TO anduin_ro" in text


def _sql(name: str) -> str:
    return (resources.files("anduin.migrations") / name).read_text(encoding="utf-8")


def test_workout_calories_fallback_order():
    """source -> work -> MET -> Fitbit. The order is the whole point of 0023:
    a power meter beats a wrist estimate, and the MET model beats Fitbit for
    lifting, which Fitbit cannot detect as resistance training at all."""
    text = _sql("0023_workout_calories.sql")
    assert "COALESCE(calories_source, calories_work, calories_met, calories_fitbit)" in text
    for col in ("calories_method", "calories_met", "calories_fitbit", "calories_work"):
        assert col in text, f"0023 must expose {col} for model comparison"


def test_workout_calories_work_conversion_is_metabolic_not_mechanical():
    """icu_joules / 4184 yields kcal of mechanical work and undercounts the
    metabolic cost ~4x; the ~24%-efficiency identity (1 kJ ~= 1 kcal) is what
    matches the calories intervals reports for the same rides."""
    text = _sql("0023_workout_calories.sql")
    assert "icu_joules')::double precision / 1000.0" in text
    body = text.split("CREATE OR REPLACE VIEW")[1]
    assert "4184" not in body, "the mechanical-work conversion is back in the view body"

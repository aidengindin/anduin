"""Sanity tests for the migration files (presence, ordering, non-empty)."""

from __future__ import annotations

import re
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


def test_goal_phases_are_append_only_with_a_tombstone_kind():
    """0025's table is the phase history the corridor and verdict read. The
    'none' kind ends a phase without starting a targeted one; the paired CHECK
    is what keeps a bulk from being saved with no target."""
    text = _sql("0025_weight_goals.sql")
    assert "CREATE TABLE IF NOT EXISTS identity.goals" in text
    assert "'bulk','cut','maintain','none'" in text.replace(", ", ",")
    assert "UNIQUE (user_id, started_on)" in text
    assert "target_lb_per_week IS NOT NULL" in text


def test_trend_view_fits_the_smoothed_series_not_raw_readings():
    """The whole point of 0025's view change: regressing avg_7d instead of raw
    weight is what stops the per-week number lurching with each weigh-in."""
    text = _sql("0025_weight_goals.sql")
    assert "CREATE OR REPLACE VIEW derived.body_composition_trend" in text
    assert "regr_slope(avg_7d" in text
    assert "avg_30d" in text


def test_smoothed_fit_uses_a_28_day_window():
    """Measured against synthetic bulks, 28d beats the 21d this was first
    written with on accuracy, stability AND time-to-settle -- a shorter window
    is too noisy to converge at all. Pinned by name so a stray `INTERVAL '28
    days'` elsewhere in the file cannot satisfy this by accident."""
    text = " ".join(_sql("0025_weight_goals.sql").split())
    assert "w_smooth AS (PARTITION BY metric ORDER BY valid_from RANGE BETWEEN INTERVAL '28 days' PRECEDING AND CURRENT ROW)" in text


def _outer_select_columns(view_body: str) -> list[str] | None:
    """Column names of a view body's outermost SELECT list."""
    body = re.sub(r"--[^\n]*", "", view_body)
    blocks = list(re.finditer(r"^SELECT\b(.*?)^FROM\b", body, re.M | re.S))
    if not blocks:
        return None
    items, depth, cur = [], 0, ""
    for ch in blocks[-1].group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    names = []
    for item in items:
        item = " ".join(item.split())
        if not item:
            continue
        alias = re.search(r"\bAS\s+([A-Za-z_]\w*)$", item, re.I)
        names.append(alias.group(1) if alias else item.split()[-1])
    return names


def test_replaced_views_only_append_columns():
    """CREATE OR REPLACE VIEW can only *append* columns -- Postgres refuses to
    rename, reorder or drop them. So a view redefined by a later migration must
    keep every earlier column in the same position.

    This is not theoretical. 0025 first shipped with `avg_30d` inserted beside
    `avg_7d`, which applied fine to a fresh database and failed on the live one
    with `cannot change name of view column "n_28d" to "avg_30d"`.
    """
    seen: dict[str, tuple[str, list[str]]] = {}
    files = resources.files("anduin.migrations")
    for name in sorted(p.name for p in files.iterdir() if p.name.endswith(".sql")):
        text = (files / name).read_text(encoding="utf-8")
        for m in re.finditer(r"CREATE OR REPLACE VIEW\s+([\w.]+)\s+AS(.*?);\s*$",
                             text, re.S | re.M):
            view, cols = m.group(1), _outer_select_columns(m.group(2))
            if cols is None:
                continue
            if view in seen:
                before_file, before = seen[view]
                assert cols[:len(before)] == before, (
                    f"{name}: {view} reorders or renames columns defined in "
                    f"{before_file}.\n  was: {before}\n  now: {cols}\n"
                    "CREATE OR REPLACE VIEW can only append; put new columns last."
                )
            seen[view] = (name, cols)

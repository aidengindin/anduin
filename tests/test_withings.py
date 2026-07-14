from __future__ import annotations

from anduin.sources.withings import _measure_rows


def _body(measures, grpid=42):
    return {
        "measuregrps": [
            {
                "grpid": grpid,
                "date": 1_700_000_000,
                "model": "Body+",
                "measures": measures,
            }
        ]
    }


def test_measure_rows_weight():
    body = _body([{"type": 1, "value": 72500, "unit": -3}])
    rows = _measure_rows(body)
    assert len(rows) == 1
    assert rows[0]["value"] == 72.5
    assert rows[0]["metric"] == "body_weight"
    assert rows[0]["unit"] == "kg"
    assert rows[0]["recording_method"] == "scale"
    # natural_key stays the grpid: the raw.samples conflict key already includes
    # `metric`, so distinct measures in one group never collide.
    assert rows[0]["natural_key"] == "42"


def test_measure_rows_body_composition_group():
    # One scale group carries weight + body-composition measures together.
    body = _body([
        {"type": 1, "value": 72500, "unit": -3},   # weight 72.5 kg
        {"type": 6, "value": 1834, "unit": -2},    # fat ratio 18.34 %
        {"type": 8, "value": 13300, "unit": -3},   # fat mass 13.3 kg
        {"type": 76, "value": 55200, "unit": -3},  # muscle mass 55.2 kg
        {"type": 88, "value": 3100, "unit": -3},   # bone mass 3.1 kg
        {"type": 77, "value": 42000, "unit": -3},  # hydration 42.0 kg
        {"type": 5, "value": 59200, "unit": -3},   # fat-free mass 59.2 kg
        {"type": 170, "value": 8, "unit": 0},      # visceral fat index 8
    ])
    by_metric = {r["metric"]: r for r in _measure_rows(body)}
    assert by_metric["body_fat_ratio"]["value"] == 18.34
    assert by_metric["body_fat_ratio"]["unit"] == "%"
    assert by_metric["fat_mass"]["value"] == 13.3
    assert by_metric["muscle_mass"]["value"] == 55.2
    assert by_metric["bone_mass"]["value"] == 3.1
    assert by_metric["hydration"]["value"] == 42.0
    assert by_metric["fat_free_mass"]["value"] == 59.2
    assert by_metric["visceral_fat"]["value"] == 8.0
    # every body-comp row shares the group's timestamp and grpid
    assert {r["natural_key"] for r in by_metric.values()} == {"42"}


def test_measure_rows_blood_pressure_group():
    # A BP monitor reading is one group with systolic + diastolic. The pulse
    # measure (type 11) is deliberately NOT ingested from Withings.
    body = _body([
        {"type": 10, "value": 118, "unit": 0},  # systolic
        {"type": 9, "value": 76, "unit": 0},    # diastolic
        {"type": 11, "value": 61, "unit": 0},   # heart pulse — must be ignored
    ], grpid=99)
    by_metric = {r["metric"]: r for r in _measure_rows(body)}
    assert "heart_pulse" not in by_metric
    assert by_metric["blood_pressure_systolic"]["value"] == 118.0
    assert by_metric["blood_pressure_systolic"]["unit"] == "mmHg"
    assert by_metric["blood_pressure_systolic"]["recording_method"] == "bp_monitor"
    assert by_metric["blood_pressure_diastolic"]["value"] == 76.0


def test_measure_rows_skips_malformed_measure():
    body = _body([
        {"type": 1, "value": 72500, "unit": -3},
        {"type": 6, "unit": -2},  # missing 'value'
    ])
    rows = _measure_rows(body)
    assert len(rows) == 1
    assert rows[0]["metric"] == "body_weight"


def test_measure_rows_ignores_unmapped_type():
    body = _body([{"type": 4, "value": 180, "unit": -2}])  # height, not mapped
    assert _measure_rows(body) == []

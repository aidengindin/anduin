from __future__ import annotations

from anduin.sources.withings import _weight_rows


def _body(measures):
    return {
        "measuregrps": [
            {
                "grpid": 42,
                "date": 1_700_000_000,
                "model": "Body+",
                "measures": measures,
            }
        ]
    }


def test_weight_rows_valid_measure():
    body = _body([{"type": 1, "value": 72500, "unit": -3}])
    rows = _weight_rows(body)
    assert len(rows) == 1
    assert rows[0]["value"] == 72.5
    assert rows[0]["natural_key"] == "42"
    assert rows[0]["metric"] == "body_weight"


def test_weight_rows_skips_malformed_measure():
    # One valid type-1 measure and one type-1 measure missing 'value':
    # only the valid one should produce a row, and no exception should escape.
    body = _body(
        [
            {"type": 1, "value": 72500, "unit": -3},
            {"type": 1, "unit": -3},  # missing 'value'
        ]
    )
    rows = _weight_rows(body)
    assert len(rows) == 1
    assert rows[0]["value"] == 72.5


def test_weight_rows_skips_missing_unit():
    body = _body([{"type": 1, "value": 72500}])  # missing 'unit'
    assert _weight_rows(body) == []


def test_weight_rows_ignores_non_weight_type():
    body = _body([{"type": 4, "value": 180, "unit": -2}])  # height, not weight
    assert _weight_rows(body) == []

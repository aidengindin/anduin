"""Display-filter tests for the web templating layer."""

from __future__ import annotations

from anduin.sources.liftohistory import _to_kg
from anduin.web.templating import _fmt_lb


def test_fmt_lb_round_trips_liftosaur_weights_exactly():
    # Weights are logged in lb, stored in kg; the display must land back on the
    # plate number that was logged, not 119.9 / 120.00000001.
    for lb in (120.0, 100.0, 45.0, 7.5, 2.5, 22.5, 52.5):
        assert _fmt_lb(_to_kg(lb, "lb")) == (f"{lb:,.0f}" if lb == int(lb) else f"{lb:,.1f}")


def test_fmt_lb_drops_trailing_zero_and_handles_none():
    assert _fmt_lb(54.43108) == "120"
    assert _fmt_lb(3.4019) == "7.5"
    assert _fmt_lb(0) == "0"
    assert _fmt_lb(None) == "—"


def test_fmt_lb_groups_thousands():
    assert _fmt_lb(_to_kg(1000.0, "lb")) == "1,000"

"""Tests for pressure-informed touch compensation (pure core)."""

from src.core.touch_compensation import (
    TouchCompensator,
    compensator_from_config,
    coupling_to_config,
)


def _comp(**kw):
    # Chamber 0 strongly moves sensors 0 and 1 (irregular: different amounts);
    # chamber 1 moves sensor 2. Measured at full inflation (ref_pct 100).
    deltas = {0: [200.0, 80.0, 0.0, 0.0], 1: [0.0, 0.0, 150.0, 0.0]}
    kw.setdefault("sensor_count", 4)
    kw.setdefault("threshold_ut", 100.0)
    return TouchCompensator(deltas, **kw)


def test_no_inflation_passes_through():
    comp = _comp()
    mag, act = comp.compensate([10, 10, 10, 10], {0: 0.0, 1: 0.0})
    assert mag == [10, 10, 10, 10]
    assert act == []


def test_offset_scales_with_level_and_clamps_at_zero():
    comp = _comp()
    # Chamber 0 at 50 % → expected offset is half the full delta.
    mag, _ = comp.compensate([120, 50, 5, 5], {0: 50.0, 1: 0.0})
    assert mag[0] == 120 - 100   # offset 200 * 0.5 = 100 → residual 20
    assert mag[1] == 10.0        # offset 80 * 0.5 = 40 → residual 10
    assert mag[2] == 5.0         # untouched by chamber 0


def test_actuation_does_not_fire_a_false_touch():
    comp = _comp()
    # Chamber 0 fully inflated, no real touch: raw mag ~ the coupling offset.
    mag, act = comp.compensate([205, 82, 8, 8], {0: 100.0, 1: 0.0})
    assert act == []             # residuals stay below threshold
    assert mag[0] < 100 and mag[1] < 100


def test_real_touch_survives_compensation():
    comp = _comp()
    # Chamber 0 inflated AND a real press adds 300 uT on sensor 0.
    mag, act = comp.compensate([505, 82, 8, 8], {0: 100.0, 1: 0.0})
    assert 0 in act              # 505 - 200 = 305 >= 100
    assert 1 not in act


def test_irregular_two_sensor_coupling():
    comp = _comp()
    # One chamber moving two sensors is handled by the full matrix.
    offset = comp.expected_offset({0: 100.0, 1: 0.0})
    assert offset[0] == 200.0 and offset[1] == 80.0 and offset[2] == 0.0


def test_suppress_above_level_blanks_strongly_coupled_sensors():
    comp = _comp(suppress_pct=90.0)
    # At/above 90 %, sensors strongly coupled to chamber 0 are blanked entirely,
    # even if a finger is pressing (last-resort "ignore while inflated").
    mag, act = comp.compensate([900, 900, 8, 8], {0: 95.0, 1: 0.0})
    assert mag[0] == 0.0 and mag[1] == 0.0
    assert 0 not in act and 1 not in act
    # sensor 2 (coupled to chamber 1, which is at rest) is unaffected.
    assert mag[2] == 8.0


def test_apply_passthrough_when_empty():
    comp = TouchCompensator({}, sensor_count=4)
    data = {"type": "magnet", "mag": [1, 2, 3, 4], "act": [2]}
    assert comp.apply(data, {0: 100.0}) == data   # unchanged


def test_apply_replaces_mag_and_act():
    comp = _comp()
    data = {"type": "magnet", "mag": [205, 82, 8, 8], "act": [0, 1]}
    out = comp.apply(data, {0: 100.0, 1: 0.0})
    assert out["act"] == []
    assert out["compensated"] is True
    assert data["act"] == [0, 1]   # original not mutated


def test_config_round_trip():
    cfg = coupling_to_config({0: [200.0, 80.0, 0.0, 0.0]}, sensor_count=4, ref_pct=100)
    touch = {"coupling": cfg, "compensation": {"enabled": True, "threshold_ut": 100}}
    comp = compensator_from_config(touch)
    assert comp is not None
    assert comp.expected_offset({0: 100.0})[0] == 200.0


def test_config_disabled_returns_none():
    cfg = coupling_to_config({0: [200.0]}, sensor_count=1)
    assert compensator_from_config({"coupling": cfg}) is None                 # no tuning
    assert compensator_from_config(
        {"coupling": cfg, "compensation": {"enabled": False}}) is None        # disabled
    assert compensator_from_config(None) is None

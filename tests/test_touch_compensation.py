"""Tests for pressure-informed touch compensation (pure core, grid model)."""

from src.core.touch_compensation import (
    CouplingState,
    GridCompensation,
    TouchCompensator,
    TransitionGuard,
    compensator_from_config,
    coupling_to_config,
)
from src.core.touch_coupling import CouplingModel, MeasuredState


def _grid(states, n=4):
    return GridCompensation(
        [CouplingState(frozenset(ch), lv, mag, vec)
         for ch, lv, mag, vec in states], n)


def _comp(states=None, n=4, **kw):
    # Chamber 0 strongly moves sensors 0 and 1 (irregular: different amounts);
    # chamber 1 moves sensor 2 — each measured alone at full inflation.
    if states is None:
        states = [
            ((0,), {0: 100.0}, [200.0, 80.0, 0.0, 0.0], None),
            ((1,), {1: 100.0}, [0.0, 0.0, 150.0, 0.0], None),
        ]
    kw.setdefault("threshold_ut", 100.0)
    return TouchCompensator(_grid(states, n), sensor_count=n, **kw)


def test_no_inflation_passes_through():
    comp = _comp()
    mag, act = comp.compensate([10, 10, 10, 10], {0: 0.0, 1: 0.0})
    assert mag == [10, 10, 10, 10]
    assert act == []


def test_offset_scales_with_level_and_clamps_at_zero():
    comp = _comp()
    # Chamber 0 at 50 %: interpolated from the origin → half the full delta.
    mag, _ = comp.compensate([120, 50, 5, 5], {0: 50.0, 1: 0.0})
    assert mag[0] == 120 - 100   # offset 200 * 0.5 = 100 → residual 20
    assert mag[1] == 10.0        # offset 80 * 0.5 = 40 → residual 10
    assert mag[2] == 5.0         # untouched by chamber 0


def test_actuation_does_not_fire_a_false_touch():
    comp = _comp()
    mag, act = comp.compensate([205, 82, 8, 8], {0: 100.0, 1: 0.0})
    assert act == []
    assert mag[0] < 100 and mag[1] < 100


def test_real_touch_survives_compensation():
    comp = _comp()
    mag, act = comp.compensate([505, 82, 8, 8], {0: 100.0, 1: 0.0})
    assert 0 in act              # 505 - 200 = 305 >= 100
    assert 1 not in act


def test_irregular_two_sensor_coupling():
    comp = _comp()
    offset = comp.expected_offset({0: 100.0, 1: 0.0})
    assert offset[0] == 200.0 and offset[1] == 80.0 and offset[2] == 0.0


def test_suppress_above_level_blanks_strongly_coupled_sensors():
    comp = _comp(suppress_pct=90.0)
    mag, act = comp.compensate([900, 900, 8, 8], {0: 95.0, 1: 0.0})
    assert mag[0] == 0.0 and mag[1] == 0.0
    assert 0 not in act and 1 not in act
    assert mag[2] == 8.0         # coupled to chamber 1, which is at rest


def test_apply_passthrough_when_empty():
    comp = TouchCompensator(_grid([], 4), sensor_count=4)
    data = {"type": "magnet", "mag": [1, 2, 3, 4], "act": [2]}
    assert comp.apply(data, {0: 100.0}) == data   # unchanged


def test_apply_replaces_mag_and_act():
    comp = _comp()
    data = {"type": "magnet", "mag": [205, 82, 8, 8], "act": [0, 1]}
    out = comp.apply(data, {0: 100.0, 1: 0.0})
    assert out["act"] == []
    assert out["compensated"] is True
    assert data["act"] == [0, 1]   # original not mutated


# --- combinations: the non-additive core --------------------------------------

def test_combination_is_non_additive():
    # Each chamber alone moves one sensor by 100; together they move it by 300
    # (silicone deforms non-additively) — the measured combo must win over sum.
    comp = _comp(states=[
        ((0,), {0: 100.0}, [100.0, 0.0, 0.0, 0.0], None),
        ((1,), {1: 100.0}, [0.0, 100.0, 0.0, 0.0], None),
        ((0, 1), {0: 100.0, 1: 100.0}, [300.0, 300.0, 0.0, 0.0], None),
    ])
    assert comp.expected_offset({0: 100.0, 1: 100.0})[:2] == [300.0, 300.0]
    # A single chamber still reads its own single-chamber corner.
    assert comp.expected_offset({0: 100.0, 1: 0.0})[0] == 100.0


def test_combination_interpolates_joint_levels():
    # A 2x2 pair grid; a query at (75, 75) is the bilinear blend of the corners.
    comp = _comp(states=[
        ((0, 1), {0: 50.0, 1: 50.0}, [10.0, 0.0, 0.0, 0.0], None),
        ((0, 1), {0: 50.0, 1: 100.0}, [30.0, 0.0, 0.0, 0.0], None),
        ((0, 1), {0: 100.0, 1: 50.0}, [50.0, 0.0, 0.0, 0.0], None),
        ((0, 1), {0: 100.0, 1: 100.0}, [90.0, 0.0, 0.0, 0.0], None),
    ])
    # bilinear at t=0.5 each: (10+30+50+90)/4 = 45.
    assert abs(comp.expected_offset({0: 75.0, 1: 75.0})[0] - 45.0) < 1e-9
    # On a grid edge (member 1 fully): linear between (50,100)=30 and (100,100)=90.
    assert abs(comp.expected_offset({0: 75.0, 1: 100.0})[0] - 60.0) < 1e-9


def test_combo_without_its_singles_does_not_recurse():
    # A chamber measured only in combination (never alone) leaves its single
    # corner missing; querying it must not infinitely recurse (live crash).
    comp = _comp(states=[
        ((1, 2), {1: 24.0, 2: 100.0}, [1.0, 2.0, 3.0, 4.0], None),
    ])
    # exact measured combo corner
    assert comp.expected_offset({1: 24.0, 2: 100.0})[2] == 3.0
    # a single chamber that was never measured alone → no data → zero, no crash
    assert comp.expected_offset({2: 100.0}) == [0.0, 0.0, 0.0, 0.0]
    assert comp.expected_offset({1: 24.0, 2: 50.0})[2] >= 0.0   # interpolates, no crash


def test_missing_combo_falls_back_to_additive():
    # Only singles measured: an unmeasured co-inflation degrades to their sum.
    comp = _comp(states=[
        ((0,), {0: 100.0}, [100.0, 0.0, 0.0, 0.0], None),
        ((1,), {1: 100.0}, [0.0, 100.0, 0.0, 0.0], None),
    ])
    off = comp.expected_offset({0: 100.0, 1: 100.0})
    assert off[0] == 100.0 and off[1] == 100.0


# --- margin, guard, vector mode ----------------------------------------------

def test_margin_raises_threshold_with_applied_offset():
    comp = _comp(margin_frac=0.5)
    mag, act = comp.compensate([350, 5, 5, 5], {0: 100.0, 1: 0.0})
    assert mag[0] == 150.0 and act == []          # over base, under margin
    _, act = comp.compensate([450, 5, 5, 5], {0: 100.0, 1: 0.0})
    assert 0 in act                                # clears the margin too
    _, act = comp.compensate([5, 5, 5, 120], {0: 100.0, 1: 0.0})
    assert 3 in act                                # uncoupled sensor: no margin


def test_transition_guard_hardens_then_relaxes():
    comp = _comp(guard=TransitionGuard(settle_ms=800.0, level_eps=3.0))
    comp.compensate([5, 5, 5, 5], {0: 0.0, 1: 0.0}, now_ms=0.0)   # anchor refs
    mag, act = comp.compensate([480, 5, 5, 5], {0: 100.0, 1: 0.0}, now_ms=1000.0)
    assert mag[0] == 280.0 and act == []           # 280 < 100 + 200 boost
    _, act = comp.compensate([480, 5, 5, 5], {0: 100.0, 1: 0.0}, now_ms=2000.0)
    assert 0 in act


def test_curve_interpolates_between_measured_levels():
    comp = _comp(n=1, threshold_ut=50.0, states=[
        ((0,), {0: 50.0}, [100.0], None),
        ((0,), {0: 100.0}, [120.0], None),
    ])
    assert comp.expected_offset({0: 25.0})[0] == 50.0     # origin → first point
    assert comp.expected_offset({0: 50.0})[0] == 100.0
    assert comp.expected_offset({0: 75.0})[0] == 110.0    # between points
    assert comp.expected_offset({0: 100.0})[0] == 120.0
    # Above the top measured level the curve clamps (no risky extrapolation).
    assert comp.expected_offset({0: 110.0})[0] == 120.0


def test_vector_mode_separates_touch_from_actuation():
    comp = _comp(n=1, threshold_ut=100.0, states=[
        ((0,), {0: 100.0}, [200.0], [[200.0, 0.0, 0.0]]),
    ])
    assert comp.has_vector
    mag, act = comp.compensate([200.0], {0: 100.0}, vec=[[200.0, 0.0, 0.0]])
    assert mag == [0.0] and act == []              # ghost (actuation only)
    _, act = comp.compensate([250.0], {0: 100.0})
    assert act == []                               # scalar path misses it
    mag, act = comp.compensate([250.0], {0: 100.0}, vec=[[200.0, 0.0, 150.0]])
    assert act == [0] and abs(mag[0] - 150.0) < 1e-9   # vector residual catches it


# --- config (de)serialisation -------------------------------------------------

def _model(states, sensor_count, baseline=None, baseline_vec=None):
    return CouplingModel(
        sensor_count=sensor_count,
        states=[MeasuredState(frozenset(ch), lv, mag, vec, 5)
                for ch, lv, mag, vec in states],
        baseline=baseline or [0.0] * sensor_count,
        baseline_vec=baseline_vec)


def test_config_round_trip():
    cfg = coupling_to_config(_model(
        [((0,), {0: 100.0}, [200.0, 80.0, 0.0, 0.0], None)], 4))
    touch = {"coupling": cfg, "compensation": {"enabled": True, "threshold_ut": 100}}
    comp = compensator_from_config(touch)
    assert comp is not None
    assert comp.expected_offset({0: 100.0})[0] == 200.0


def test_config_round_trip_with_combos_margin_and_guard():
    cfg = coupling_to_config(_model([
        ((0,), {0: 100.0}, [100.0], None),
        ((1,), {1: 100.0}, [100.0], None),
        ((0, 1), {0: 100.0, 1: 100.0}, [300.0], None),
    ], 1))
    comp = compensator_from_config({
        "coupling": cfg,
        "compensation": {"enabled": True, "threshold_ut": 100.0,
                         "margin_frac": 0.25, "guard_ms": 800.0},
    })
    assert comp is not None
    assert comp.margin_frac == 0.25 and comp.guard is not None
    # The stored combo survives the round trip (non-additive, not 200).
    assert comp.expected_offset({0: 100.0, 1: 100.0})[0] == 300.0


def test_config_round_trip_with_vectors():
    cfg = coupling_to_config(_model(
        [((0,), {0: 100.0}, [200.0], [[200.0, 0.0, 0.0]])], 1,
        baseline_vec=[[0.0, 0.0, 0.0]]))
    comp = compensator_from_config(
        {"coupling": cfg, "compensation": {"enabled": True}})
    assert comp is not None and comp.has_vector


def test_config_disabled_returns_none():
    cfg = coupling_to_config(_model([((0,), {0: 100.0}, [200.0], None)], 1))
    assert compensator_from_config({"coupling": cfg}) is None                 # no tuning
    assert compensator_from_config(
        {"coupling": cfg, "compensation": {"enabled": False}}) is None        # disabled
    assert compensator_from_config(None) is None


def test_config_without_new_keys_keeps_defaults():
    cfg = coupling_to_config(_model([((0,), {0: 100.0}, [200.0], None)], 1))
    comp = compensator_from_config(
        {"coupling": cfg, "compensation": {"enabled": True}})
    assert comp is not None
    assert comp.margin_frac == 0.0 and comp.guard is None
    assert not comp.has_vector

"""Tests for the leak-compensation calibration cores and plumbing.

Covers the Qt-free hold-servo stability detector and leak-rate sampler, the
hold/leak curve storage (per chamber + per-type templates + resolver), the
curve interpolation helper, and Skin.hold_regulated seeding the controller.
"""

import math

from src.hardware.fill_calibration import (
    HoldStabilityDetector,
    LeakRateSampler,
    get_type_hold_curve,
    resolve_fill_profiles,
    set_hold_curve,
    set_leak_curve,
    set_type_hold_curve,
    set_type_leak_curve,
)
from src.hardware.fill_scaling import FillLoadTracker, interp_curve
from src.hardware.hold_duty import HOLD_DUTY_MIN, seed_hold_duty
from src.hardware.skin import Skin


# ---------------------------------------------------------------------------
# HoldStabilityDetector
# ---------------------------------------------------------------------------

def test_stability_declared_after_settle_in_band():
    det = HoldStabilityDetector(band=0.2, settle_ms=1000, min_ms=500)
    assert not det.update(0, 5.0)
    assert not det.update(400, 5.1)      # in band, before settle
    assert det.update(1100, 4.9)         # in band for >= settle_ms


def test_excursion_restarts_the_clock():
    det = HoldStabilityDetector(band=0.2, settle_ms=1000, min_ms=0)
    det.update(0, 5.0)
    det.update(900, 5.6)                 # out of band -> re-anchor at 5.6
    assert not det.update(1200, 5.6)     # only 300 ms since re-anchor
    assert det.update(1950, 5.7)


def test_nan_reading_never_settles():
    det = HoldStabilityDetector(band=0.2, settle_ms=500, min_ms=0)
    det.update(0, float("nan"))
    assert not det.update(1000, float("nan"))


# ---------------------------------------------------------------------------
# LeakRateSampler
# ---------------------------------------------------------------------------

def test_leak_rate_positive_for_falling_reading():
    s = LeakRateSampler(duration_ms=2000)
    for t in range(0, 2001, 200):
        s.record(t, 10.0 - 0.5 * t / 1000.0)   # falling 0.5 kPa/s
    assert s.done
    assert abs(s.rate_per_s - 0.5) < 1e-6


def test_leak_rate_nan_with_too_few_points():
    s = LeakRateSampler(duration_ms=1000)
    s.record(1000, 5.0)
    assert math.isnan(s.rate_per_s)


# ---------------------------------------------------------------------------
# interp_curve
# ---------------------------------------------------------------------------

def test_interp_curve_interpolates_and_clamps():
    curve = [[0.0, 40], [10.0, 120]]
    assert interp_curve(curve, 5.0) == 80.0
    assert interp_curve(curve, -3.0) == 40.0    # clamped below
    assert interp_curve(curve, 99.0) == 120.0   # clamped above
    assert interp_curve(None, 5.0) is None
    assert interp_curve([], 5.0) is None


# ---------------------------------------------------------------------------
# Storage + resolver
# ---------------------------------------------------------------------------

def _settings_data():
    return {"robots": {"turtles": [{
        "id": "turtle",
        "nodes": [{"mac": "AA:01", "node_type": "node_multiplexed"}],
        "skins": [{"skin_id": "shell", "skin_type": "turtle_round",
                   "skin_variant": "natural",
                   "chambers": [{"mac": "AA:01", "slot": 0,
                                 "max_pressure": 8.0}]}],
    }]}}


def test_hold_and_leak_curves_round_trip_per_chamber():
    data = _settings_data()
    assert set_hold_curve(data, "AA:01", 0, [[2.0, 60], [6.0, 110]]) == 1
    assert set_leak_curve(data, "AA:01", 0, [[2.0, 0.1], [6.0, 0.4]]) == 1
    ch = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    assert ch["hold_duty_curve"] == [[2.0, 60], [6.0, 110]]
    assert ch["leak_curve"] == [[2.0, 0.1], [6.0, 0.4]]
    assert set_hold_curve(data, "AA:01", 0, None) == 1
    assert "hold_duty_curve" not in ch


def test_type_templates_resolve_onto_untyped_chambers():
    data = _settings_data()
    set_type_hold_curve(data, "turtle_round", "natural", 0, [[3.0, 70]])
    set_type_leak_curve(data, "turtle_round", "natural", 0, [[3.0, 0.2]])
    assert get_type_hold_curve(data, "turtle_round", "natural", 0) == [[3.0, 70]]
    skins = resolve_fill_profiles(data, data["robots"]["turtles"][0]["skins"])
    ch = skins[0]["chambers"][0]
    assert ch["hold_duty_curve"] == [[3.0, 70]]
    assert ch["leak_curve"] == [[3.0, 0.2]]
    # The stored config was not mutated (copy-on-write resolve).
    stored = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    assert "hold_duty_curve" not in stored


# ---------------------------------------------------------------------------
# Skin.hold_regulated
# ---------------------------------------------------------------------------

class _HoldCtrl:
    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=1)
        self.hold_calls: list[dict] = []
        self.stop_calls: list[int | None] = []

    def on_pressure(self, _cb) -> None:
        pass

    def start_hold(self, chamber, duty, kpa=None, timed=False) -> bool:
        self.hold_calls.append({"chamber": chamber, "duty": duty,
                                "kpa": kpa, "timed": timed})
        return True

    def stop_hold(self, chamber=None) -> None:
        self.stop_calls.append(chamber)

    def hold(self, chamber) -> bool:
        return True

    # Plain actuation stubs so the auto-hold tests can drive a Skin.
    def inflate(self, chamber, delta=10, **kw) -> bool:
        return True

    def deflate(self, chamber, delta=10, **kw) -> bool:
        return True

    def set_pressure(self, chamber, value, **kw) -> bool:
        return True


def test_seed_hold_duty_clamps_to_floor():
    assert seed_hold_duty(None, 5.0) == HOLD_DUTY_MIN
    assert seed_hold_duty([[0.0, 40], [10.0, 140]], 5.0) == HOLD_DUTY_MIN
    assert seed_hold_duty([[0.0, 140], [10.0, 200]], 5.0) == HOLD_DUTY_MIN   # 170 < floor
    assert seed_hold_duty([[0.0, 160], [10.0, 220]], 5.0) == 190
    assert seed_hold_duty([[0.0, 300]], 1.0) == 255


def test_hold_regulated_seeds_duty_from_curve():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{
        "controller": ctrl, "node_slot": 0, "max_pressure": 10.0,
        "hold_duty_curve": [[0.0, 160], [10.0, 220]],
    }])
    assert skin.hold_regulated(0, pct=50)      # 50% of 0..10 kPa -> 5 kPa
    call = ctrl.hold_calls[-1]
    assert call["kpa"] == 5.0
    assert call["duty"] == 190                 # midpoint of 160..220
    assert call["timed"] is False


def test_hold_regulated_falls_back_without_curve_and_releases():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{"controller": ctrl, "node_slot": 3,
                           "max_pressure": 10.0}])
    assert skin.hold_regulated(0, pct=80)
    assert ctrl.hold_calls[-1]["duty"] == HOLD_DUTY_MIN
    skin.release_hold(0)
    assert ctrl.stop_calls == [3]              # node slot, not local index


# ---------------------------------------------------------------------------
# Automatic hold: a chamber that settles at a level is held there
# ---------------------------------------------------------------------------

def _status(skin, slot, kpa, st):
    """Feed one firmware status frame (st 0 idle / 1 inflating / 2 deflating)."""
    skin._on_pressure(slot, 0, st, kpa)


def test_auto_hold_engages_when_inflate_settles_and_releases_on_deflate():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{"controller": ctrl, "node_slot": 2,
                           "max_pressure": 10.0}])
    assert skin.set_pressure(0, 60)             # target 6 kPa
    assert ctrl.hold_calls == []                # nothing to hold yet
    _status(skin, 2, 3.0, 1)                    # inflating
    _status(skin, 2, 6.0, 0)                    # firmware idle at the level
    assert len(ctrl.hold_calls) == 1
    assert ctrl.hold_calls[0]["chamber"] == 2
    assert ctrl.hold_calls[0]["kpa"] == 6.0
    _status(skin, 2, 6.0, 0)                    # steady frames: no re-send
    assert len(ctrl.hold_calls) == 1

    assert skin.deflate(0, 100)                 # actuation drops the hold first
    assert ctrl.stop_calls == [2]
    _status(skin, 2, 2.0, 2)
    _status(skin, 2, 0.0, 0)                    # empty: nothing to hold
    assert len(ctrl.hold_calls) == 1


def test_auto_hold_immediate_when_already_at_target():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{"controller": ctrl, "node_slot": 0,
                           "max_pressure": 10.0}])
    _status(skin, 0, 5.0, 0)                    # sitting at 5 kPa, no target
    assert ctrl.hold_calls == []
    assert skin.set_pressure(0, 50)             # already there -> hold now
    assert len(ctrl.hold_calls) == 1
    assert ctrl.hold_calls[0]["kpa"] == 5.0


def test_auto_hold_skips_vacuum_and_sensorless():
    ctrl = _HoldCtrl()
    vac = Skin("wrinkles", [{"controller": ctrl, "node_slot": 0,
                             "min_pressure": -20.0, "max_pressure": 0.0}])
    assert vac.set_pressure(0, 50)              # -10 kPa: inflate-only engine
    _status(vac, 0, -15.0, 2)
    _status(vac, 0, -10.0, 0)
    assert ctrl.hold_calls == []

    blind = Skin("blind", [{"controller": ctrl, "node_slot": 1,
                            "max_pressure": 10.0, "fill_time_ms": 1000}],
                 pressure_sensors=False)
    assert blind.set_pressure(0, 50)
    _status(blind, 1, 0.0, 1)
    _status(blind, 1, 0.0, 0)
    assert ctrl.hold_calls == []


def test_pause_and_hold_release_auto_holds():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{"controller": ctrl, "node_slot": 4,
                           "max_pressure": 10.0}])
    skin.set_pressure(0, 40)
    _status(skin, 4, 1.0, 1)
    _status(skin, 4, 4.0, 0)
    assert len(ctrl.hold_calls) == 1
    skin.hold(0)                                # freeze = closed valves, no hold
    assert ctrl.stop_calls == [4]
    _status(skin, 4, 4.0, 0)                    # no transition: stays released
    assert len(ctrl.hold_calls) == 1


def test_hold_regulated_sensorless_is_duty_only():
    ctrl = _HoldCtrl()
    skin = Skin("shell", [{"controller": ctrl, "node_slot": 0,
                           "max_pressure": 10.0,
                           "hold_duty_curve": [[0.0, 50], [10.0, 50]]}],
                pressure_sensors=False)
    assert skin.hold_regulated(0, pct=50)
    assert ctrl.hold_calls[-1]["timed"] is True

"""Tests for Skin fill-mode selection: time- vs pressure-based inflate.

A calibrated chamber in ``time`` mode inflates by a calculated ``ms`` window; in
``pressure`` mode it ignores the curve and inflates closed-loop (no ``ms``), so
the firmware decides when to stop from the gauge sensor. A chamber with no curve
always falls back to pressure regardless of mode. An absolute ``set_pressure``
follows the same choice for an upward move.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingCtrl:
    """Minimal controller double recording inflate()/set_pressure() calls."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.inflate_calls: list[dict] = []
        self.set_pressure_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def inflate(self, chamber: int, delta: int = 10, ms=None) -> bool:
        self.inflate_calls.append({"chamber": chamber, "delta": delta, "ms": ms})
        return True


    def set_pressure(self, chamber: int, value: int, duty=None) -> bool:
        self.set_pressure_calls.append(
            {"chamber": chamber, "value": value, "duty": duty})
        return True


def _skin(fill_mode=None, with_profile=True):
    ctrl = _RecordingCtrl()
    inp = {
        "controller": ctrl,
        "node_slot": 0,
        "max_pressure": 8.0,
        "fill_profile": [[0, 0], [1000, 100]] if with_profile else None,
        "fill_mode": fill_mode,
    }
    return Skin("belly", [inp]), ctrl


def test_time_mode_inflates_by_ms_when_calibrated():
    skin, ctrl = _skin(fill_mode="time", with_profile=True)
    assert skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is not None
    assert ctrl.inflate_calls[-1]["ms"] > 0


def test_pressure_mode_ignores_profile_and_sends_no_ms():
    skin, ctrl = _skin(fill_mode="pressure", with_profile=True)
    assert skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is None


def test_default_mode_is_pressure():
    skin, ctrl = _skin(fill_mode=None, with_profile=True)
    skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is None


def test_no_profile_falls_back_to_pressure():
    skin, ctrl = _skin(fill_mode="time", with_profile=False)
    skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is None


def test_chamber_defs_always_serializes_the_fill_mode():
    pressure_skin, _ = _skin(fill_mode="pressure", with_profile=False)
    assert pressure_skin.chamber_defs[0]["fill_mode"] == "pressure"

    time_skin, _ = _skin(fill_mode="time", with_profile=False)
    assert time_skin.chamber_defs[0]["fill_mode"] == "time"

    default_skin, _ = _skin(fill_mode=None, with_profile=False)
    assert default_skin.chamber_defs[0]["fill_mode"] == "pressure"


def test_set_pressure_in_time_mode_inflates_by_ms():
    skin, ctrl = _skin(fill_mode="time", with_profile=True)
    assert skin.set_pressure(0, 60)
    assert ctrl.inflate_calls[-1]["delta"] == 60
    assert ctrl.inflate_calls[-1]["ms"] > 0
    assert not ctrl.set_pressure_calls


def test_set_pressure_in_pressure_mode_stays_closed_loop():
    skin, ctrl = _skin(fill_mode="pressure", with_profile=True)
    assert skin.set_pressure(0, 60)
    assert ctrl.set_pressure_calls[-1]["value"] == 60
    assert not ctrl.inflate_calls


def test_set_pressure_in_time_mode_deflates_and_slow_strokes_closed_loop():
    skin, ctrl = _skin(fill_mode="time", with_profile=True)
    skin.set_pressure(0, 60)
    skin.set_pressure(0, 20)                    # downward: always the gauge
    assert ctrl.set_pressure_calls[-1]["value"] == 20
    skin.set_pressure(0, 80, duty=120)          # slowed stroke: the gauge too
    assert ctrl.set_pressure_calls[-1]["value"] == 80
    assert len(ctrl.inflate_calls) == 1

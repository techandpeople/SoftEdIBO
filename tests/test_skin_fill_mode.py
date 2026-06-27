"""Tests for Skin fill-mode selection: time- vs pressure-based inflate.

A calibrated chamber in ``time`` mode inflates by a calculated ``ms`` window; in
``pressure`` mode it ignores the curve and inflates closed-loop (no ``ms``), so
the firmware decides when to stop from the gauge sensor. A chamber with no curve
always falls back to pressure regardless of mode.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingCtrl:
    """Minimal controller double recording inflate() calls."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.inflate_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def inflate(self, chamber: int, delta: int = 10, ms=None) -> bool:
        self.inflate_calls.append({"chamber": chamber, "delta": delta, "ms": ms})
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


def test_default_mode_is_time():
    skin, ctrl = _skin(fill_mode=None, with_profile=True)
    skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is not None


def test_no_profile_falls_back_to_pressure():
    skin, ctrl = _skin(fill_mode="time", with_profile=False)
    skin.inflate(0, 50)
    assert ctrl.inflate_calls[-1]["ms"] is None


def test_chamber_defs_serializes_pressure_mode_only():
    pressure_skin, _ = _skin(fill_mode="pressure", with_profile=False)
    assert pressure_skin.chamber_defs[0]["fill_mode"] == "pressure"

    time_skin, _ = _skin(fill_mode="time", with_profile=False)
    assert "fill_mode" not in time_skin.chamber_defs[0]

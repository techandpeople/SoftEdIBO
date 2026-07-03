"""Tests for Skin deflate timing: a target at/below the measured deflate floor
gets an open-time budget (``ms``) from the calibrated falling curve, because the
gauge can't supervise the pull there; targets comfortably above the floor stay
purely closed-loop (no ``ms``)."""

from src.hardware.fill_profile import MAX_DEFLATE_MS
from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingCtrl:
    """Minimal controller double recording deflate() calls."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.deflate_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def deflate(self, chamber: int, delta: int = 10, ms=None) -> bool:
        self.deflate_calls.append({"chamber": chamber, "delta": delta, "ms": ms})
        return True


def _skin(with_deflate_profile=True):
    ctrl = _RecordingCtrl()
    inp = {
        "controller": ctrl,
        "node_slot": 0,
        "max_pressure": 8.0,
        # Falling curve: full → measured floor at ~6 % (ambient on this gauge).
        "deflate_profile": [[0, 100], [1000, 50], [3000, 6]]
        if with_deflate_profile else None,
    }
    return Skin("belly", [inp]), ctrl


def test_deflate_to_floor_sends_time_budget():
    skin, ctrl = _skin(with_deflate_profile=True)
    skin.chambers[0].target_pressure = 80
    skin.chambers[0].pressure = 80
    assert skin.deflate(0, 80)                    # target 0 — below the 6 % floor
    ms = ctrl.deflate_calls[-1]["ms"]
    assert ms is not None and 0 < ms <= MAX_DEFLATE_MS


def test_deflate_above_floor_stays_closed_loop():
    skin, ctrl = _skin(with_deflate_profile=True)
    skin.chambers[0].target_pressure = 80
    skin.chambers[0].pressure = 80
    assert skin.deflate(0, 30)                    # target 50 — well above the floor
    assert ctrl.deflate_calls[-1]["ms"] is None


def test_deflate_without_curve_sends_no_budget():
    skin, ctrl = _skin(with_deflate_profile=False)
    skin.chambers[0].target_pressure = 80
    skin.chambers[0].pressure = 80
    assert skin.deflate(0, 80)
    assert ctrl.deflate_calls[-1]["ms"] is None

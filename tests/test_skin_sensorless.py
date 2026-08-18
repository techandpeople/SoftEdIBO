"""Tests for the sensorless (no pressure sensors populated) open-loop path.

A node flagged ``pressure_sensors: false`` has floating ADC pins, so the Skin
must never trust telemetry pressure and every actuation goes out with a manual
time window plus ``timed=True`` (the firmware then ignores its gauge entirely).
Deflate scales the manual full-empty window by the requested delta; without a
manual time the move is refused instead of pulsing blind.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingCtrl:
    """Minimal controller double recording inflate()/deflate() calls."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.inflate_calls: list[dict] = []
        self.deflate_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def inflate(self, chamber, delta=10, ms=None, timed=False) -> bool:
        self.inflate_calls.append({"chamber": chamber, "delta": delta,
                                   "ms": ms, "timed": timed})
        return True

    def deflate(self, chamber, delta=10, ms=None, timed=False) -> bool:
        self.deflate_calls.append({"chamber": chamber, "delta": delta,
                                   "ms": ms, "timed": timed})
        return True


def _skin(fill_time_ms: int | None = 1000, empty_time_ms: int | None = 2000,
          **skin_kwargs):
    ctrl = _RecordingCtrl()
    inp = {
        "controller": ctrl,
        "node_slot": 0,
        "max_pressure": 8.0,
        "fill_time_ms": fill_time_ms,
        "empty_time_ms": empty_time_ms,
    }
    return Skin("belly", [inp], pressure_sensors=False, **skin_kwargs), ctrl


def test_inflate_sends_timed_window():
    skin, ctrl = _skin()
    assert skin.inflate(0, 50)
    call = ctrl.inflate_calls[-1]
    assert call["timed"] is True
    assert call["ms"] == 500          # 50% of the 1000 ms full-fill window


def test_inflate_without_manual_time_is_refused():
    skin, ctrl = _skin(fill_time_ms=None)
    assert not skin.inflate(0, 50)
    assert not ctrl.inflate_calls


def test_deflate_sends_timed_window_scaled_by_delta():
    skin, ctrl = _skin()
    skin.inflate(0, 60)               # estimate now at 60%
    assert skin.deflate(0, 30)
    call = ctrl.deflate_calls[-1]
    assert call["timed"] is True
    assert call["ms"] == 600          # 30% of the 2000 ms full-empty window


def test_deflate_without_manual_time_is_refused():
    skin, ctrl = _skin(empty_time_ms=None)
    skin.inflate(0, 60)
    assert not skin.deflate(0, 30)
    assert not ctrl.deflate_calls


def test_deflate_from_zero_estimate_is_noop():
    skin, ctrl = _skin()
    assert skin.deflate(0, 30)        # nothing to pull - trivially true
    assert not ctrl.deflate_calls


def test_telemetry_noise_does_not_touch_the_estimate():
    skin, ctrl = _skin()
    skin.inflate(0, 50)
    chamber = skin.chambers[0]
    assert chamber.pressure == 50     # open-loop estimate
    # Floating-pin noise arrives via status - must NOT move the estimate.
    skin._on_pressure(0, 93, state=0, kpa=7.4)
    assert chamber.pressure == 50


def test_set_pressure_decomposes_into_timed_moves():
    skin, ctrl = _skin()
    assert skin.set_pressure(0, 80)
    call = ctrl.inflate_calls[-1]
    assert call["timed"] is True and call["ms"] == 800
    assert skin.set_pressure(0, 30)
    call = ctrl.deflate_calls[-1]
    assert call["timed"] is True and call["ms"] == 1000   # 50% of 2000 ms

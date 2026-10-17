"""Tests for Skin deflate to the bottom: the gauge cannot read below ambient,
so a deflate (or set_pressure) to 0 % carries the chamber's blind vacuum time
(``vac_ms``) - the node pulls to ambient on the sensor, then that long blind.
Targets above 0 % stay purely closed-loop (no ``vac_ms``)."""

from src.core.skin_config import DEFAULT_VACUUM_MS
from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingCtrl:
    """Minimal controller double recording deflate()/set_pressure() calls."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.deflate_calls: list[dict] = []
        self.set_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def deflate(self, chamber: int, delta: int = 10, vac_ms=None) -> bool:
        self.deflate_calls.append({"chamber": chamber, "delta": delta,
                                   "vac_ms": vac_ms})
        return True

    def set_pressure(self, chamber: int, value: int, vac_ms=None) -> bool:
        self.set_calls.append({"chamber": chamber, "value": value,
                               "vac_ms": vac_ms})
        return True


def _skin(**extra):
    ctrl = _RecordingCtrl()
    inp = {"controller": ctrl, "node_slot": 0, "max_pressure": 8.0, **extra}
    skin = Skin("belly", [inp])
    skin.chambers[0].target_pressure = 80
    skin.chambers[0].pressure = 80
    return skin, ctrl


def test_deflate_to_bottom_sends_default_vacuum_time():
    skin, ctrl = _skin()
    assert skin.deflate(0, 80)                    # target 0 %
    assert ctrl.deflate_calls[-1]["vac_ms"] == DEFAULT_VACUUM_MS


def test_deflate_to_bottom_uses_configured_vacuum_time():
    skin, ctrl = _skin(vacuum_ms=2500)
    assert skin.deflate(0, 100)
    assert ctrl.deflate_calls[-1]["vac_ms"] == 2500


def test_deflate_already_at_ambient_still_pulls_vacuum():
    skin, ctrl = _skin()
    skin.chambers[0].target_pressure = 0
    skin.chambers[0].pressure = 0
    assert skin.deflate(0, 10)
    assert ctrl.deflate_calls[-1]["vac_ms"] == DEFAULT_VACUUM_MS


def test_deflate_above_bottom_stays_closed_loop():
    skin, ctrl = _skin()
    assert skin.deflate(0, 30)                    # target 50 %
    assert ctrl.deflate_calls[-1]["vac_ms"] is None


def test_zero_vacuum_time_stops_at_ambient():
    skin, ctrl = _skin(vacuum_ms=0)
    assert skin.deflate(0, 100)
    assert ctrl.deflate_calls[-1]["vac_ms"] is None


def test_set_pressure_zero_sends_vacuum_time():
    skin, ctrl = _skin(vacuum_ms=1500)
    assert skin.set_pressure(0, 0)
    assert ctrl.set_calls[-1]["vac_ms"] == 1500
    assert skin.set_pressure(0, 40)
    assert ctrl.set_calls[-1]["vac_ms"] is None


def test_min_pressure_in_config_is_ignored():
    skin, _ctrl = _skin(min_pressure=-6.0)
    assert skin.chambers[0].min_pressure == 0.0

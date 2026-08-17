"""Tests for Skin's compensated magnet source (pressure-informed touch).

Verifies the PC wiring end to end: a Skin with an enabled coupling matrix feeds
detection consumers (via ``skin.on_magnet``) a stream with the actuation offset
removed and the active-sensor set recomputed, scaled by the live chamber level.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _FakeNode:
    """Controller double that can push magnet frames to subscribers."""

    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self._magnet_cbs: list = []

    def on_pressure(self, _cb) -> None:
        pass

    def on_magnet(self, cb) -> None:
        self._magnet_cbs.append(cb)

    def emit_magnet(self, data: dict) -> None:
        for cb in list(self._magnet_cbs):
            cb(data)


def _skin(enabled: bool):
    node = _FakeNode()
    touch = {
        "node_mac": "AA:01",
        "sensor_count": 2,
        "coupling": {"unit": "uT", "sensor_count": 2, "bin_pct": 10.0,
                     "states": [{"chambers": [0], "levels": {"0": 100.0},
                                 "mag": [200.0, 0.0]}]},
        "compensation": {"enabled": enabled, "threshold_ut": 100.0},
    }
    inp = {"controller": node, "node_slot": 0, "max_pressure": 8.0}
    skin = Skin("belly", [inp], touch=touch, touch_controller=node)
    return skin, node


def test_enabled_compensation_removes_actuation_offset():
    skin, node = _skin(enabled=True)
    skin._on_pressure(0, 100)            # chamber 0 fully inflated
    seen: list = []
    skin.on_magnet(seen.append)
    node.emit_magnet({"type": "magnet", "mag": [205.0, 8.0], "act": [0]})
    assert seen
    out = seen[-1]
    assert out["mag"][0] == 5.0          # 205 - 200 (offset at 100 %)
    assert out["act"] == []              # residual 5 < threshold 100
    assert out["compensated"] is True


def test_real_touch_still_registers_while_inflated():
    skin, node = _skin(enabled=True)
    skin._on_pressure(0, 100)
    seen: list = []
    skin.on_magnet(seen.append)
    node.emit_magnet({"type": "magnet", "mag": [505.0, 8.0], "act": [0]})
    assert seen[-1]["act"] == [0]        # 505 - 200 = 305 >= 100


def test_offset_scales_with_chamber_level():
    skin, node = _skin(enabled=True)
    skin._on_pressure(0, 50)             # half inflated -> half the offset
    seen: list = []
    skin.on_magnet(seen.append)
    node.emit_magnet({"type": "magnet", "mag": [205.0, 8.0], "act": [0]})
    assert seen[-1]["mag"][0] == 105.0   # 205 - 100


def test_disabled_compensation_is_passthrough():
    skin, node = _skin(enabled=False)
    # No CompensatedMagnetSource -> touch_source is the raw controller.
    assert skin.touch_source is node
    seen: list = []
    skin.on_magnet(seen.append)
    raw = {"type": "magnet", "mag": [205.0, 8.0], "act": [0]}
    node.emit_magnet(raw)
    assert seen[-1] == raw               # unchanged

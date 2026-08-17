"""The per-skin-type saved sensitivity is the GENERAL default, everywhere.

Covers the chain that makes ``Settings.touch_threshold_ut`` apply outside the
dialogs that save it: the robot builder overlays it onto the skin's touch
config, the Skin pushes it to the node at build, and the pressure compensator
rederives ``act`` at the same value (so the node's detection and the
compensated stream agree).
"""

from unittest.mock import patch

from src.core.touch_compensation import (
    DEFAULT_THRESHOLD_UT,
    compensator_from_config,
)
from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin
from src.robots._robot_builder import _touch_with_saved_threshold


class _FakeNode:
    def __init__(self) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2)
        self.sent: list[tuple] = []

    def on_pressure(self, _cb) -> None:
        pass

    def on_magnet(self, _cb) -> None:
        pass

    def send_command(self, cmd, **kw) -> None:
        self.sent.append((cmd, kw))


_COUPLING = {"unit": "uT", "sensor_count": 2, "bin_pct": 10.0,
             "states": [{"chambers": [0], "levels": {"0": 100.0},
                         "mag": [200.0, 0.0]}]}


# ---------------------------------------------------------------------------
# Compensator threshold resolution
# ---------------------------------------------------------------------------

def test_compensator_threshold_falls_back_to_act_threshold():
    comp = compensator_from_config({
        "act_threshold_ut": 130.0,
        "coupling": _COUPLING,
        "compensation": {"enabled": True},          # no explicit threshold_ut
    })
    assert comp is not None
    assert comp.threshold_ut == 130.0


def test_explicit_compensation_threshold_still_wins():
    comp = compensator_from_config({
        "act_threshold_ut": 130.0,
        "coupling": _COUPLING,
        "compensation": {"enabled": True, "threshold_ut": 90.0},
    })
    assert comp is not None
    assert comp.threshold_ut == 90.0


def test_compensator_default_without_any_threshold():
    comp = compensator_from_config({
        "coupling": _COUPLING,
        "compensation": {"enabled": True},
    })
    assert comp is not None
    assert comp.threshold_ut == DEFAULT_THRESHOLD_UT


# ---------------------------------------------------------------------------
# Skin pushes the threshold to the node at build
# ---------------------------------------------------------------------------

def test_skin_pushes_act_threshold_at_build():
    node = _FakeNode()
    inp = {"controller": node, "node_slot": 0, "max_pressure": 8.0}
    Skin("belly", [inp], touch_controller=node,
         touch={"node_mac": "AA:01", "sensor_count": 2,
                "act_threshold_ut": 130.0})
    assert ("configure", {"act_threshold_ut": 130.0}) in node.sent


def test_skin_without_threshold_pushes_nothing():
    node = _FakeNode()
    inp = {"controller": node, "node_slot": 0, "max_pressure": 8.0}
    Skin("belly", [inp], touch_controller=node,
         touch={"node_mac": "AA:01", "sensor_count": 2})
    assert not any(kw.get("act_threshold_ut") for _c, kw in node.sent)


# ---------------------------------------------------------------------------
# Robot builder overlays the saved per-type value
# ---------------------------------------------------------------------------

def _patched_saved(value):
    return patch("src.config.settings.Settings.touch_threshold_ut",
                 lambda self, st: value if st == "thymio" else None)


def test_builder_overlays_saved_threshold_per_type():
    with _patched_saved(120.0):
        out = _touch_with_saved_threshold({"node_mac": "AA:01"}, "thymio")
    assert out is not None
    assert out["act_threshold_ut"] == 120.0


def test_builder_keeps_explicit_config_value():
    with _patched_saved(120.0):
        out = _touch_with_saved_threshold(
            {"node_mac": "AA:01", "act_threshold_ut": 80.0}, "thymio")
    assert out is not None
    assert out["act_threshold_ut"] == 80.0


def test_builder_passthrough_without_saved_or_type():
    with _patched_saved(None):
        cfg = {"node_mac": "AA:01"}
        assert _touch_with_saved_threshold(cfg, "thymio") is cfg
    with _patched_saved(120.0):
        assert _touch_with_saved_threshold(cfg, "") is cfg
        assert _touch_with_saved_threshold(None, "thymio") is None

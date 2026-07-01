"""Internal helpers for constructing Skin objects from config dicts.

Used by TurtleTreeRobot, ThymioRobot, and SimulatedRobot so the
config-parsing logic lives in one place.
"""

from __future__ import annotations

import logging
from typing import Any

from src.hardware.skin import Skin

logger = logging.getLogger(__name__)


def set_pump_counts(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Tell each controller how many pressure pumps its node shares.

    Drives the shared-pump fill-time scaling (see
    :mod:`src.hardware.fill_scaling`): concurrent inflations on a node split its
    pumps' airflow. ``node_direct`` has two onboard pumps; every other node type
    is treated as a single shared pump.
    """
    for node_cfg in node_configs:
        ctrl = controllers.get(node_cfg.get("mac", ""))
        if ctrl is None or not hasattr(ctrl, "fill_load"):
            continue
        count = 2 if node_cfg.get("node_type") == "node_direct" else 1
        ctrl.fill_load.pump_count = max(1, count)


def configure_multiplexed_nodes(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Send runtime `configure` to every node_multiplexed controller.

    The multiplexed firmware is runtime-sized by gateway config. This helper
    pushes the chamber count (and any organ channels) at connect time.
    """
    for node_cfg in node_configs:
        if node_cfg.get("node_type") != "node_multiplexed":
            continue
        mac = node_cfg.get("mac", "")
        ctrl = controllers.get(mac)
        if ctrl is None:
            continue

        max_slots = max(1, min(int(node_cfg.get("max_slots", 12)), 16))

        # Mux channels carrying organ+cover circuits (index = slot in the
        # firmware's organ broadcasts). Convention: highest channels first
        # (I13..I15) so they stay clear of the chamber autodetect.
        organ_channels = [int(c) for c in node_cfg.get("organ_channels", [])] or None

        ctrl.configure(num_chambers=max_slots, organ_channels=organ_channels)


def build_skins(
    skin_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
    touch_controllers: dict[str, Any] | None = None,
) -> dict[str, Skin]:
    """Construct Skin objects from the config format.

    Each skin's chambers must all reference the same MAC (single-node invariant
    — see Skin docstring). Skins that mix MACs or reference unknown nodes are
    skipped with an error log.

    Args:
        skin_configs:  List of skin dicts::

            [{"skin_id": "belly",
              "chambers": [{"mac": "AA:BB:...", "slot": 0, "max_pressure": 8.0}, ...]},
             ...]

        controllers:   Pre-built ``{mac: controller}`` dict for all nodes of this robot.
        touch_controllers:  Optional ``{skin_id: touch_controller}`` overriding the
            per-skin touch device. Used in simulation to give each skin its own
            ``SimulatedMagnetSensor`` so its T-buttons drive only that skin, even when
            several skins share a touch ``node_mac``. When absent, the touch
            controller is resolved from ``controllers`` by ``touch.node_mac``.
    """
    skins: dict[str, Skin] = {}
    for skin_cfg in skin_configs:
        skin = _build_one_skin(skin_cfg, controllers, touch_controllers)
        if skin is not None:
            skins[skin.skin_id] = skin
    return skins


def _build_one_skin(skin_cfg: dict[str, Any],
                    controllers: dict[str, Any],
                    touch_controllers: dict[str, Any] | None = None) -> Skin | None:
    """Build a single Skin from its config dict, or return None if the
    config is invalid (logs the reason)."""
    skin_id = skin_cfg.get("skin_id", "?")
    chambers = skin_cfg.get("chambers", [])
    if not chambers:
        return None

    macs = {ch["mac"] for ch in chambers}
    if len(macs) > 1:
        logger.error(
            "Skin %s spans multiple MACs (%s) — skipping. "
            "A skin must belong to a single node.", skin_id, sorted(macs))
        return None

    mac = next(iter(macs))
    ctrl = controllers.get(mac)
    if ctrl is None:
        logger.error("Skin %s references unknown MAC %s — skipping.",
                     skin_id, mac)
        return None

    chamber_inputs = [
        {"controller":   ctrl,
         "node_slot":    int(ch["slot"]),
         "max_pressure": float(ch.get("max_pressure", 8.0)),
         "min_pressure": float(ch.get("min_pressure", 0.0)),
         "fill_time_ms": ch.get("fill_time_ms"),
         "fill_profile": ch.get("fill_profile"),
         "fill_profiles": ch.get("fill_profiles"),
         "fill_mode": ch.get("fill_mode")}
        for ch in chambers
    ]
    # Per-ring LED mounting angle (from the skin config) → the node's controller,
    # so every activity's LED look is rotated to match a physically-turned ring
    # without the behaviour having to know about it.
    led_angles = skin_cfg.get("led_angles")
    if led_angles and hasattr(ctrl, "set_led_angles"):
        ctrl.set_led_angles(led_angles)

    touch_ctrl = (touch_controllers or {}).get(skin_id)
    if touch_ctrl is None:
        touch_ctrl = _resolve_touch_ctrl(skin_cfg, controllers)
    return Skin(
        skin_id=skin_id,
        chamber_inputs=chamber_inputs,
        grid=skin_cfg.get("grid"),
        chamber_grid=skin_cfg.get("chamber_grid"),
        touch=skin_cfg.get("touch"),
        touch_controller=touch_ctrl,
        shape=skin_cfg.get("shape", "rect"),
        organ=skin_cfg.get("organ"),
        organs=skin_cfg.get("organs"),
        skin_type=skin_cfg.get("skin_type", ""),
        skin_variant=skin_cfg.get("skin_variant", ""),
    )


def _resolve_touch_ctrl(skin_cfg: dict[str, Any],
                        controllers: dict[str, Any]) -> Any:
    """Return the controller for the magnet sensor referenced by ``skin_cfg.touch``."""
    touch_cfg = skin_cfg.get("touch") or {}
    return controllers.get(touch_cfg.get("node_mac")) if touch_cfg else None

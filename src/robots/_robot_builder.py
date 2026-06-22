"""Internal helpers for constructing Skin and AirReservoir objects from config dicts.

Used by TurtleRobot, TreeRobot, ThymioRobot, and SimulatedRobot so the
config-parsing logic lives in one place.
"""

from __future__ import annotations

import logging
from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.skin import Skin

logger = logging.getLogger(__name__)


def set_pump_counts(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Tell each controller how many pressure pumps its node shares.

    Drives the shared-pump fill-time scaling (see
    :mod:`src.hardware.fill_scaling`): concurrent inflations on a node split its
    pumps' airflow. ``node_direct`` has two onboard pumps; ``node_multiplexed``
    shares ``pump_inflate_count`` pumps when it has reservoirs (else there are no
    chamber pumps, so the count is left at the default 1).
    """
    for node_cfg in node_configs:
        ctrl = controllers.get(node_cfg.get("mac", ""))
        if ctrl is None or not hasattr(ctrl, "fill_load"):
            continue
        node_type = node_cfg.get("node_type")
        if node_type == "node_direct":
            count = 2
        elif node_type == "node_multiplexed" and node_cfg.get("has_reservoirs"):
            count = int(node_cfg.get("pump_inflate_count", 3))
        else:
            count = 1
        ctrl.fill_load.pump_count = max(1, count)


def configure_multiplexed_nodes(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Send runtime `configure` to every node_multiplexed controller.

    The multiplexed firmware is runtime-sized by gateway config. This helper
    keeps chamber sizing and tank safety limits in one place and ensures safe
    defaults are pushed at connect time.

    Tank limits and pump groups are only included when the node config has
    ``has_reservoirs: true`` — multiplexed nodes without reservoirs only
    receive ``num_chambers``.
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

        if not node_cfg.get("has_reservoirs", False):
            ctrl.configure(num_chambers=max_slots, organ_channels=organ_channels)
            continue

        pump_inflate_count = max(0, min(int(node_cfg.get("pump_inflate_count", 3)), 6))
        pump_deflate_count = max(0, min(int(node_cfg.get("pump_deflate_count", 3)), 6))
        tank_pressure_min_kpa = float(node_cfg.get("tank_pressure_min_kpa", 0.0))
        tank_pressure_max_kpa = float(node_cfg.get("tank_pressure_max_kpa", 50.0))
        tank_vacuum_min_kpa   = float(node_cfg.get("tank_vacuum_min_kpa", -50.0))
        tank_vacuum_max_kpa   = float(node_cfg.get("tank_vacuum_max_kpa", 0.0))

        # Operational set-point: take from YAML if present, otherwise default
        # to the midpoint of [min, max] so the pumps have headroom to work in.
        tank_pressure_target_kpa = float(node_cfg.get(
            "tank_pressure_target_kpa",
            (tank_pressure_min_kpa + tank_pressure_max_kpa) / 2.0))
        tank_vacuum_target_kpa = float(node_cfg.get(
            "tank_vacuum_target_kpa",
            (tank_vacuum_min_kpa + tank_vacuum_max_kpa) / 2.0))

        pressure_group = list(range(1, pump_inflate_count + 1))
        vacuum_start = pump_inflate_count + 1
        vacuum_end = min(vacuum_start + pump_deflate_count - 1, 6)
        vacuum_group = list(range(vacuum_start, vacuum_end + 1))

        ctrl.configure(
            num_chambers=max_slots,
            pump_inflate_count=pump_inflate_count,
            pump_deflate_count=pump_deflate_count,
            tank_pressure_min_kpa=tank_pressure_min_kpa,
            tank_pressure_max_kpa=tank_pressure_max_kpa,
            tank_pressure_target_kpa=tank_pressure_target_kpa,
            tank_vacuum_min_kpa=tank_vacuum_min_kpa,
            tank_vacuum_max_kpa=tank_vacuum_max_kpa,
            tank_vacuum_target_kpa=tank_vacuum_target_kpa,
            pump_groups={"pressure": pressure_group, "vacuum": vacuum_group},
            organ_channels=organ_channels,
        )


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

            [{"skin_id": "belly", "name": "Belly",
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
         "fill_time_ms": ch.get("fill_time_ms")}
        for ch in chambers
    ]
    touch_ctrl = (touch_controllers or {}).get(skin_id)
    if touch_ctrl is None:
        touch_ctrl = _resolve_touch_ctrl(skin_cfg, controllers)
    return Skin(
        skin_id=skin_id,
        chamber_inputs=chamber_inputs,
        name=skin_cfg.get("name"),
        grid=skin_cfg.get("grid"),
        chamber_grid=skin_cfg.get("chamber_grid"),
        touch=skin_cfg.get("touch"),
        touch_controller=touch_ctrl,
        shape=skin_cfg.get("shape", "rect"),
        organ=skin_cfg.get("organ"),
        skin_type=skin_cfg.get("skin_type", ""),
        skin_variant=skin_cfg.get("skin_variant", ""),
    )


def _resolve_touch_ctrl(skin_cfg: dict[str, Any],
                        controllers: dict[str, Any]) -> Any:
    """Return the controller for the magnet sensor referenced by ``skin_cfg.touch``."""
    touch_cfg = skin_cfg.get("touch") or {}
    return controllers.get(touch_cfg.get("node_mac")) if touch_cfg else None


def build_reservoirs(
    node_configs: list[dict[str, Any]],
    reservoir_configs: dict[str, Any] | None,
    controllers: dict[str, Any],
) -> dict[str, AirReservoir]:
    """Construct AirReservoir objects.

    Args:
        node_configs: Node list from robot settings.
        reservoir_configs:  Dict with optional ``"pressure"`` and ``"vacuum"`` keys::

            {"pressure": {"mac": "AA:BB:...", "node_type": "reservoir",
                           "pump_count": 2},
             "vacuum":   {"mac": "BB:CC:...", "pump_count": 1}}

        controllers:  Pre-built ``{mac: controller}`` dict.

    Returns:
        ``{"pressure": AirReservoir, "vacuum": AirReservoir}`` (only present keys).
    """
    reservoirs: dict[str, AirReservoir] = {}
    if reservoir_configs:
        for kind in ("pressure", "vacuum"):
            cfg = reservoir_configs.get(kind)
            if not cfg:
                continue
            mac = cfg.get("mac", "")
            ctrl = controllers.get(mac)
            if ctrl is None:
                continue
            reservoirs[kind] = AirReservoir(
                kind=kind,  # type: ignore[arg-type]
                controller=ctrl,
                node_slot=int(cfg.get("node_slot", 0)),
                pump_count=int(cfg.get("pump_count", 1)),
            )

    # Auto-derive internal shared reservoirs from node_multiplexed nodes that
    # have has_reservoirs: true.
    for node_cfg in node_configs:
        if node_cfg.get("node_type") != "node_multiplexed":
            continue
        if not node_cfg.get("has_reservoirs", False):
            continue
        mac = node_cfg.get("mac", "")
        ctrl = controllers.get(mac)
        if ctrl is None:
            continue
        max_slots = max(1, min(int(node_cfg.get("max_slots", 12)), 16))
        reservoirs.setdefault(
            "pressure",
            AirReservoir(
                kind="pressure",
                controller=ctrl,
                node_slot=max_slots,
                pump_count=int(node_cfg.get("pump_inflate_count", 3)),
            ),
        )
        reservoirs.setdefault(
            "vacuum",
            AirReservoir(
                kind="vacuum",
                controller=ctrl,
                node_slot=max_slots + 1,
                pump_count=int(node_cfg.get("pump_deflate_count", 3)),
            ),
        )

    return reservoirs

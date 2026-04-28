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


def configure_reservoir_nodes(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Send runtime `configure` to every node_reservoir controller.

    The reservoir firmware is runtime-sized by gateway config. This helper keeps
    chamber sizing and tank safety limits in one place and ensures safe defaults
    are pushed at connect time.
    """
    for node_cfg in node_configs:
        if node_cfg.get("node_type") != "node_reservoir":
            continue
        mac = node_cfg.get("mac", "")
        ctrl = controllers.get(mac)
        if ctrl is None:
            continue

        max_slots = max(1, min(int(node_cfg.get("max_slots", 12)), 16))
        pump_inflate_count = max(0, min(int(node_cfg.get("pump_inflate_count", 3)), 6))
        pump_deflate_count = max(0, min(int(node_cfg.get("pump_deflate_count", 3)), 6))
        tank_pressure_max_kpa = float(node_cfg.get("tank_pressure_max_kpa", 50.0))
        tank_vacuum_max_kpa = float(node_cfg.get("tank_vacuum_max_kpa", 50.0))

        pressure_group = list(range(1, pump_inflate_count + 1))
        vacuum_start = pump_inflate_count + 1
        vacuum_end = min(vacuum_start + pump_deflate_count - 1, 6)
        vacuum_group = list(range(vacuum_start, vacuum_end + 1))

        ctrl.configure(
            num_chambers=max_slots,
            pump_inflate_count=pump_inflate_count,
            pump_deflate_count=pump_deflate_count,
            tank_pressure_max_kpa=tank_pressure_max_kpa,
            tank_vacuum_max_kpa=tank_vacuum_max_kpa,
            pump_groups={"pressure": pressure_group, "vacuum": vacuum_group},
        )


def build_skins(
    skin_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
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
    """
    skins: dict[str, Skin] = {}
    for skin_cfg in skin_configs:
        skin_id = skin_cfg.get("skin_id", "?")
        chambers = skin_cfg.get("chambers", [])
        if not chambers:
            continue

        macs = {ch["mac"] for ch in chambers}
        if len(macs) > 1:
            logger.error(
                "Skin %s spans multiple MACs (%s) — skipping. "
                "A skin must belong to a single node.", skin_id, sorted(macs))
            continue

        mac = next(iter(macs))
        ctrl = controllers.get(mac)
        if ctrl is None:
            logger.error("Skin %s references unknown MAC %s — skipping.", skin_id, mac)
            continue

        chamber_inputs = [
            {"controller":   ctrl,
             "node_slot":    int(ch["slot"]),
             "max_pressure": float(ch.get("max_pressure", 8.0))}
            for ch in chambers
        ]
        skins[skin_id] = Skin(
            skin_id=skin_id,
            chamber_inputs=chamber_inputs,
            name=skin_cfg.get("name"),
        )
    return skins


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

    # Auto-derive internal shared reservoirs from node_reservoir nodes.
    for node_cfg in node_configs:
        if node_cfg.get("node_type") != "node_reservoir":
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

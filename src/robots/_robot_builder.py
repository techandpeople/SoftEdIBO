"""Internal helpers for constructing Skin and AirReservoir objects from config dicts.

Used by TurtleRobot, TreeRobot, ThymioRobot, and SimulatedRobot so the
config-parsing logic lives in one place.
"""

from __future__ import annotations

from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.skin import Skin


def build_skins(
    skin_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> dict[str, Skin]:
    """Construct Skin objects from the new config format.

    Args:
        skin_configs:  List of skin dicts::

            [{"skin_id": "belly", "name": "Belly",
              "chambers": [{"mac": "AA:BB:...", "slot": 0, "max_pressure": 8.0}, ...]},
             ...]

        controllers:   Pre-built ``{mac: controller}`` dict for all nodes of this robot.

    Returns:
        ``{skin_id: Skin}`` ordered dict.
    """
    skins: dict[str, Skin] = {}
    for skin_cfg in skin_configs:
        chamber_inputs = []
        for ch in skin_cfg.get("chambers", []):
            mac = ch["mac"]
            ctrl = controllers.get(mac)
            if ctrl is None:
                continue  # node not configured — skip this chamber
            chamber_inputs.append({
                "controller":   ctrl,
                "node_slot":    int(ch["slot"]),
                "max_pressure": float(ch.get("max_pressure", 8.0)),
            })
        if not chamber_inputs:
            continue
        skin = Skin(
            skin_id=skin_cfg["skin_id"],
            chamber_inputs=chamber_inputs,
            name=skin_cfg.get("name"),
        )
        skins[skin.skin_id] = skin
    return skins


def build_reservoirs(
    reservoir_configs: dict[str, Any] | None,
    controllers: dict[str, Any],
) -> dict[str, AirReservoir]:
    """Construct AirReservoir objects from the config block.

    Args:
        reservoir_configs:  Dict with optional ``"pressure"`` and ``"vacuum"`` keys::

            {"pressure": {"mac": "AA:BB:...", "node_type": "reservoir",
                           "pump_count": 2},
             "vacuum":   {"mac": "BB:CC:...", "pump_count": 1}}

        controllers:  Pre-built ``{mac: controller}`` dict.

    Returns:
        ``{"pressure": AirReservoir, "vacuum": AirReservoir}`` (only present keys).
    """
    reservoirs: dict[str, AirReservoir] = {}
    if not reservoir_configs:
        return reservoirs
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
    return reservoirs

"""Skin module — a logical grouping of 1-3 air chambers from a single ESP32 node.

A Skin is a physical tactile piece. With the current hardware (`node_direct`
3 chambers, `node_reservoir` up to 12 chambers) every chamber of a skin lives
on the same ESP32 — multi-node skins are no longer a use case. The class is
correspondingly simple: one controller, one slot list.

Config expected by the constructor (``chamber_inputs``):
    [
        {"controller":   <ESP32Controller|SimulatedController>,
         "node_slot":    <int>,            # physical slot on the node
         "max_pressure": <float>},         # kPa cap for this chamber
        ...
    ]

All entries must share the same ``controller`` (single-MAC invariant).
"""

import logging
from typing import Any

from src.hardware.air_chamber import AirChamber, ChamberState

logger = logging.getLogger(__name__)


class Skin:
    """A physical skin unit with 1-3 air chambers on a single ESP32 node."""

    def __init__(
        self,
        skin_id: str,
        chamber_inputs: list[dict[str, Any]],
        name: str | None = None,
    ):
        if not chamber_inputs:
            raise ValueError(f"Skin {skin_id!r} has no chambers")

        self.skin_id = skin_id
        self.name = name or skin_id

        self._ctrl = chamber_inputs[0]["controller"]
        self.mac: str = self._ctrl.mac_address

        # local_idx → node_slot
        self._slots: list[int] = []
        # node_slot → local_idx
        self._reverse: dict[int, int] = {}
        # local_idx → AirChamber
        self._chambers: dict[int, AirChamber] = {}

        for local_idx, inp in enumerate(chamber_inputs):
            if inp["controller"] is not self._ctrl:
                raise ValueError(
                    f"Skin {skin_id!r}: all chambers must share one controller "
                    f"(got {inp['controller'].mac_address!r} vs {self.mac!r}). "
                    "With node_direct (3 chambers) and node_reservoir (12 chambers) "
                    "a single skin always fits inside one node."
                )

            node_slot    = int(inp["node_slot"])
            max_pressure = float(inp.get("max_pressure", 8.0))

            self._slots.append(node_slot)
            self._reverse[node_slot] = local_idx
            self._chambers[local_idx] = AirChamber(
                chamber_id=local_idx,
                esp32_mac=self.mac,
                max_pressure=max_pressure,
            )

        # One callback registration for the whole skin.
        self._ctrl.on_pressure(self._on_pressure)
        on_target = getattr(self._ctrl, "on_target", None)
        if on_target is not None:
            on_target(self._on_target)

        # Push per-chamber max pressure to the firmware so it survives PC crashes.
        set_limit = getattr(self._ctrl, "set_max_pressure", None)
        if set_limit is not None:
            for local_idx, slot in enumerate(self._slots):
                set_limit(slot, self._chambers[local_idx].max_pressure)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chambers(self) -> dict[int, AirChamber]:
        """Local-index → AirChamber mapping."""
        return self._chambers

    @property
    def chamber_count(self) -> int:
        return len(self._chambers)

    @property
    def node_macs(self) -> list[str]:
        """Single-element list with this skin's node MAC (kept for backward compat)."""
        return [self.mac]

    @property
    def is_connected(self) -> bool:
        return self._ctrl.is_connected

    @property
    def chamber_defs(self) -> list[dict[str, Any]]:
        """Chamber descriptors in config format (for serialisation / simulation)."""
        return [
            {"mac": self.mac, "slot": slot,
             "max_pressure": self._chambers[idx].max_pressure}
            for idx, slot in enumerate(self._slots)
        ]

    # ------------------------------------------------------------------
    # Commands  (local_idx = 0-based position within this skin)
    # ------------------------------------------------------------------

    def inflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Inflate by delta % (relative). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "inflate", delta) for i in self._chambers)
        return self._apply(local_idx, "inflate", delta)

    def deflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Deflate by delta % (relative). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "deflate", delta) for i in self._chambers)
        return self._apply(local_idx, "deflate", delta)

    def set_pressure(self, local_idx: int | None = None, value: int = 100) -> bool:
        """Set absolute target pressure (0-100 %). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "set_pressure", value) for i in self._chambers)
        return self._apply(local_idx, "set_pressure", value)

    def hold(self, local_idx: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s has no chamber at local index %d", self.skin_id, local_idx)
            return False
        chamber.target_pressure = chamber.pressure
        chamber.state = ChamberState.IDLE
        return self._ctrl.hold(self._slots[local_idx])

    def fire_touch(self, local_idx: int, raw_value: int) -> None:
        slot = self._slot_or_none(local_idx)
        fire = getattr(self._ctrl, "fire_touch", None)
        if slot is not None and fire is not None:
            fire(slot, raw_value)

    def touch_press(self, local_idx: int) -> None:
        slot = self._slot_or_none(local_idx)
        sim_press = getattr(self._ctrl, "simulate_touch_press", None)
        if slot is not None and sim_press is not None:
            sim_press(slot)

    def touch_release(self, local_idx: int, hold_ms: int) -> None:
        slot = self._slot_or_none(local_idx)
        sim_release = getattr(self._ctrl, "simulate_touch_release", None)
        if slot is not None and sim_release is not None:
            sim_release(slot, hold_ms)

    def pause(self) -> None:
        for chamber in self._chambers.values():
            chamber.state = ChamberState.IDLE

    def get_status(self) -> dict[str, Any]:
        return {
            "skin_id": self.skin_id,
            "mac": self.mac,
            "chambers": {
                idx: {"state": c.state.value, "pressure": c.pressure}
                for idx, c in self._chambers.items()
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _slot_or_none(self, local_idx: int) -> int | None:
        if 0 <= local_idx < len(self._slots):
            return self._slots[local_idx]
        return None

    def _on_pressure(self, node_slot: int, pressure: int) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            self._chambers[local_idx].update_pressure(pressure)

    def _on_target(self, node_slot: int, target: int) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            self._chambers[local_idx].target_pressure = target

    def _apply(self, local_idx: int, kind: str, value: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        slot = self._slots[local_idx]

        if kind == "inflate":
            new_target = min(100, chamber.target_pressure + value)
            chamber.target_pressure = new_target
            if chamber.pressure < new_target:
                chamber.state = ChamberState.INFLATING
            elif new_target > 0:
                chamber.state = ChamberState.INFLATED
            return self._ctrl.inflate(slot, value)

        if kind == "deflate":
            new_target = max(0, chamber.target_pressure - value)
            chamber.target_pressure = new_target
            if chamber.pressure > new_target:
                chamber.state = ChamberState.DEFLATING
            else:
                chamber.state = ChamberState.IDLE
            return self._ctrl.deflate(slot, value)

        # set_pressure
        v = max(0, min(100, value))
        chamber.target_pressure = v
        if chamber.pressure < v:
            chamber.state = ChamberState.INFLATING
        elif chamber.pressure > v:
            chamber.state = ChamberState.DEFLATING
        elif v > 0:
            chamber.state = ChamberState.INFLATED
        else:
            chamber.state = ChamberState.IDLE
        return self._ctrl.set_pressure(slot, v)

    def __repr__(self) -> str:
        return (
            f"Skin(id={self.skin_id!r}, chambers={self.chamber_count}, "
            f"mac={self.mac!r})"
        )

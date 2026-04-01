"""Skin module — a logical grouping of 1-3 air chambers from the same robot.

A Skin is a physical tactile piece. Its chambers may live on one or more
ESP32 nodes belonging to the same robot.  Internally, Skin maps local
chamber indices (0, 1, 2) to (node_mac, node_slot) pairs so that callers
— activities, monitor widgets, etc. — never need to know about node topology.

Config expected by the constructor (``chamber_inputs``):
    [
        {"controller": <ESP32Controller|SimulatedController>,
         "node_slot":  <int>,          # physical slot on that node
         "max_pressure": <float>},     # kPa cap for this chamber
        ...
    ]
"""

import logging
from typing import Any, Callable

from src.hardware.air_chamber import AirChamber, ChamberState

logger = logging.getLogger(__name__)


class Skin:
    """A physical skin unit with 1-3 air chambers, potentially across multiple nodes."""

    def __init__(
        self,
        skin_id: str,
        chamber_inputs: list[dict[str, Any]],
        name: str | None = None,
    ):
        """Initialize a skin.

        Args:
            skin_id:         Unique machine key for this skin.
            chamber_inputs:  Ordered list of chamber descriptors.  Each entry:
                             ``{"controller": ..., "node_slot": int,
                                "max_pressure": float}``
                             The list order defines the local chamber index
                             (0 = first entry, 1 = second, …).
            name:            Human-readable display label (defaults to skin_id).
        """
        self.skin_id = skin_id
        self.name = name or skin_id

        # mac → controller
        self._controllers: dict[str, Any] = {}
        # local_idx → (mac, node_slot)
        self._routing: dict[int, tuple[str, int]] = {}
        # (mac, node_slot) → local_idx   (reverse lookup for callbacks)
        self._reverse: dict[tuple[str, int], int] = {}
        # local_idx → AirChamber
        self._chambers: dict[int, AirChamber] = {}

        for local_idx, inp in enumerate(chamber_inputs):
            ctrl = inp["controller"]
            node_slot = int(inp["node_slot"])
            mac = ctrl.mac_address
            max_pressure = float(inp.get("max_pressure", 8.0))

            self._routing[local_idx] = (mac, node_slot)
            self._reverse[(mac, node_slot)] = local_idx
            self._chambers[local_idx] = AirChamber(
                chamber_id=local_idx,
                esp32_mac=mac,
                max_pressure=max_pressure,
            )

            if mac not in self._controllers:
                self._controllers[mac] = ctrl
                ctrl.on_pressure(self._make_pressure_cb(mac))
                on_target = getattr(ctrl, "on_target", None)
                if on_target is not None:
                    on_target(self._make_target_cb(mac))

            set_limit = getattr(ctrl, "set_max_pressure", None)
            if set_limit is not None:
                set_limit(node_slot, max_pressure)

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
        """MACs of all nodes used by this skin."""
        return list(self._controllers.keys())

    @property
    def is_connected(self) -> bool:
        """True only if every node used by this skin is connected."""
        return all(c.is_connected for c in self._controllers.values())

    @property
    def chamber_defs(self) -> list[dict[str, Any]]:
        """Return chamber descriptors in config format (for serialisation / simulation).

        Returns a list ordered by local index::

            [{"mac": ..., "slot": ..., "max_pressure": ...}, ...]
        """
        result = []
        for local_idx in sorted(self._routing):
            mac, node_slot = self._routing[local_idx]
            result.append({
                "mac": mac,
                "slot": node_slot,
                "max_pressure": self._chambers[local_idx].max_pressure,
            })
        return result

    # ------------------------------------------------------------------
    # Commands  (local_idx = 0-based position within this skin)
    # ------------------------------------------------------------------

    def inflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Inflate by delta % (relative). Pass None to inflate all chambers."""
        if local_idx is not None:
            return self._inflate_one(local_idx, delta)
        return all(self._inflate_one(i, delta) for i in self._chambers)

    def deflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Deflate by delta % (relative). Pass None to deflate all chambers."""
        if local_idx is not None:
            return self._deflate_one(local_idx, delta)
        return all(self._deflate_one(i, delta) for i in self._chambers)

    def set_pressure(self, local_idx: int | None = None, value: int = 100) -> bool:
        """Set absolute target pressure (0-100 %). Pass None for all chambers."""
        if local_idx is not None:
            return self._set_pressure_one(local_idx, value)
        return all(self._set_pressure_one(i, value) for i in self._chambers)

    def hold(self, local_idx: int) -> bool:
        """Freeze a chamber at its current pressure."""
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s has no chamber at local index %d", self.skin_id, local_idx)
            return False
        mac, node_slot = self._routing[local_idx]
        chamber.target_pressure = chamber.pressure
        chamber.state = ChamberState.IDLE
        return self._controllers[mac].hold(node_slot)

    def fire_touch(self, local_idx: int, raw_value: int) -> None:
        """Simulate a touch sensor event (routes to the correct node)."""
        routing = self._routing.get(local_idx)
        if routing is None:
            return
        mac, node_slot = routing
        fire = getattr(self._controllers[mac], "fire_touch", None)
        if fire is not None:
            fire(node_slot, raw_value)

    def touch_press(self, local_idx: int) -> None:
        """Trigger a simulated touch press ramp-up on the correct node."""
        routing = self._routing.get(local_idx)
        if routing is None:
            return
        mac, node_slot = routing
        sim_press = getattr(self._controllers[mac], "simulate_touch_press", None)
        if sim_press is not None:
            sim_press(node_slot)

    def touch_release(self, local_idx: int, hold_ms: int) -> None:
        """Trigger a simulated touch release ramp-down on the correct node."""
        routing = self._routing.get(local_idx)
        if routing is None:
            return
        mac, node_slot = routing
        sim_release = getattr(self._controllers[mac], "simulate_touch_release", None)
        if sim_release is not None:
            sim_release(node_slot, hold_ms)

    def pause(self) -> None:
        """Set all chambers to IDLE (called when the session is paused)."""
        for chamber in self._chambers.values():
            chamber.state = ChamberState.IDLE

    def get_status(self) -> dict[str, Any]:
        return {
            "skin_id": self.skin_id,
            "node_macs": self.node_macs,
            "chambers": {
                idx: {"state": c.state.value, "pressure": c.pressure}
                for idx, c in self._chambers.items()
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_pressure_cb(self, mac: str) -> Callable[[int, int], None]:
        def cb(node_slot: int, pressure: int) -> None:
            local_idx = self._reverse.get((mac, node_slot))
            if local_idx is None:
                return
            chamber = self._chambers[local_idx]
            chamber.pressure = pressure
            target = chamber.target_pressure
            if pressure == target:
                chamber.state = ChamberState.INFLATED if target > 0 else ChamberState.IDLE
            elif pressure < target:
                chamber.state = ChamberState.INFLATING
            else:
                chamber.state = ChamberState.DEFLATING
        return cb

    def _make_target_cb(self, mac: str) -> Callable[[int, int], None]:
        def cb(node_slot: int, target: int) -> None:
            local_idx = self._reverse.get((mac, node_slot))
            if local_idx is not None:
                self._chambers[local_idx].target_pressure = target
        return cb

    def _inflate_one(self, local_idx: int, delta: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        mac, node_slot = self._routing[local_idx]
        new_target = min(100, chamber.target_pressure + delta)
        chamber.target_pressure = new_target
        if chamber.pressure < new_target:
            chamber.state = ChamberState.INFLATING
        elif new_target > 0:
            chamber.state = ChamberState.INFLATED
        return self._controllers[mac].inflate(node_slot, delta)

    def _deflate_one(self, local_idx: int, delta: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        mac, node_slot = self._routing[local_idx]
        new_target = max(0, chamber.target_pressure - delta)
        chamber.target_pressure = new_target
        if chamber.pressure > new_target:
            chamber.state = ChamberState.DEFLATING
        else:
            chamber.state = ChamberState.IDLE
        return self._controllers[mac].deflate(node_slot, delta)

    def _set_pressure_one(self, local_idx: int, value: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        mac, node_slot = self._routing[local_idx]
        value = max(0, min(100, value))
        chamber.target_pressure = value
        if chamber.pressure < value:
            chamber.state = ChamberState.INFLATING
        elif chamber.pressure > value:
            chamber.state = ChamberState.DEFLATING
        elif value > 0:
            chamber.state = ChamberState.INFLATED
        else:
            chamber.state = ChamberState.IDLE
        return self._controllers[mac].set_pressure(node_slot, value)

    def __repr__(self) -> str:
        return (
            f"Skin(id={self.skin_id!r}, chambers={self.chamber_count}, "
            f"nodes={self.node_macs!r})"
        )

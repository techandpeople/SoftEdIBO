"""Skin module - a physical unit containing 1 to 3 air chambers.

A Skin is the basic tactile unit in SoftEdIBO. Each skin is a physical
piece that can be touched and contains air chambers that inflate/deflate.

Multiple skins can share the same ESP32 node as long as the total
number of chambers does not exceed 3 (the node's capacity).
"""

import logging
from typing import Any

from src.hardware.air_chamber import AirChamber, ChamberState

logger = logging.getLogger(__name__)


class Skin:
    """A physical skin unit with 1-3 air chambers on an ESP32 node."""

    def __init__(
        self,
        skin_id: str,
        controller: Any,
        chamber_slots: list[int],
        name: str | None = None,
        pressure_limits: dict[int, int] | None = None,
    ):
        """Initialize a skin.

        Args:
            skin_id: Unique identifier for this skin.
            controller: ESP32 controller managing this skin's chambers.
            chamber_slots: Which chamber slots (0-2) on the ESP32 this skin uses.
                e.g. [0] for a 1-chamber skin, [0, 1, 2] for a full 3-chamber skin.
            name: Human-readable display label. Defaults to skin_id if not given.
            pressure_limits: Per-slot max pressure, e.g. {0: 80, 1: 100}.
        """
        self.skin_id = skin_id
        self.name = name or skin_id
        self._controller = controller
        limits = pressure_limits or {}
        self._chambers: dict[int, AirChamber] = {
            slot: AirChamber(
                chamber_id=slot,
                esp32_mac=controller.mac_address,
                max_pressure=limits.get(slot, 100),
            )
            for slot in chamber_slots
        }
        # Propagate limits to controller (used by SimulatedController touch ramps)
        set_limit = getattr(controller, "set_max_pressure", None)
        if set_limit is not None:
            for slot, chamber in self._chambers.items():
                set_limit(slot, chamber.max_pressure)

        controller.on_pressure(self._on_pressure_update)
        on_target = getattr(controller, "on_target", None)
        if on_target is not None:
            on_target(self._on_target_update)

    @property
    def chambers(self) -> dict[int, AirChamber]:
        """Get the air chambers in this skin."""
        return self._chambers

    @property
    def chamber_count(self) -> int:
        """Get the number of chambers in this skin."""
        return len(self._chambers)

    @property
    def is_connected(self) -> bool:
        """True if the underlying ESP32 controller's gateway is connected."""
        return self._controller.is_connected

    @property
    def esp32_mac(self) -> str:
        """Get the MAC address of the ESP32 controlling this skin."""
        return self._controller.mac_address

    def inflate(self, slot: int | None = None, delta: int = 10) -> bool:
        """Inflate a chamber by delta % (relative), or all chambers if slot is None."""
        if slot is not None:
            return self._inflate_one(slot, delta)
        return all(self._inflate_one(s, delta) for s in self._chambers)

    def deflate(self, slot: int | None = None, delta: int = 10) -> bool:
        """Deflate a chamber by delta % (relative), or all chambers if slot is None."""
        if slot is not None:
            return self._deflate_one(slot, delta)
        return all(self._deflate_one(s, delta) for s in self._chambers)

    def set_pressure(self, slot: int | None = None, value: int = 100) -> bool:
        """Set absolute target pressure (0-100 %), or all chambers if slot is None."""
        if slot is not None:
            return self._set_pressure_one(slot, value)
        return all(self._set_pressure_one(s, value) for s in self._chambers)

    def hold(self, slot: int) -> bool:
        """Freeze a chamber at its current pressure."""
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        chamber.target_pressure = chamber.pressure
        chamber.state = ChamberState.IDLE
        return self._controller.hold(slot)

    def fire_touch(self, sensor_id: int, raw_value: int) -> None:
        """Simulate a touch sensor event if the controller supports it."""
        fire = getattr(self._controller, "fire_touch", None)
        if fire is not None:
            fire(sensor_id, raw_value)

    def touch_press(self, slot: int) -> None:
        """Trigger a simulated touch press ramp-up if the controller supports it."""
        sim_press = getattr(self._controller, "simulate_touch_press", None)
        if sim_press is not None:
            sim_press(slot)

    def touch_release(self, slot: int, hold_ms: int) -> None:
        """Trigger a simulated touch release ramp-down if the controller supports it."""
        sim_release = getattr(self._controller, "simulate_touch_release", None)
        if sim_release is not None:
            sim_release(slot, hold_ms)

    def pause(self) -> None:
        """Set all chambers to IDLE (called when the session is paused)."""
        for chamber in self._chambers.values():
            chamber.state = ChamberState.IDLE

    def get_status(self) -> dict[str, Any]:
        """Get status of all chambers in this skin."""
        return {
            "skin_id": self.skin_id,
            "esp32_mac": self.esp32_mac,
            "chambers": {
                slot: {"state": c.state.value, "pressure": c.pressure}
                for slot, c in self._chambers.items()
            },
        }

    def _on_target_update(self, chamber_id: int, target: int) -> None:
        """Update chamber target_pressure when the controller reports a target change."""
        chamber = self._chambers.get(chamber_id)
        if chamber is not None:
            chamber.target_pressure = target

    def _on_pressure_update(self, chamber_id: int, pressure: int) -> None:
        """Update chamber pressure and infer state from pressure movement."""
        chamber = self._chambers.get(chamber_id)
        if chamber is None:
            return
        chamber.pressure = pressure
        target = chamber.target_pressure
        if pressure == target:
            chamber.state = ChamberState.INFLATED if target > 0 else ChamberState.IDLE
        elif pressure < target:
            chamber.state = ChamberState.INFLATING
        else:
            chamber.state = ChamberState.DEFLATING

    def _inflate_one(self, slot: int, delta: int) -> bool:
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        new_target = min(chamber.max_pressure, chamber.target_pressure + delta)
        chamber.target_pressure = new_target
        if chamber.pressure < new_target:
            chamber.state = ChamberState.INFLATING
        elif new_target > 0:
            chamber.state = ChamberState.INFLATED
        return self._controller.inflate(slot, delta)

    def _deflate_one(self, slot: int, delta: int) -> bool:
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        new_target = max(0, chamber.target_pressure - delta)
        chamber.target_pressure = new_target
        if chamber.pressure > new_target:
            chamber.state = ChamberState.DEFLATING
        else:
            chamber.state = ChamberState.IDLE
        return self._controller.deflate(slot, delta)

    def _set_pressure_one(self, slot: int, value: int) -> bool:
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        value = max(0, min(chamber.max_pressure, value))
        chamber.target_pressure = value
        if chamber.pressure < value:
            chamber.state = ChamberState.INFLATING
        elif chamber.pressure > value:
            chamber.state = ChamberState.DEFLATING
        elif value > 0:
            chamber.state = ChamberState.INFLATED
        else:
            chamber.state = ChamberState.IDLE
        return self._controller.set_pressure(slot, value)

    def __repr__(self) -> str:
        slots = list(self._chambers.keys())
        return f"Skin(id={self.skin_id!r}, slots={slots}, esp32={self.esp32_mac!r})"

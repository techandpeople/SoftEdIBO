"""Skin module - a physical unit containing 1 to 3 air chambers.

A Skin is the basic tactile unit in SoftEdIBO. Each skin is a physical
piece that can be touched and contains air chambers that inflate/deflate.

Multiple skins can share the same ESP32 node as long as the total
number of chambers does not exceed 3 (the node's capacity).
"""

import logging
from typing import Any

from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.esp32_controller import ESP32Controller

logger = logging.getLogger(__name__)


class Skin:
    """A physical skin unit with 1-3 air chambers on an ESP32 node."""

    def __init__(
        self,
        skin_id: str,
        controller: ESP32Controller,
        chamber_slots: list[int],
    ):
        """Initialize a skin.

        Args:
            skin_id: Unique identifier for this skin.
            controller: ESP32 controller managing this skin's chambers.
            chamber_slots: Which chamber slots (0-2) on the ESP32 this skin uses.
                e.g. [0] for a 1-chamber skin, [0, 1, 2] for a full 3-chamber skin.
        """
        self.skin_id = skin_id
        self._controller = controller
        self._chambers: dict[int, AirChamber] = {
            slot: AirChamber(chamber_id=slot, esp32_mac=controller.mac_address)
            for slot in chamber_slots
        }

    @property
    def chambers(self) -> dict[int, AirChamber]:
        """Get the air chambers in this skin."""
        return self._chambers

    @property
    def chamber_count(self) -> int:
        """Get the number of chambers in this skin."""
        return len(self._chambers)

    @property
    def esp32_mac(self) -> str:
        """Get the MAC address of the ESP32 controlling this skin."""
        return self._controller.mac_address

    def inflate(self, slot: int | None = None, value: int = 255) -> bool:
        """Inflate a chamber by slot, or all chambers if slot is None."""
        if slot is not None:
            return self._inflate_one(slot, value)
        return all(self._inflate_one(s, value) for s in self._chambers)

    def deflate(self, slot: int | None = None) -> bool:
        """Deflate a chamber by slot, or all chambers if slot is None."""
        if slot is not None:
            return self._deflate_one(slot)
        return all(self._deflate_one(s) for s in self._chambers)

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

    def _inflate_one(self, slot: int, value: int) -> bool:
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        success = self._controller.inflate(slot, value)
        if success:
            chamber.state = ChamberState.INFLATING
        return success

    def _deflate_one(self, slot: int) -> bool:
        chamber = self._chambers.get(slot)
        if chamber is None:
            logger.error("Skin %s has no chamber at slot %d", self.skin_id, slot)
            return False
        success = self._controller.deflate(slot)
        if success:
            chamber.state = ChamberState.DEFLATING
        return success

    def __repr__(self) -> str:
        slots = list(self._chambers.keys())
        return f"Skin(id={self.skin_id!r}, slots={slots}, esp32={self.esp32_mac!r})"

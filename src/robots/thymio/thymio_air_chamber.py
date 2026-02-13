"""Air chambers mounted on top of a Thymio robot.

Each Thymio has air chambers controlled by a dedicated ESP32 node
via ESP-NOW, separate from the Thymio's own communication.
"""

import logging
from typing import Any

from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway

logger = logging.getLogger(__name__)


class ThymioAirChamber:
    """Manages air chambers mounted on a Thymio robot."""

    def __init__(self, thymio_id: str, esp32_mac: str, gateway: ESPNowGateway):
        """Initialize air chambers for a Thymio.

        Args:
            thymio_id: ID of the parent Thymio robot.
            esp32_mac: MAC address of the ESP32 controlling the chambers.
            gateway: ESP-NOW gateway for communication.
        """
        self.thymio_id = thymio_id
        self._controller = ESP32Controller(esp32_mac, gateway)
        self._chamber = AirChamber(chamber_id=0, esp32_mac=esp32_mac)

    @property
    def chamber(self) -> AirChamber:
        """Get the air chamber."""
        return self._chamber

    def inflate(self, value: int = 255) -> bool:
        """Inflate the Thymio's air chamber."""
        success = self._controller.inflate(0, value)
        if success:
            self._chamber.state = ChamberState.INFLATING
        return success

    def deflate(self) -> bool:
        """Deflate the Thymio's air chamber."""
        success = self._controller.deflate(0)
        if success:
            self._chamber.state = ChamberState.DEFLATING
        return success

    def get_status(self) -> dict[str, Any]:
        """Get air chamber status."""
        return {
            "thymio_id": self.thymio_id,
            "state": self._chamber.state.value,
            "pressure": self._chamber.pressure,
        }

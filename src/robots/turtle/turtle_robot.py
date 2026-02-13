"""Turtle robot - large robot with multiple skins for group interaction.

The Turtle has multiple skins, each with 1-3 air chambers. Skins can
share an ESP32 node when they have fewer than 3 chambers.
The entire group interacts with the Turtle simultaneously.
"""

import logging
from typing import Any

from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class TurtleRobot(BaseRobot):
    """Turtle robot with multiple skins for group tactile interaction."""

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        node_configs: list[dict[str, Any]],
    ):
        """Initialize the Turtle robot.

        Args:
            robot_id: Unique identifier for this robot.
            gateway: ESP-NOW gateway for communication.
            node_configs: List of node dicts from settings.yaml, each with
                'mac' and 'skins' keys. Each skin has 'skin_id' and 'slots'.
        """
        super().__init__(robot_id, "Turtle")
        self._gateway = gateway
        self._controllers: dict[str, ESP32Controller] = {}
        self._skins: dict[str, Skin] = {}

        for node in node_configs:
            mac = node["mac"]
            controller = ESP32Controller(mac, gateway)
            self._controllers[mac] = controller

            for skin_cfg in node["skins"]:
                skin = Skin(
                    skin_id=skin_cfg["skin_id"],
                    controller=controller,
                    chamber_slots=skin_cfg["slots"],
                )
                self._skins[skin.skin_id] = skin

    @property
    def skins(self) -> dict[str, Skin]:
        """Get all skins on this Turtle."""
        return self._skins

    @property
    def total_chambers(self) -> int:
        """Get total number of air chambers across all skins."""
        return sum(s.chamber_count for s in self._skins.values())

    def connect(self) -> bool:
        """Connect to the Turtle via the ESP-NOW gateway."""
        if not self._gateway.is_connected:
            logger.error("Gateway not connected")
            return False
        self._status = RobotStatus.CONNECTED
        logger.info(
            "Turtle connected: %d skins, %d chambers",
            len(self._skins), self.total_chambers,
        )
        return True

    def disconnect(self) -> None:
        """Disconnect the Turtle robot."""
        self._status = RobotStatus.DISCONNECTED

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to a specific skin."""
        skin_id = kwargs.get("skin")
        skin = self._skins.get(skin_id)
        if skin is None:
            logger.error("Invalid skin ID: %s", skin_id)
            return False
        slot = kwargs.get("slot")
        if command == "inflate":
            return skin.inflate(slot, kwargs.get("value", 255))
        if command == "deflate":
            return skin.deflate(slot)
        return False

    def inflate_skin(self, skin_id: str, value: int = 255) -> bool:
        """Inflate all chambers in a skin."""
        skin = self._skins.get(skin_id)
        if skin is None:
            return False
        return skin.inflate(value=value)

    def deflate_skin(self, skin_id: str) -> bool:
        """Deflate all chambers in a skin."""
        skin = self._skins.get(skin_id)
        if skin is None:
            return False
        return skin.deflate()

    def inflate_all(self, value: int = 255) -> bool:
        """Inflate all skins simultaneously."""
        return all(s.inflate(value=value) for s in self._skins.values())

    def deflate_all(self) -> bool:
        """Deflate all skins."""
        return all(s.deflate() for s in self._skins.values())

    def get_status_data(self) -> dict[str, Any]:
        """Get status of all skins."""
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "skins": {
                sid: s.get_status() for sid, s in self._skins.items()
            },
        }

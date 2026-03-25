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
        skin_configs: list[dict[str, Any]],
    ):
        """Initialize the Turtle robot.

        Args:
            robot_id: Unique identifier for this robot.
            gateway: ESP-NOW gateway for communication.
            skin_configs: Flat list of skin dicts from settings.yaml, each with
                'skin_id', 'name' (optional), 'mac', and 'slots' keys.
                Multiple skins may share the same MAC (up to 3 slots total per MAC).
        """
        super().__init__(robot_id, "Turtle")
        self._gateway = gateway
        self._controllers: dict[str, ESP32Controller] = {}
        self._skins: dict[str, Skin] = {}

        for skin_cfg in skin_configs:
            mac = skin_cfg["mac"]
            if mac not in self._controllers:
                self._controllers[mac] = ESP32Controller(mac, gateway)
            raw_max = skin_cfg.get("max_pressure", {})
            max_pressure = {int(k): v for k, v in raw_max.items()} if raw_max else None
            skin = Skin(
                skin_id=skin_cfg["skin_id"],
                controller=self._controllers[mac],
                chamber_slots=skin_cfg["slots"],
                name=skin_cfg.get("name"),
                pressure_limits=max_pressure,
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

    def pause(self) -> None:
        """Freeze all chambers — send hold to each chamber on the real hardware."""
        for skin in self._skins.values():
            for slot in skin.chambers:
                skin.hold(slot)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to a specific skin."""
        skin_id = kwargs.get("skin")
        skin = self._skins.get(skin_id)
        if skin is None:
            logger.error("Invalid skin ID: %s", skin_id)
            return False
        slot = kwargs.get("slot")
        if command == "set_pressure":
            return skin.set_pressure(slot, kwargs.get("value", 100))
        if command == "inflate":
            return skin.inflate(slot, kwargs.get("delta", 10))
        if command == "deflate":
            return skin.deflate(slot, kwargs.get("delta", 10))
        if command == "hold":
            return skin.hold(slot)
        return False

    def inflate_skin(self, skin_id: str, value: int = 100) -> bool:
        """Set all chambers in a skin to an absolute target pressure (0-100 %)."""
        skin = self._skins.get(skin_id)
        if skin is None:
            return False
        return skin.set_pressure(value=value)

    def deflate_skin(self, skin_id: str) -> bool:
        """Deflate all chambers in a skin to zero."""
        skin = self._skins.get(skin_id)
        if skin is None:
            return False
        return skin.set_pressure(value=0)

    def inflate_all(self, value: int = 100) -> bool:
        """Set all skins to an absolute target pressure (0-100 %)."""
        return all(s.set_pressure(value=value) for s in self._skins.values())

    def deflate_all(self) -> bool:
        """Deflate all skins to zero."""
        return all(s.set_pressure(value=0) for s in self._skins.values())

    def get_status_data(self) -> dict[str, Any]:
        """Get status of all skins."""
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "skins": {
                sid: s.get_status() for sid, s in self._skins.items()
            },
        }

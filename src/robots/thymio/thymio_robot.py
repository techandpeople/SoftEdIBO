"""Thymio robot control via tdm-client.

Thymio robots are small wheeled robots that move around.
Each Thymio has air chambers mounted on top (controlled by a separate ESP32).
"""

import logging
from typing import Any

from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class ThymioRobot(BaseRobot):
    """Thymio wheeled robot with movement, sensor, and air chamber capabilities."""

    def __init__(
        self,
        robot_id: str,
        tdm_host: str = "localhost",
        tdm_port: int = 8596,
        gateway: ESPNowGateway | None = None,
        skin_configs: list[dict[str, Any]] | None = None,
    ):
        """Initialize the Thymio robot.

        Args:
            robot_id: Unique identifier for this Thymio.
            tdm_host: Thymio Device Manager host address.
            tdm_port: Thymio Device Manager port.
            gateway: ESP-NOW gateway for air chamber communication.
            skin_configs: Flat list of skin dicts with 'skin_id', 'name', 'mac', 'slots'.
        """
        super().__init__(robot_id, f"Thymio-{robot_id}")
        self._tdm_host = tdm_host
        self._tdm_port = tdm_port
        self._node = None  # tdm-client node, set on connect
        self._controllers: dict[str, ESP32Controller] = {}
        self._skins: dict[str, Skin] = {}

        if gateway and skin_configs:
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
        """Get all air chamber skins on this Thymio."""
        return self._skins

    def connect(self) -> bool:
        """Connect to the Thymio via Thymio Device Manager."""
        self._status = RobotStatus.CONNECTING
        try:
            # TODO: Implement tdm-client connection
            # from tdmclient import ClientAsync
            # self._client = ClientAsync(host=self._tdm_host, port=self._tdm_port)
            self._status = RobotStatus.CONNECTED
            logger.info("Thymio %s connected", self.robot_id)
            return True
        except Exception:
            self._status = RobotStatus.ERROR
            logger.exception("Failed to connect Thymio %s", self.robot_id)
            return False

    def disconnect(self) -> None:
        """Disconnect from the Thymio."""
        self._node = None
        self._status = RobotStatus.DISCONNECTED
        logger.info("Thymio %s disconnected", self.robot_id)

    def pause(self) -> None:
        """Freeze all air chambers — send hold to each chamber on the real hardware."""
        for skin in self._skins.values():
            for slot in skin.chambers:
                skin.hold(slot)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a movement or action command to the Thymio."""
        if self._status != RobotStatus.CONNECTED:
            logger.error("Thymio %s not connected", self.robot_id)
            return False

        skin_id = kwargs.get("skin")
        if skin_id and skin_id in self._skins:
            skin = self._skins[skin_id]
            slot = kwargs.get("slot")
            if command == "set_pressure":
                return skin.set_pressure(slot, kwargs.get("value", 100))
            if command == "inflate":
                return skin.inflate(slot, kwargs.get("delta", 10))
            if command == "deflate":
                return skin.deflate(slot, kwargs.get("delta", 10))
            if command == "hold":
                return skin.hold(slot)

        # TODO: Implement tdm-client movement commands
        logger.debug("Thymio %s command: %s %s", self.robot_id, command, kwargs)
        return True

    def set_motors(self, left: int, right: int) -> bool:
        """Set left and right motor speeds (-500 to 500)."""
        return self.send_command("motors", left=left, right=right)

    def stop(self) -> bool:
        """Stop the Thymio motors."""
        return self.set_motors(0, 0)

    def set_leds(self, r: int, g: int, b: int) -> bool:
        """Set the Thymio top LEDs color (0-32 each)."""
        return self.send_command("leds", r=r, g=g, b=b)

    def get_status_data(self) -> dict[str, Any]:
        """Get Thymio sensor and status data."""
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "skins": {sid: s.get_status() for sid, s in self._skins.items()},
            "sensors": {},  # TODO: Read from tdm-client
        }

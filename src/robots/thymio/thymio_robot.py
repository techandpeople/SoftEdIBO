"""Thymio robot — wheeled tdm-client robot with optional ESP-NOW skin nodes."""

import logging
from typing import Any

from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.esp_robot import EspRobot
from src.robots.base_robot import RobotStatus

logger = logging.getLogger(__name__)


class ThymioRobot(EspRobot):
    """Thymio wheeled robot with movement, sensor, and air chamber capabilities.

    The ESP-NOW node side is identical to other robots and lives in EspRobot.
    Thymio adds the tdm-client connection and motor / LED commands.
    """

    def __init__(
        self,
        robot_id: str,
        tdm_host: str = "localhost",
        tdm_port: int = 8596,
        gateway: ESPNowGateway | None = None,
        node_configs: list[dict[str, Any]] | None = None,
        skin_configs: list[dict[str, Any]] | None = None,
        reservoir_configs: dict[str, Any] | None = None,
    ):
        super().__init__(
            robot_id, f"Thymio-{robot_id}",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
            reservoir_configs=reservoir_configs,
        )
        self._tdm_host = tdm_host
        self._tdm_port = tdm_port
        self._tdm_node = None

    # ------------------------------------------------------------------
    # Lifecycle (overrides EspRobot to add tdm-client)
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        self._status = RobotStatus.CONNECTING
        try:
            # TODO: Implement tdm-client connection.
            self._status = RobotStatus.CONNECTED
            logger.info("Thymio %s connected", self.robot_id)
            return True
        except Exception:
            self._status = RobotStatus.ERROR
            logger.exception("Failed to connect Thymio %s", self.robot_id)
            return False

    def disconnect(self) -> None:
        self._tdm_node = None
        super().disconnect()

    # ------------------------------------------------------------------
    # Commanding (skins go through EspRobot; movement is Thymio-specific)
    # ------------------------------------------------------------------

    def send_command(self, command: str, **kwargs: Any) -> bool:
        if self._status != RobotStatus.CONNECTED:
            return False
        if kwargs.get("skin"):
            return super().send_command(command, **kwargs)
        # TODO: tdm-client movement / LED commands.
        logger.debug("Thymio %s command: %s %s", self.robot_id, command, kwargs)
        return True

    def set_motors(self, left: int, right: int) -> bool:
        return self.send_command("motors", left=left, right=right)

    def stop(self) -> bool:
        return self.set_motors(0, 0)

    def set_leds(self, r: int, g: int, b: int) -> bool:
        return self.send_command("leds", r=r, g=g, b=b)

    def get_status_data(self) -> dict[str, Any]:
        return {**super().get_status_data(), "sensors": {}}

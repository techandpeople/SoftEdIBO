"""Thymio robot control via tdm-client.

Thymio robots are small wheeled robots that move around.
Each Thymio has air chambers mounted on top (controlled by a separate ESP32).
"""

import logging
from typing import Any

from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class ThymioRobot(BaseRobot):
    """Thymio wheeled robot with movement and sensor capabilities."""

    def __init__(self, robot_id: str, tdm_host: str = "localhost", tdm_port: int = 8596):
        """Initialize the Thymio robot.

        Args:
            robot_id: Unique identifier for this Thymio.
            tdm_host: Thymio Device Manager host address.
            tdm_port: Thymio Device Manager port.
        """
        super().__init__(robot_id, f"Thymio-{robot_id}")
        self._tdm_host = tdm_host
        self._tdm_port = tdm_port
        self._node = None  # tdm-client node, set on connect

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

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a movement or action command to the Thymio."""
        if self._status != RobotStatus.CONNECTED:
            logger.error("Thymio %s not connected", self.robot_id)
            return False

        # TODO: Implement command dispatch via tdm-client
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
            "sensors": {},  # TODO: Read from tdm-client
        }

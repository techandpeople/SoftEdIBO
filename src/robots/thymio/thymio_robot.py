"""Thymio robot — wheeled tdm-client robot with optional ESP-NOW skin nodes."""

import logging
from typing import Any

from src.hardware.gateway import Gateway
from src.robots.esp_robot import EspRobot
from src.robots.base_robot import RobotStatus
from src.robots.thymio.thymio_link import ThymioLink

logger = logging.getLogger(__name__)


class ThymioRobot(EspRobot):
    """Thymio wheeled robot with movement, sensor, and air chamber capabilities.

    The ESP-NOW node side (air chambers) is identical to other robots and lives in
    EspRobot. Thymio adds the wheeled base: motors and LEDs driven through an injected
    link — either a :class:`ThymioLink` (RF dongle via thymiodirect) or a
    :class:`~src.robots.thymio.thymio_gateway_link.ThymioGatewayLink` (the gateway's C6
    over 802.15.4, no dongle). Both share the same duck-typed interface. When no link is
    given the movement commands are no-ops and ``connect`` still succeeds, so the sim /
    no-hardware path is unchanged.
    """

    robot_kind = "thymio"

    def __init__(
        self,
        robot_id: str,
        gateway: Gateway | None = None,
        node_configs: list[dict[str, Any]] | None = None,
        skin_configs: list[dict[str, Any]] | None = None,
        link: ThymioLink | None = None,
    ):
        super().__init__(
            robot_id, f"Thymio-{robot_id}",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
        )
        self._link = link

    # ------------------------------------------------------------------
    # Lifecycle (overrides EspRobot to add the wheeled base)
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        self._status = RobotStatus.CONNECTING
        if self._link is not None and not self._link.connect():
            self._status = RobotStatus.ERROR
            logger.error("Thymio %s: could not reach the wheeled base (TDM/dongle)", self.robot_id)
            return False
        self._status = RobotStatus.CONNECTED
        logger.info("Thymio %s connected%s", self.robot_id,
                    "" if self._link else " (no wheeled base link)")
        return True

    def disconnect(self) -> None:
        if self._link is not None:
            self._link.close()
        super().disconnect()

    # ------------------------------------------------------------------
    # Commanding (skins go through EspRobot; movement is Thymio-specific)
    # ------------------------------------------------------------------

    def send_command(self, command: str, **kwargs: Any) -> bool:
        if self._status != RobotStatus.CONNECTED:
            return False
        if kwargs.get("skin"):
            return super().send_command(command, **kwargs)
        if command == "motors":
            return self.set_motors(kwargs.get("left", 0), kwargs.get("right", 0))
        if command == "leds":
            return self.set_leds(kwargs.get("r", 0), kwargs.get("g", 0), kwargs.get("b", 0))
        if command == "stop":
            return self.stop()
        logger.debug("Thymio %s: unhandled command %r", self.robot_id, command)
        return False

    def set_motors(self, left: int, right: int) -> bool:
        if self._link is not None:
            return self._link.set_motors(left, right)
        logger.debug("Thymio %s set_motors %d %d (no link)", self.robot_id, left, right)
        return True

    def set_leds(self, r: int, g: int, b: int) -> bool:
        if self._link is not None:
            return self._link.set_leds(r, g, b)
        logger.debug("Thymio %s set_leds %d %d %d (no link)", self.robot_id, r, g, b)
        return True

    def play_sound(self, system: int | None = None, freq: int | None = None,
                   duration_ms: int = 500) -> bool:
        """Beep a built-in ``system`` sound (0-7) or a ``freq`` Hz tone (no-op w/o a link)."""
        if self._link is not None:
            return self._link.play_sound(system=system, freq=freq,
                                         duration_ms=duration_ms)
        logger.debug("Thymio %s play_sound (no link)", self.robot_id)
        return True

    def stop(self) -> bool:
        return self.set_motors(0, 0)

    def emergency_stop(self) -> None:
        # Stop the wheels too, then latch the air nodes off (EspRobot).
        self.stop()
        super().emergency_stop()

    def get_status_data(self) -> dict[str, Any]:
        return {**super().get_status_data(), "sensors": {}}

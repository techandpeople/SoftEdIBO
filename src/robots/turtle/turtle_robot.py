"""Turtle robot — large robot with multiple skins for group interaction."""

from typing import Any

from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.esp_robot import EspRobot


class TurtleRobot(EspRobot):
    """Turtle robot with multiple skins for group tactile interaction.

    All controller / skin / reservoir / pause / send_command logic lives in
    EspRobot. This class only fixes the kind label.
    """

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
        reservoir_configs: dict[str, Any] | None = None,
    ):
        super().__init__(
            robot_id, "Turtle",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
            reservoir_configs=reservoir_configs,
        )

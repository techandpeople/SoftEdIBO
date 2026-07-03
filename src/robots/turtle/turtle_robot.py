"""Turtle robot — one shared body, chambers used by the whole group.

Same ESP-NOW hardware as the Tree (nodes driving inflatable skins); what makes
it a Turtle is its own hardware set and the turtle skins fitted on top
(``skin_type`` ``turtle_*``). All chamber / command behaviour lives in
:class:`~src.robots.esp_robot.EspRobot`; unlike the Tree, the Turtle's chambers
are shared by the children, so there is no per-skin ownership bookkeeping.
"""

from typing import Any

from src.hardware.gateway import Gateway
from src.robots.esp_robot import EspRobot


class TurtleRobot(EspRobot):
    """Turtle robot: multiple skins for shared group tactile interaction."""

    robot_kind = "turtle"

    def __init__(
        self,
        robot_id: str,
        gateway: Gateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
    ):
        super().__init__(
            robot_id, "Turtle",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
        )

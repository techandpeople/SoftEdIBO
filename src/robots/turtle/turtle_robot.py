"""Turtle robot - one shared body, chambers used by the whole group.

Same ESP-NOW hardware as the Tree - in fact the SAME board: one
``node_multiplexed`` PCB is moved between the two bodies, and both robots stay
configured on that MAC. What makes it a Turtle is the body it is mounted in and
the turtle skins fitted on top (``skin_type`` ``turtle_*``); the board-level
state that differs between the two is (re)pushed by ``claim_board``.

All chamber / command behaviour lives in :class:`~src.robots.esp_robot.EspRobot`; unlike the Tree, the Turtle's chambers
are shared by the children, so there is no per-skin ownership bookkeeping.
"""

from typing import Any

from src.hardware.gateway import Gateway
from src.hardware.node_registry import NodeRegistry
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
        registry: NodeRegistry | None = None,
    ):
        super().__init__(
            robot_id, "Turtle",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
            registry=registry,
        )

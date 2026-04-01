"""Simulation activity — mock touch interaction with pressure animation.

Works with any robot type. Substitutes real robots with SimulatedRobot instances
backed by SimulatedController so the GUI layer is completely hardware-agnostic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.activities.base_activity import BaseActivity
from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session
    from src.robots.simulated_robot import SimulatedRobot

logger = logging.getLogger(__name__)


class SimulationActivity(BaseActivity):
    """Activity that replaces real robots with simulated ones backed by mock hardware.

    The monitor receives SimulatedRobot instances — no simulation awareness needed
    in any GUI widget. Touch press/release drives inflate/deflate via SimulatedController.
    """

    robot_type = BaseRobot

    def __init__(self) -> None:
        super().__init__(
            name="Simulation",
            description="Mock touch interactions with animated pressure response.",
        )
        self._is_running = False
        self._simulated_robots: list[SimulatedRobot] = []

    def prepare_robots(self, robots: list[BaseRobot]) -> list[BaseRobot]:
        """Return SimulatedRobot instances mirroring each robot's skin configuration."""
        from src.robots.simulated_robot import SimulatedRobot

        self._simulated_robots = []
        for robot in robots:
            skins = getattr(robot, "skins", {})
            skin_configs = [
                {
                    "skin_id":  skin.skin_id,
                    "name":     skin.name,
                    "chambers": skin.chamber_defs,
                }
                for skin in skins.values()
            ]
            sim = SimulatedRobot(robot.robot_id, robot.name, skin_configs)
            self._simulated_robots.append(sim)
            logger.debug("Created SimulatedRobot for %s", robot.robot_id)

        return self._simulated_robots  # type: ignore[return-value]

    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        logger.info("Simulation activity set up with %d robots", len(robots))

    def start(self) -> None:
        self._is_running = True
        logger.info("Simulation activity started")

    def pause(self) -> None:
        for robot in self._simulated_robots:
            robot.pause()
        logger.info("Simulation activity paused")

    def resume(self) -> None:
        for robot in self._simulated_robots:
            robot.resume()
        logger.info("Simulation activity resumed")

    def stop(self) -> None:
        self._is_running = False
        for robot in self._simulated_robots:
            robot.disconnect()
        self._simulated_robots = []
        logger.info("Simulation activity stopped")

    def get_state(self) -> dict[str, Any]:
        return {"name": self.name, "is_running": self._is_running}

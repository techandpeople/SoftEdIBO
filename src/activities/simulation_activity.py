"""Simulation activity — runs in simulation mode by default.

Kept as a thin convenience entry so users can pick "Simulation" from the
SessionPanel and get the simulated experience without touching the
simulation_mode flag. The actual robot wrapping lives in BaseActivity now,
so any other activity can also run in simulation by setting
``simulation_mode = True`` on the instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.activities.base_activity import BaseActivity
from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session

logger = logging.getLogger(__name__)


class SimulationActivity(BaseActivity):
    """Generic "test in simulation" activity.

    Always runs with ``simulation_mode = True`` — picks up whatever robots
    are configured, swaps them for SimulatedRobot equivalents, and lets the
    operator interact via the normal monitor widgets.
    """

    robot_type = BaseRobot
    simulation_mode = True

    def __init__(self) -> None:
        super().__init__(
            name="Simulation",
            description="Mock touch interactions with animated pressure response.",
        )
        self._is_running = False
        self._robots: list[BaseRobot] = []

    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        self._robots = robots
        logger.info("Simulation activity set up with %d robots", len(robots))

    def start(self) -> None:
        self._is_running = True
        logger.info("Simulation activity started")

    def pause(self) -> None:
        for robot in self._robots:
            robot.pause()
        logger.info("Simulation activity paused")

    def resume(self) -> None:
        for robot in self._robots:
            robot.resume()
        logger.info("Simulation activity resumed")

    def stop(self) -> None:
        self._is_running = False
        for robot in self._robots:
            robot.disconnect()
        self._robots = []
        logger.info("Simulation activity stopped")

    def get_state(self) -> dict[str, Any]:
        return {"name": self.name, "is_running": self._is_running}

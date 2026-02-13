"""Group touch activity - all participants interact with a robot together.

This activity is designed for the Turtle robot where the entire group
touches and interacts with the air chambers simultaneously.
"""

import logging
from typing import Any

from src.activities.base_activity import BaseActivity
from src.core.session import Session
from src.robots.base_robot import BaseRobot

logger = logging.getLogger(__name__)


class GroupTouchActivity(BaseActivity):
    """Activity where the group interacts with a robot through touch."""

    def __init__(self):
        super().__init__(
            name="Group Touch",
            description="All participants interact with the robot's air chambers together.",
        )
        self._session: Session | None = None
        self._robots: list[BaseRobot] = []
        self._is_running = False

    def setup(self, session: Session, robots: list[BaseRobot]) -> None:
        """Prepare the group touch activity."""
        self._session = session
        self._robots = robots
        logger.info("Group Touch activity set up with %d robots", len(robots))

    def start(self) -> None:
        """Start the group touch activity."""
        self._is_running = True
        logger.info("Group Touch activity started")

    def stop(self) -> None:
        """Stop the group touch activity."""
        self._is_running = False
        logger.info("Group Touch activity stopped")

    def get_state(self) -> dict[str, Any]:
        """Get current activity state."""
        return {
            "name": self.name,
            "is_running": self._is_running,
            "robot_count": len(self._robots),
        }

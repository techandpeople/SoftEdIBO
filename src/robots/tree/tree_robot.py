"""Tree robot - each person has their own branch and can share with others.

The Tree robot has multiple branches, each controlled individually.
Participants can interact with their own branch and optionally share
branches with other participants, promoting inclusion.
"""

import logging
from typing import Any

from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.base_robot import BaseRobot, RobotStatus
from src.robots.tree.branch import Branch

logger = logging.getLogger(__name__)


class TreeRobot(BaseRobot):
    """Tree robot with individual and shareable branches."""

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        esp32_mac: str,
        branch_ids: list[int],
    ):
        """Initialize the Tree robot.

        Args:
            robot_id: Unique identifier for this tree.
            gateway: ESP-NOW gateway for communication.
            esp32_mac: MAC address of the ESP32 controlling the branches.
            branch_ids: List of branch IDs available on this tree.
        """
        super().__init__(robot_id, "Tree")
        self._gateway = gateway
        self._esp32_mac = esp32_mac
        self._branches: dict[int, Branch] = {
            bid: Branch(bid, esp32_mac, gateway) for bid in branch_ids
        }

    @property
    def branches(self) -> dict[int, Branch]:
        """Get all branches."""
        return self._branches

    def connect(self) -> bool:
        """Connect to the Tree via the ESP-NOW gateway."""
        if not self._gateway.is_connected:
            logger.error("Gateway not connected")
            return False
        self._status = RobotStatus.CONNECTED
        logger.info("Tree robot connected with %d branches", len(self._branches))
        return True

    def disconnect(self) -> None:
        """Disconnect the Tree robot."""
        self._status = RobotStatus.DISCONNECTED

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to a specific branch."""
        branch_id = kwargs.get("branch")
        branch = self._branches.get(branch_id)
        if branch is None:
            logger.error("Invalid branch ID: %s", branch_id)
            return False
        return branch.send_command(command, **kwargs)

    def get_status_data(self) -> dict[str, Any]:
        """Get status of all branches."""
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "branches": {
                bid: b.get_status() for bid, b in self._branches.items()
            },
        }

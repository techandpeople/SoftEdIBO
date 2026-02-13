"""Branch of a Tree robot.

Each branch can be assigned to a participant and optionally shared
between participants to promote inclusive interaction.
"""

import logging
from typing import Any

from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway

logger = logging.getLogger(__name__)


class Branch:
    """A single branch on a Tree robot."""

    def __init__(self, branch_id: int, esp32_mac: str, gateway: ESPNowGateway):
        self.branch_id = branch_id
        self._controller = ESP32Controller(esp32_mac, gateway)
        self._chamber = AirChamber(chamber_id=branch_id, esp32_mac=esp32_mac)
        self._owner: str | None = None
        self._shared_with: list[str] = []

    @property
    def owner(self) -> str | None:
        """Get the participant ID who owns this branch."""
        return self._owner

    def assign_to(self, participant_id: str) -> None:
        """Assign this branch to a participant."""
        self._owner = participant_id
        self._shared_with = []
        logger.info("Branch %d assigned to %s", self.branch_id, participant_id)

    def share_with(self, participant_id: str) -> None:
        """Share this branch with another participant."""
        if participant_id not in self._shared_with:
            self._shared_with.append(participant_id)
            logger.info(
                "Branch %d shared with %s (owner: %s)",
                self.branch_id, participant_id, self._owner,
            )

    def unshare(self, participant_id: str) -> None:
        """Remove sharing with a participant."""
        if participant_id in self._shared_with:
            self._shared_with.remove(participant_id)

    def inflate(self, value: int = 255) -> bool:
        """Inflate this branch's air chamber."""
        success = self._controller.inflate(self.branch_id, value)
        if success:
            self._chamber.state = ChamberState.INFLATING
        return success

    def deflate(self) -> bool:
        """Deflate this branch's air chamber."""
        success = self._controller.deflate(self.branch_id)
        if success:
            self._chamber.state = ChamberState.DEFLATING
        return success

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a generic command to this branch."""
        return self._controller.send_command(command, chamber=self.branch_id, **kwargs)

    def get_status(self) -> dict[str, Any]:
        """Get branch status including ownership."""
        return {
            "branch_id": self.branch_id,
            "state": self._chamber.state.value,
            "pressure": self._chamber.pressure,
            "owner": self._owner,
            "shared_with": self._shared_with.copy(),
        }

"""Tree robot - each person has their own skin/branch and can share with others.

The Tree robot has multiple skins (one per branch), each controlled individually.
Participants can interact with their own skin and optionally share with others.
"""

import logging
from typing import Any

from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class TreeRobot(BaseRobot):
    """Tree robot with individual and shareable skins (branches)."""

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        skin_configs: list[dict[str, Any]],
    ):
        """Initialize the Tree robot.

        Args:
            robot_id: Unique identifier for this tree.
            gateway: ESP-NOW gateway for communication.
            skin_configs: Flat list of skin dicts from settings.yaml, each with
                'skin_id', 'name' (optional), 'mac', and 'slots' keys.
        """
        super().__init__(robot_id, "Tree")
        self._gateway = gateway
        self._controllers: dict[str, ESP32Controller] = {}
        self._skins: dict[str, Skin] = {}
        self._owners: dict[str, str | None] = {}   # skin_id => participant_id
        self._shared: dict[str, list[str]] = {}     # skin_id => [participant_ids]

        for skin_cfg in skin_configs:
            mac = skin_cfg["mac"]
            if mac not in self._controllers:
                self._controllers[mac] = ESP32Controller(mac, gateway)
            raw_max = skin_cfg.get("max_pressure", {})
            max_pressure = {int(k): v for k, v in raw_max.items()} if raw_max else None
            skin = Skin(
                skin_id=skin_cfg["skin_id"],
                controller=self._controllers[mac],
                chamber_slots=skin_cfg["slots"],
                name=skin_cfg.get("name"),
                pressure_limits=max_pressure,
            )
            self._skins[skin.skin_id] = skin
            self._owners[skin.skin_id] = None
            self._shared[skin.skin_id] = []

    @property
    def skins(self) -> dict[str, Skin]:
        """Get all skins on this Tree."""
        return self._skins

    def assign_to(self, skin_id: str, participant_id: str) -> None:
        """Assign a skin to a participant (clears sharing)."""
        if skin_id in self._owners:
            self._owners[skin_id] = participant_id
            self._shared[skin_id] = []

    def share_with(self, skin_id: str, participant_id: str) -> None:
        """Share a skin with an additional participant."""
        if skin_id in self._shared and participant_id not in self._shared[skin_id]:
            self._shared[skin_id].append(participant_id)

    def unshare(self, skin_id: str, participant_id: str) -> None:
        """Remove a participant from a skin's sharing list."""
        if skin_id in self._shared:
            self._shared[skin_id] = [
                p for p in self._shared[skin_id] if p != participant_id
            ]

    def get_owner(self, skin_id: str) -> str | None:
        return self._owners.get(skin_id)

    def get_shared(self, skin_id: str) -> list[str]:
        return list(self._shared.get(skin_id, []))

    def connect(self) -> bool:
        """Connect to the Tree via the ESP-NOW gateway."""
        if not self._gateway.is_connected:
            logger.error("Gateway not connected")
            return False
        self._status = RobotStatus.CONNECTED
        logger.info("Tree robot connected with %d skins", len(self._skins))
        return True

    def disconnect(self) -> None:
        """Disconnect the Tree robot."""
        self._status = RobotStatus.DISCONNECTED

    def pause(self) -> None:
        """Freeze all chambers — send hold to each chamber on the real hardware."""
        for skin in self._skins.values():
            for slot in skin.chambers:
                skin.hold(slot)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to a specific skin."""
        skin_id = kwargs.get("skin")
        skin = self._skins.get(skin_id)
        if skin is None:
            logger.error("Invalid skin ID: %s", skin_id)
            return False
        slot = kwargs.get("slot")
        if command == "set_pressure":
            return skin.set_pressure(slot, kwargs.get("value", 100))
        if command == "inflate":
            return skin.inflate(slot, kwargs.get("delta", 10))
        if command == "deflate":
            return skin.deflate(slot, kwargs.get("delta", 10))
        if command == "hold":
            return skin.hold(slot)
        return False

    def get_status_data(self) -> dict[str, Any]:
        """Get status of all skins."""
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "skins": {sid: s.get_status() for sid, s in self._skins.items()},
            "owners": dict(self._owners),
        }

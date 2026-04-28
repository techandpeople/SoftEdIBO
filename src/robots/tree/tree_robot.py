"""Tree robot — each person has their own skin/branch and can share with others."""

from typing import Any

from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.esp_robot import EspRobot


class TreeRobot(EspRobot):
    """Tree robot with individual and shareable skins (branches).

    Adds owner / sharing bookkeeping on top of the standard ESP-NOW skin
    behaviour inherited from EspRobot.
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
            robot_id, "Tree",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
            reservoir_configs=reservoir_configs,
        )
        self._owners: dict[str, str | None] = dict.fromkeys(self._skins, None)
        self._shared: dict[str, list[str]] = {sid: [] for sid in self._skins}

    # ------------------------------------------------------------------
    # Ownership / sharing
    # ------------------------------------------------------------------

    def assign_to(self, skin_id: str, participant_id: str) -> None:
        if skin_id in self._owners:
            self._owners[skin_id] = participant_id
            self._shared[skin_id] = []

    def share_with(self, skin_id: str, participant_id: str) -> None:
        if skin_id in self._shared and participant_id not in self._shared[skin_id]:
            self._shared[skin_id].append(participant_id)

    def unshare(self, skin_id: str, participant_id: str) -> None:
        if skin_id in self._shared:
            self._shared[skin_id] = [p for p in self._shared[skin_id] if p != participant_id]

    def get_owner(self, skin_id: str) -> str | None:
        return self._owners.get(skin_id)

    def get_shared(self, skin_id: str) -> list[str]:
        return list(self._shared.get(skin_id, []))

    def get_status_data(self) -> dict[str, Any]:
        return {**super().get_status_data(), "owners": dict(self._owners)}

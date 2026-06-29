"""Turtle & Tree robot — one robot kind, many skins.

Turtle and Tree are the same hardware: ESP-NOW nodes driving inflatable skins.
The only thing that differs between them is the skins fitted on top (a Turtle's
flat pads vs a Tree's round branches), which is already a per-skin ``skin_type``
choice. So they share a single robot class; all chamber / command behaviour
lives in :class:`~src.robots.esp_robot.EspRobot`.

On top of that this class keeps the optional owner / sharing bookkeeping that a
Tree-style deployment uses to track which participant a given skin (branch)
belongs to, and who else it is currently shared with.
"""

from typing import Any

from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.esp_robot import EspRobot


class TurtleTreeRobot(EspRobot):
    """Robot with multiple skins, usable as a Turtle or a Tree.

    Adds owner / sharing bookkeeping on top of the standard ESP-NOW skin
    behaviour inherited from :class:`EspRobot`. The ownership map is keyed by
    skin id, so it works whether the skins are Turtle pads or Tree branches.
    """

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
    ):
        super().__init__(
            robot_id, "Turtle & Tree",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
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

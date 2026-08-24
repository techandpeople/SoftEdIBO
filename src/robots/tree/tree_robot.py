"""Tree robot - each child has their own skin/branch and can share it.

Same ESP-NOW hardware as the Turtle - in fact the SAME board, swapped from one
body to the other with both robots left configured on that MAC. What makes it a
Tree is the body it is mounted in and the round branch skins fitted on top
(``skin_type`` ``tree_round``). All chamber / command behaviour lives in
:class:`~src.robots.esp_robot.EspRobot`; on top of that this class keeps the
owner / sharing bookkeeping that tracks which participant a branch belongs to
and who else it is currently shared with.
"""

from typing import Any

from src.hardware.gateway import Gateway
from src.hardware.node_registry import NodeRegistry
from src.robots.esp_robot import EspRobot


class TreeRobot(EspRobot):
    """Tree robot with individual, shareable branch skins.

    The ownership map is keyed by skin id (one skin per branch).
    """

    robot_kind = "tree"

    def __init__(
        self,
        robot_id: str,
        gateway: Gateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
        registry: NodeRegistry | None = None,
    ):
        super().__init__(
            robot_id, "Tree",
            gateway=gateway,
            node_configs=node_configs,
            skin_configs=skin_configs,
            registry=registry,
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

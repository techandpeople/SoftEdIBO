"""Tree robot — each person has their own skin/branch and can share with others."""

import logging
from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots._robot_builder import build_reservoirs, build_skins
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class TreeRobot(BaseRobot):
    """Tree robot with individual and shareable skins (branches)."""

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
        reservoir_configs: dict[str, Any] | None = None,
    ):
        super().__init__(robot_id, "Tree")
        self._gateway = gateway
        self._controllers: dict[str, ESP32Controller] = {
            n["mac"]: ESP32Controller(n["mac"], gateway)
            for n in node_configs
        }
        if reservoir_configs:
            for cfg in reservoir_configs.values():
                mac = cfg.get("mac", "")
                if mac and mac not in self._controllers:
                    self._controllers[mac] = ESP32Controller(mac, gateway)

        self._skins: dict[str, Skin] = build_skins(skin_configs, self._controllers)
        self._reservoirs: dict[str, AirReservoir] = build_reservoirs(
            reservoir_configs, self._controllers
        )
        self._owners: dict[str, str | None] = {sid: None for sid in self._skins}
        self._shared: dict[str, list[str]] = {sid: [] for sid in self._skins}

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    @property
    def pressure_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("pressure")

    @property
    def vacuum_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("vacuum")

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

    def connect(self) -> bool:
        if not self._gateway.is_connected:
            logger.error("Gateway not connected")
            return False
        self._status = RobotStatus.CONNECTED
        logger.info("Tree %s connected with %d skins", self.robot_id, len(self._skins))
        return True

    def disconnect(self) -> None:
        self._status = RobotStatus.DISCONNECTED

    def pause(self) -> None:
        for skin in self._skins.values():
            for local_idx in skin.chambers:
                skin.hold(local_idx)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        skin = self._skins.get(kwargs.get("skin", ""))
        if skin is None:
            logger.error("Invalid skin ID: %s", kwargs.get("skin"))
            return False
        idx = kwargs.get("slot")
        if command == "set_pressure":
            return skin.set_pressure(idx, kwargs.get("value", 100))
        if command == "inflate":
            return skin.inflate(idx, kwargs.get("delta", 10))
        if command == "deflate":
            return skin.deflate(idx, kwargs.get("delta", 10))
        if command == "hold":
            return skin.hold(idx)
        return False

    def get_status_data(self) -> dict[str, Any]:
        return {
            "robot_id":   self.robot_id,
            "status":     self._status.value,
            "skins":      {sid: s.get_status() for sid, s in self._skins.items()},
            "reservoirs": {k: r.get_status() for k, r in self._reservoirs.items()},
            "owners":     dict(self._owners),
        }

"""Turtle robot — large robot with multiple skins for group interaction."""

import logging
from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots._robot_builder import build_reservoirs, build_skins
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class TurtleRobot(BaseRobot):
    """Turtle robot with multiple skins for group tactile interaction."""

    def __init__(
        self,
        robot_id: str,
        gateway: ESPNowGateway,
        node_configs: list[dict[str, Any]],
        skin_configs: list[dict[str, Any]],
        reservoir_configs: dict[str, Any] | None = None,
    ):
        """Initialize the Turtle robot.

        Args:
            robot_id:          Unique identifier.
            gateway:           Shared ESP-NOW gateway.
            node_configs:      List of node dicts, e.g.::

                [{"mac": "AA:BB:...", "node_type": "standard", "max_slots": 3}]

            skin_configs:      List of skin dicts, e.g.::

                [{"skin_id": "belly", "name": "Belly",
                  "chambers": [{"mac": "AA:BB:...", "slot": 0,
                                "max_pressure": 8.0}]}]

            reservoir_configs: Optional ``{"pressure": {...}, "vacuum": {...}}``.
        """
        super().__init__(robot_id, "Turtle")
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

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    @property
    def pressure_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("pressure")

    @property
    def vacuum_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("vacuum")

    @property
    def total_chambers(self) -> int:
        return sum(s.chamber_count for s in self._skins.values())

    def connect(self) -> bool:
        if not self._gateway.is_connected:
            logger.error("Gateway not connected")
            return False
        self._status = RobotStatus.CONNECTED
        logger.info(
            "Turtle %s connected: %d skins, %d chambers",
            self.robot_id, len(self._skins), self.total_chambers,
        )
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

    def inflate_skin(self, skin_id: str, value: int = 100) -> bool:
        skin = self._skins.get(skin_id)
        return skin.set_pressure(value=value) if skin else False

    def deflate_skin(self, skin_id: str) -> bool:
        skin = self._skins.get(skin_id)
        return skin.set_pressure(value=0) if skin else False

    def inflate_all(self, value: int = 100) -> bool:
        return all(s.set_pressure(value=value) for s in self._skins.values())

    def deflate_all(self) -> bool:
        return all(s.set_pressure(value=0) for s in self._skins.values())

    def get_status_data(self) -> dict[str, Any]:
        return {
            "robot_id":   self.robot_id,
            "status":     self._status.value,
            "skins":      {sid: s.get_status() for sid, s in self._skins.items()},
            "reservoirs": {k: r.get_status() for k, r in self._reservoirs.items()},
        }

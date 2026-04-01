"""Thymio robot control via tdm-client."""

import logging
from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots._robot_builder import build_reservoirs, build_skins
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class ThymioRobot(BaseRobot):
    """Thymio wheeled robot with movement, sensor, and air chamber capabilities."""

    def __init__(
        self,
        robot_id: str,
        tdm_host: str = "localhost",
        tdm_port: int = 8596,
        gateway: ESPNowGateway | None = None,
        node_configs: list[dict[str, Any]] | None = None,
        skin_configs: list[dict[str, Any]] | None = None,
        reservoir_configs: dict[str, Any] | None = None,
    ):
        super().__init__(robot_id, f"Thymio-{robot_id}")
        self._tdm_host = tdm_host
        self._tdm_port = tdm_port
        self._node = None
        self._controllers: dict[str, ESP32Controller] = {}
        self._skins: dict[str, Skin] = {}
        self._reservoirs: dict[str, AirReservoir] = {}

        if gateway and node_configs:
            self._controllers = {
                n["mac"]: ESP32Controller(n["mac"], gateway)
                for n in node_configs
            }
            if reservoir_configs:
                for cfg in reservoir_configs.values():
                    mac = cfg.get("mac", "")
                    if mac and mac not in self._controllers:
                        self._controllers[mac] = ESP32Controller(mac, gateway)
            self._skins = build_skins(skin_configs or [], self._controllers)
            self._reservoirs = build_reservoirs(reservoir_configs, self._controllers)

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    @property
    def pressure_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("pressure")

    @property
    def vacuum_reservoir(self) -> AirReservoir | None:
        return self._reservoirs.get("vacuum")

    def connect(self) -> bool:
        self._status = RobotStatus.CONNECTING
        try:
            # TODO: Implement tdm-client connection
            self._status = RobotStatus.CONNECTED
            logger.info("Thymio %s connected", self.robot_id)
            return True
        except Exception:
            self._status = RobotStatus.ERROR
            logger.exception("Failed to connect Thymio %s", self.robot_id)
            return False

    def disconnect(self) -> None:
        self._node = None
        self._status = RobotStatus.DISCONNECTED

    def pause(self) -> None:
        for skin in self._skins.values():
            for local_idx in skin.chambers:
                skin.hold(local_idx)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        if self._status != RobotStatus.CONNECTED:
            return False
        skin_id = kwargs.get("skin")
        if skin_id and skin_id in self._skins:
            skin = self._skins[skin_id]
            idx = kwargs.get("slot")
            if command == "set_pressure":
                return skin.set_pressure(idx, kwargs.get("value", 100))
            if command == "inflate":
                return skin.inflate(idx, kwargs.get("delta", 10))
            if command == "deflate":
                return skin.deflate(idx, kwargs.get("delta", 10))
            if command == "hold":
                return skin.hold(idx)
        # TODO: tdm-client movement commands
        logger.debug("Thymio %s command: %s %s", self.robot_id, command, kwargs)
        return True

    def set_motors(self, left: int, right: int) -> bool:
        return self.send_command("motors", left=left, right=right)

    def stop(self) -> bool:
        return self.set_motors(0, 0)

    def set_leds(self, r: int, g: int, b: int) -> bool:
        return self.send_command("leds", r=r, g=g, b=b)

    def get_status_data(self) -> dict[str, Any]:
        return {
            "robot_id":   self.robot_id,
            "status":     self._status.value,
            "skins":      {sid: s.get_status() for sid, s in self._skins.items()},
            "reservoirs": {k: r.get_status() for k, r in self._reservoirs.items()},
            "sensors":    {},
        }

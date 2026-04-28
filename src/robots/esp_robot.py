"""EspRobot — concrete base for any robot that drives ESP32 nodes via the gateway.

Houses everything that's identical across TurtleRobot, TreeRobot, ThymioRobot:
  * Controller dictionary built from ``node_configs``
  * ``configure`` push for ``node_reservoir`` nodes
  * Skin dictionary built from ``skin_configs``
  * Optional pressure / vacuum reservoirs auto-derived from reservoir nodes
  * ``pause()`` (calls ``hold`` on every chamber)
  * ``send_command()`` dispatching to skins (inflate / deflate / set_pressure / hold)
  * ``get_status_data()`` skeleton
  * Default ``connect()`` / ``disconnect()`` (subclasses override if they need more)

Subclasses contribute their own behaviour on top: Turtle has bulk skin helpers,
Tree has owner / sharing logic, Thymio adds tdm-client motors and LEDs.
"""

from __future__ import annotations

import logging
from typing import Any

from src.hardware.air_reservoir import AirReservoir
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin import Skin
from src.robots._robot_builder import (
    build_reservoirs,
    build_skins,
    configure_reservoir_nodes,
)
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class EspRobot(BaseRobot):
    """Robot backed by ESP32 nodes over ESP-NOW.

    Args:
        robot_id:          Unique identifier.
        kind:              Display name ("Turtle", "Tree", ...).
        gateway:           Shared ESP-NOW gateway. Required for hardware mode;
                           may be ``None`` for robots that boot in
                           "no-hardware" mode (e.g. ThymioRobot without nodes).
        node_configs:      List of ``{"mac": ..., "node_type": ...}`` dicts.
        skin_configs:      List of skin dicts (see ``build_skins``).
        reservoir_configs: Optional explicit reservoir block. Auto-derived from
                           any ``node_reservoir`` node when omitted.
    """

    def __init__(
        self,
        robot_id: str,
        kind: str,
        gateway: ESPNowGateway | None,
        node_configs: list[dict[str, Any]] | None,
        skin_configs: list[dict[str, Any]] | None,
        reservoir_configs: dict[str, Any] | None = None,
    ):
        super().__init__(robot_id, kind)
        self._gateway = gateway

        nodes = node_configs or []
        skins = skin_configs or []

        if gateway is not None and nodes:
            self._controllers: dict[str, ESP32Controller] = {
                n["mac"]: ESP32Controller(n["mac"], gateway) for n in nodes
            }
            configure_reservoir_nodes(nodes, self._controllers)
            self._skins: dict[str, Skin] = build_skins(skins, self._controllers)
            self._reservoirs: dict[str, AirReservoir] = build_reservoirs(
                nodes, reservoir_configs, self._controllers,
            )
        else:
            self._controllers = {}
            self._skins = {}
            self._reservoirs = {}

    # ------------------------------------------------------------------
    # Public model accessors
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        if self._gateway is None or not self._gateway.is_connected:
            logger.error("%s %s: gateway not connected", self.name, self.robot_id)
            return False
        self._status = RobotStatus.CONNECTED
        logger.info("%s %s connected: %d skin(s), %d chamber(s)",
                    self.name, self.robot_id, len(self._skins), self.total_chambers)
        return True

    def disconnect(self) -> None:
        self._status = RobotStatus.DISCONNECTED

    def pause(self) -> None:
        for skin in self._skins.values():
            for local_idx in skin.chambers:
                skin.hold(local_idx)

    # ------------------------------------------------------------------
    # Commanding
    # ------------------------------------------------------------------

    def send_command(self, command: str, **kwargs: Any) -> bool:
        skin_id = kwargs.get("skin", "")
        skin = self._skins.get(skin_id)
        if skin is None:
            logger.error("%s %s: invalid skin ID %r", self.name, self.robot_id, skin_id)
            return False
        idx: int | None = kwargs.get("slot")
        if command == "set_pressure":
            return skin.set_pressure(idx, kwargs.get("value", 100))
        if command == "inflate":
            return skin.inflate(idx, kwargs.get("delta", 10))
        if command == "deflate":
            return skin.deflate(idx, kwargs.get("delta", 10))
        if command == "hold":
            if idx is None:
                return False
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

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status_data(self) -> dict[str, Any]:
        return {
            "robot_id":   self.robot_id,
            "status":     self._status.value,
            "skins":      {sid: s.get_status() for sid, s in self._skins.items()},
            "reservoirs": {k: r.get_status() for k, r in self._reservoirs.items()},
        }

"""EspRobot - concrete base for any robot that drives ESP32 nodes via the gateway.

Houses everything that's identical across TurtleRobot, TreeRobot, ThymioRobot:
  * Controller dictionary built from ``node_configs``
  * ``configure`` push for ``node_multiplexed`` nodes
  * Skin dictionary built from ``skin_configs``
  * ``pause()`` (calls ``hold`` on every chamber)
  * ``send_command()`` dispatching to skins (inflate / deflate / set_pressure / hold)
  * ``get_status_data()`` skeleton
  * Default ``connect()`` / ``disconnect()`` (subclasses override if they need more)

Subclasses contribute their own behaviour on top: Tree adds owner / sharing
logic, Thymio adds tdm-client motors and LEDs.
"""

from __future__ import annotations

import logging
from typing import Any

from src.hardware.esp32_controller import ESP32Controller
from src.hardware.gateway import Gateway
from src.hardware.skin import Skin
from src.robots._robot_builder import (
    build_skins,
    configure_multiplexed_nodes,
    set_pump_counts,
)
from src.robots.base_robot import BaseRobot, RobotStatus

logger = logging.getLogger(__name__)


class EspRobot(BaseRobot):
    """Robot backed by ESP32 nodes over ESP-NOW.

    Args:
        robot_id:          Unique identifier.
        kind:              Display name ("Turtle", "Tree", "Thymio", ...).
        gateway:           Shared SoftEdIBO gateway. Required for hardware mode;
                           may be ``None`` for robots that boot in
                           "no-hardware" mode (e.g. ThymioRobot without nodes).
        node_configs:      List of ``{"mac": ..., "node_type": ...}`` dicts.
        skin_configs:      List of skin dicts (see ``build_skins``).
    """

    def __init__(
        self,
        robot_id: str,
        kind: str,
        gateway: Gateway | None,
        node_configs: list[dict[str, Any]] | None,
        skin_configs: list[dict[str, Any]] | None,
    ):
        super().__init__(robot_id, kind)
        self._gateway = gateway

        nodes = node_configs or []
        skins = skin_configs or []

        if gateway is not None and nodes:
            self._controllers: dict[str, ESP32Controller] = {
                n["mac"]: ESP32Controller(n["mac"], gateway) for n in nodes
            }
            set_pump_counts(nodes, self._controllers)
            configure_multiplexed_nodes(nodes, self._controllers)
            # Nodes whose PCB has no pressure sensors populated yet (config
            # ``pressure_sensors: false``): their skins actuate open-loop on
            # the manual per-chamber times.
            sensorless = {n["mac"] for n in nodes
                          if n.get("pressure_sensors") is False}
            self._skins: dict[str, Skin] = build_skins(
                skins, self._controllers, sensorless_macs=sensorless)
        else:
            self._controllers = {}
            self._skins = {}

    # ------------------------------------------------------------------
    # Public model accessors
    # ------------------------------------------------------------------

    @property
    def gateway(self):
        """The SoftEdIBO gateway this robot talks through (or None in sim/no-hardware)."""
        return self._gateway

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    @property
    def node_macs(self) -> list[str]:
        """MAC addresses of this robot's configured ESP-NOW nodes."""
        return list(self._controllers)

    def nodes_seen_since(self, since: float) -> tuple[int, int]:
        """``(alive, total)`` of this robot's nodes heard from after ``since``
        (a ``time.monotonic()`` reference, e.g. taken just before a gateway
        scan). ``(0, 0)`` when the robot has no nodes or no gateway - the
        caller decides what that means (a bare wireless Thymio is still
        usable when its transport is up)."""
        if self._gateway is None or not self._controllers:
            return 0, 0
        alive = sum(
            1 for mac in self._controllers
            if (self._gateway.node_last_seen(mac) or 0) >= since
        )
        return alive, len(self._controllers)

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

    def emergency_stop(self) -> None:
        """Latch every node OFF - all pumps off, all valves closed.

        Sent to each controller directly (not per chamber) so the nodes drop
        all actuation and the firmware holds the safe state even if the link
        drops afterwards. Re-arm with :meth:`rearm`.
        """
        for ctrl in self._controllers.values():
            ctrl.emergency_stop()

    def rearm(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.resume()

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
            "robot_id": self.robot_id,
            "status":   self._status.value,
            "skins":    {sid: s.get_status() for sid, s in self._skins.items()},
        }

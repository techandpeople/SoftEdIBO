"""SimulatedRobot — a robot backed by SimulatedController instead of ESP32Controller.

Used by SimulationActivity to provide a fully-wired robot with mock hardware.
The GUI receives a SimulatedRobot exactly as it would a real robot — no simulation
awareness required outside this class and SimulationActivity.
"""

from __future__ import annotations

from typing import Any

from src.hardware.simulated_controller import SimulatedController
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot, RobotStatus


class SimulatedRobot(BaseRobot):
    """Mock robot — skins backed by SimulatedController instead of real ESP32."""

    def __init__(
        self,
        robot_id: str,
        name: str,
        skin_configs: list[dict[str, Any]],
    ) -> None:
        """Initialize a simulated robot.

        Args:
            robot_id: Unique identifier (mirrors the original robot's id).
            name: Display name.
            skin_configs: List of skin dicts with 'skin_id', 'name', 'mac', 'slots'.
        """
        super().__init__(robot_id, name)
        self._controllers: dict[str, SimulatedController] = {}
        self._skins: dict[str, Skin] = {}
        self._status = RobotStatus.CONNECTED

        for cfg in skin_configs:
            mac = cfg["mac"]
            if mac not in self._controllers:
                self._controllers[mac] = SimulatedController(mac)
            skin = Skin(
                skin_id=cfg["skin_id"],
                controller=self._controllers[mac],
                chamber_slots=cfg["slots"],
                name=cfg.get("name"),
            )
            self._skins[skin.skin_id] = skin

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    def connect(self) -> bool:
        return True

    def pause(self) -> None:
        """Freeze everything: stop timers, align targets to current pressures, set IDLE."""
        for ctrl in self._controllers.values():
            ctrl.stop_all()
        for skin in self._skins.values():
            for chamber in skin.chambers.values():
                chamber.target_pressure = chamber.pressure
            skin.pause()

    def resume(self) -> None:
        """Allow new commands (next user action restarts timers)."""

    def disconnect(self) -> None:
        """Stop all simulated timers."""
        for ctrl in self._controllers.values():
            ctrl.stop_all()
        self._status = RobotStatus.DISCONNECTED

    def send_command(self, command: str, **kwargs: Any) -> bool:
        skin_id = kwargs.get("skin")
        skin = self._skins.get(skin_id)
        if skin is None:
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
        return {
            "robot_id": self.robot_id,
            "status": self._status.value,
            "skins": {sid: s.get_status() for sid, s in self._skins.items()},
        }

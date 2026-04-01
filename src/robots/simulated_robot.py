"""SimulatedRobot — a robot backed by SimulatedController instead of ESP32Controller."""

from __future__ import annotations

from typing import Any

from src.hardware.simulated_controller import SimulatedController
from src.hardware.skin import Skin
from src.robots._robot_builder import build_skins
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
            robot_id:     Mirrors the original robot's id.
            name:         Display name.
            skin_configs: List of skin dicts in the new format::

                [{"skin_id": ..., "name": ...,
                  "chambers": [{"mac": ..., "slot": int,
                                "max_pressure": float}, ...]}, ...]
        """
        super().__init__(robot_id, name)
        self._controllers: dict[str, SimulatedController] = {}
        self._status = RobotStatus.CONNECTED

        # Collect all unique MACs from all skin chambers and build controllers
        for skin_cfg in skin_configs:
            for ch in skin_cfg.get("chambers", []):
                mac = ch["mac"]
                if mac not in self._controllers:
                    self._controllers[mac] = SimulatedController(mac)

        self._skins: dict[str, Skin] = build_skins(skin_configs, self._controllers)

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    def connect(self) -> bool:
        return True

    def pause(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.stop_all()
        for skin in self._skins.values():
            for chamber in skin.chambers.values():
                chamber.target_pressure = chamber.pressure
            skin.pause()

    def resume(self) -> None:
        pass

    def disconnect(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.stop_all()
        self._status = RobotStatus.DISCONNECTED

    def send_command(self, command: str, **kwargs: Any) -> bool:
        skin = self._skins.get(kwargs.get("skin", ""))
        if skin is None:
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
            "robot_id": self.robot_id,
            "status":   self._status.value,
            "skins":    {sid: s.get_status() for sid, s in self._skins.items()},
        }

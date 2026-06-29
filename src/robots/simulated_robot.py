"""SimulatedRobot — a robot backed by SimulatedController instead of ESP32Controller."""

from __future__ import annotations

from typing import Any

from src.hardware.simulated_controller import SimulatedController
from src.hardware.simulated_magnet_sensor import SimulatedMagnetSensor
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
        *,
        sim_params: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a simulated robot.

        Args:
            robot_id:     Mirrors the original robot's id.
            name:         Display name.
            skin_configs: List of skin dicts in the standard format.
            sim_params:   Optional dict of simulation knobs (sim_inflate_speed,
                          sim_deflate_speed, sim_touch_release_delay_ms, …).
                          Forwarded to each SimulatedController so the operator-
                          tunable inflate/deflate rates take effect.
        """
        super().__init__(robot_id, name)
        self._controllers: dict[str, SimulatedController] = {}
        self._status = RobotStatus.CONNECTED
        self._sim_params = sim_params or {}

        for skin_cfg in skin_configs:
            for ch in skin_cfg.get("chambers", []):
                mac = ch["mac"]
                if mac not in self._controllers:
                    self._controllers[mac] = SimulatedController(
                        mac, sim_params=self._sim_params,
                    )

        # One SimulatedMagnetSensor **per skin** — the simulated "sensor board" the
        # T-buttons feed. Keyed by skin_id (not node_mac) so each skin's
        # T-buttons drive only that skin, even if several skins share a touch
        # node_mac. Keeps touch input separate from chamber actuation,
        # mirroring the real node_magnet_sensor vs chamber-node split.
        self._magnet_sensors: dict[str, SimulatedMagnetSensor] = {}
        for skin_cfg in skin_configs:
            touch = skin_cfg.get("touch") or {}
            if touch:
                skin_id = skin_cfg.get("skin_id", "")
                mac = touch.get("node_mac", skin_id)
                self._magnet_sensors[skin_id] = SimulatedMagnetSensor(mac)

        self._skins: dict[str, Skin] = build_skins(
            skin_configs, self._controllers, touch_controllers=self._magnet_sensors,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def skins(self) -> dict[str, Skin]:
        return self._skins

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        # No-op: SimulatedController state is restored by Skin/AirChamber writes
        # already issued before pause() — there is nothing extra to revive here.
        return

    def emergency_stop(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.emergency_stop()
        for skin in self._skins.values():
            for chamber in skin.chambers.values():
                chamber.target_pressure = chamber.pressure
            skin.pause()

    def rearm(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.resume()

    def disconnect(self) -> None:
        for ctrl in self._controllers.values():
            ctrl.stop_all()
        self._status = RobotStatus.DISCONNECTED

    def send_command(self, command: str, **kwargs: Any) -> bool:
        skin = self._skins.get(kwargs.get("skin", ""))
        if skin is None:
            return False
        idx: int = kwargs.get("slot", 0)
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

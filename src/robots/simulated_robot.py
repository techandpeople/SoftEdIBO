"""SimulatedRobot — a robot backed by SimulatedController instead of ESP32Controller."""

from __future__ import annotations

from typing import Any

# Tank simulation constants (adjust to taste).
TANK_CHAMBER_RATIO: float = 6.0  # 1 unit of chamber pressure uses 1/12 of tank
TANK_REFILL_RATE: int = 1         # % per 300 ms tick (0→100 in 30 s)

from src.hardware.air_reservoir import AirReservoir
from src.hardware.simulated_controller import SimulatedController
from src.hardware.simulated_magnet_sensor import SimulatedMagnetSensor
from src.hardware.skin import Skin
from src.robots._robot_builder import build_skins
from src.robots.base_robot import BaseRobot, RobotStatus


class SimulatedRobot(BaseRobot):
    """Mock robot — skins backed by SimulatedController instead of real ESP32.

    Optionally exposes simulated pressure/vacuum reservoirs so the monitor can
    show tank widgets in simulation mode. Each simulated tank just holds a
    static percentage — useful as a visual placeholder, not a true simulation.
    """

    def __init__(
        self,
        robot_id: str,
        name: str,
        skin_configs: list[dict[str, Any]],
        *,
        tank_kinds: list[str] | None = None,
        sim_params: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a simulated robot.

        Args:
            robot_id:     Mirrors the original robot's id.
            name:         Display name.
            skin_configs: List of skin dicts in the standard format.
            tank_kinds:   Optional kinds of reservoir tanks to expose (any of
                          ``"pressure"``, ``"vacuum"``). Used by the monitor in
                          simulation mode to show tank widgets when the original
                          robot had reservoirs.
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
        self._reservoirs: dict[str, AirReservoir] = self._build_simulated_reservoirs(
            tank_kinds or []
        )
        # skin_id → local_idx → last seen chamber.pressure (blue bar)
        self._prev_pressures: dict[str, dict[int, int]] = {
            sid: dict.fromkeys(skin.chambers, 0)
            for sid, skin in self._skins.items()
        }

    # ------------------------------------------------------------------
    # Properties
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

    def tick(self) -> None:
        """Update simulated tank pressures based on chamber pressure deltas."""
        pressure_tank = self._reservoirs.get("pressure")
        vacuum_tank   = self._reservoirs.get("vacuum")

        p_level = pressure_tank._pressure if pressure_tank else 0  # noqa: SLF001
        v_level = vacuum_tank._pressure   if vacuum_tank   else 0  # noqa: SLF001

        for sid, skin in self._skins.items():
            prev = self._prev_pressures.setdefault(sid, dict.fromkeys(skin.chambers, 0))
            for idx, chamber in skin.chambers.items():
                current = chamber.pressure
                delta   = current - prev.get(idx, 0)
                if delta > 0:
                    p_level -= round(delta / TANK_CHAMBER_RATIO)
                elif delta < 0:
                    v_level -= round(abs(delta) / TANK_CHAMBER_RATIO)
                prev[idx] = current

        if pressure_tank is not None:
            pressure_tank._pressure = max(0, min(100, p_level + TANK_REFILL_RATE))  # noqa: SLF001
        if vacuum_tank is not None:
            vacuum_tank._pressure = max(0, min(100, v_level + TANK_REFILL_RATE))  # noqa: SLF001

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
            "robot_id":   self.robot_id,
            "status":     self._status.value,
            "skins":      {sid: s.get_status() for sid, s in self._skins.items()},
            "reservoirs": {k: r.get_status() for k, r in self._reservoirs.items()},
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_simulated_reservoirs(self, tank_kinds: list[str]) -> dict[str, AirReservoir]:
        reservoirs: dict[str, AirReservoir] = {}
        for kind in tank_kinds:
            if kind not in ("pressure", "vacuum"):
                continue
            sim_mac = f"SIM:TANK:{self.robot_id}:{kind}"
            ctrl = SimulatedController(sim_mac, sim_params=self._sim_params)
            self._controllers[sim_mac] = ctrl
            res = AirReservoir(kind=kind, controller=ctrl)  # type: ignore[arg-type]
            res._pressure = 100  # noqa: SLF001  — full tank on sim start
            reservoirs[kind] = res
        return reservoirs

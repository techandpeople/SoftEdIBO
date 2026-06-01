"""OrganSwap activity — heal a soft robot by plugging in the right organs.

State machine (per robot):

    ┌──────┐  organs_match?  ┌───────┐
    │ SICK │ ──────────────► │ CURED │
    └──────┘                 └───────┘
       │ on_enter:                │ on_enter:
       │  - LED pulsing red       │  - LED solid green
       │  - touch → inflate       │  - breathing animation
       │ periodic:                │ periodic:
       │  - small idle "pant"     │  - slow inflate/deflate cycle
       └──────────────────────────┘

Hardware support is wired through getattr so the activity runs even before
``set_led`` / ``on_organ`` land on ``ESP32Controller`` (Phase 1d). In
simulation mode, the LED commands are silently swallowed and the operator
drives organ swaps via the dialog's "Organ catalogue" tool (planned —
today, transitions are exposed via :meth:`force_state` so the GUI can fire
them manually for testing).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer

from src.activities.base_activity import BaseActivity, Param
from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session

logger = logging.getLogger(__name__)


STATE_SICK  = "sick"
STATE_CURED = "cured"


class OrganSwapActivity(BaseActivity):
    """Cure-the-robot activity driven by organ resistance + touch events."""

    robot_type = BaseRobot   # works on any robot (Turtle / Tree / Thymio / Simulated)

    PARAMS = (
        # --- Cure condition ---
        Param(
            name="organ_readout_mode",
            type="enum", default="aggregate",
            choices=("aggregate", "per_organ"),
            label="Organ readout mode",
            description="aggregate: trust the total resistance vs a target. "
                        "per_organ: decompose the total into known organs via "
                        "1/Rtot = Σ 1/Ri (PC-side, against the catalogue).",
        ),
        Param(
            name="cured_total_resistance_ohm",
            type="float", default=952.4, min=0.0, max=1_000_000.0,
            label="Cured total resistance (Ω)",
            description="Aggregate mode: total resistance read by the firmware "
                        "when all required organs are correctly plugged in "
                        "(e.g. liver_good=1500 ∥ heart_good=2200 ∥ "
                        "lung_good=3300 ≈ 952.4 Ω).",
        ),
        Param(
            name="cured_tolerance_ohm",
            type="float", default=80.0, min=0.0, max=10_000.0,
            label="Cured tolerance (±Ω)",
            description="How far the measured total can drift from the cured "
                        "value before the robot reverts to 'sick'.",
        ),
        Param(
            name="organ_catalogue",
            type="json",
            default={"liver_good": 1500, "heart_good": 2200, "lung_good": 3300,
                     "liver_bad":  4700, "heart_bad":  5600, "lung_bad":  6800},
            label="Organ catalogue",
            description="per_organ mode: {organ_id: resistance_ohm} of every "
                        "organ the operator might plug in. The PC enumerates "
                        "subsets and picks the combination whose parallel "
                        "resistance matches the measured total.",
        ),
        # --- Sick-state look & feel ---
        Param(
            name="sick_color", type="color", default="#e74c3c",
            label="Sick LED colour",
        ),
        Param(
            name="sick_pulse_ms", type="int", default=1000, min=100, max=5000,
            label="Sick LED pulse period (ms)",
        ),
        Param(
            name="sick_touch_inflate_pct", type="int", default=30, min=0, max=100,
            label="Touch reaction (%)",
            description="On a touch, the mapped chamber inflates by this "
                        "percentage of its max range.",
        ),
        Param(
            name="sick_touch_hold_ms", type="int", default=300, min=0, max=5000,
            label="Touch hold duration (ms)",
            description="How long the chamber stays inflated after a touch "
                        "before deflating back to zero. Re-touching restarts "
                        "the countdown. Applies in both simulation and on "
                        "real hardware.",
        ),
        Param(
            name="sim_deflate_speed_pct_per_s", type="int", default=33,
            min=5, max=100,
            label="Sim deflate speed (%/s)",
            description="How fast chambers deflate in simulation mode after "
                        "the touch hold duration expires. On real hardware the "
                        "firmware controls the deflate rate.",
        ),
        Param(
            name="sensor_to_chamber",
            type="sensor_map", default="auto",
            label="Sensor → Chamber routing",
            description="Maps an IMU sensor index to the chamber that should "
                        "react to its touch in this activity. 'auto' means 1:1 "
                        "mapping (sensor 0→chamber 0, sensor 1→chamber 1, etc.). "
                        "Click '+ Add mapping' to customize — so different "
                        "activities can reuse the same skin with different routings.",
        ),
        # --- Cured-state look & feel ---
        Param(
            name="cured_color", type="color", default="#2ecc71",
            label="Cured LED colour",
        ),
        Param(
            name="breathe_period_ms", type="int", default=4000,
            min=500, max=20_000,
            label="Breathe period (ms)",
            description="One full inflate-and-deflate cycle in the cured "
                        "breathing animation.",
        ),
        Param(
            name="breathe_depth_pct", type="int", default=60, min=10, max=100,
            label="Breathe depth (%)",
            description="Peak inflate target reached at the top of each breath.",
        ),
    )

    def __init__(self) -> None:
        super().__init__(
            name="Organ Swap",
            description="Heal the robot by swapping bad organs for good ones.",
        )
        self._robots: list[BaseRobot] = []
        # Per-robot state machine (always per-robot for this activity — each
        # child works on their own robot independently).
        self._state: dict[str, str] = {}
        self._cured_at: dict[str, float] = {}
        self._last_resistance: dict[str, float] = {}
        # Periodic driver for breathing + idle "pant". Started in ``start``,
        # paused/stopped via the BaseActivity lifecycle.
        self._tick_owner: QObject | None = None
        self._tick: QTimer | None = None
        # Per-chamber auto-deflate timers: a touch inflates the chamber, then
        # after ``sick_touch_hold_ms`` it deflates back to zero. Keyed by
        # ``(skin_id, chamber_idx)``; re-touching the same chamber restarts the
        # countdown so a held/repeated touch keeps it inflated.
        self._deflate_timers: dict[tuple[str, int], QTimer] = {}
        # Currently-active (held) sensors per skin, for press/release edge
        # detection against the IMU's ``act`` set.
        self._active_touch: dict[str, set[int]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        self._robots = robots
        for robot in robots:
            self._state[robot.robot_id] = STATE_SICK
            self._last_resistance[robot.robot_id] = float("inf")
            self._subscribe_robot(robot)
        logger.info("OrganSwap set up with %d robots", len(robots))

    def start(self) -> None:
        # Kick every robot into its initial state (sick) so the LED and any
        # other on_enter actions fire immediately.
        for robot in self._robots:
            self._enter_state(robot, STATE_SICK)
        # Drive periodic animations on a 60 ms tick — enough for visibly
        # smooth breathing without burning CPU.
        self._tick_owner = QObject()
        self._tick = QTimer(self._tick_owner)
        self._tick.setInterval(60)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        logger.info("OrganSwap started")

    def pause(self) -> None:
        if self._tick is not None:
            self._tick.stop()

    def resume(self) -> None:
        if self._tick is not None:
            self._tick.start()

    def stop(self) -> None:
        if self._tick is not None:
            self._tick.stop()
            self._tick = None
        for timer in self._deflate_timers.values():
            timer.stop()
        self._deflate_timers.clear()
        self._active_touch.clear()
        self._tick_owner = None
        for robot in self._robots:
            for ctrl in self._controllers_of(robot):
                set_led = getattr(ctrl, "set_led", None)
                if set_led is not None:
                    set_led("#000000", pattern="off", period_ms=0)
        self._robots = []
        self._state.clear()
        self._cured_at.clear()
        self._last_resistance.clear()
        logger.info("OrganSwap stopped")

    def get_state(self) -> dict[str, Any]:
        return {
            "name":   self.name,
            "states": dict(self._state),
        }

    # ------------------------------------------------------------------
    # Operator hooks (useful for the preset editor / debug panel)
    # ------------------------------------------------------------------

    def force_state(self, robot_id: str, state: str) -> None:
        """Set a robot's state from outside (debug button or scripted demo)."""
        if state not in (STATE_SICK, STATE_CURED):
            return
        robot = next((r for r in self._robots if r.robot_id == robot_id), None)
        if robot is None:
            return
        self._enter_state(robot, state)

    def robot_state(self, robot_id: str) -> str:
        return self._state.get(robot_id, STATE_SICK)

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _enter_state(self, robot: BaseRobot, state: str) -> None:
        prev = self._state.get(robot.robot_id)
        self._state[robot.robot_id] = state
        if state == STATE_CURED:
            self._cured_at[robot.robot_id] = time.monotonic()
            self._set_robot_led(
                robot,
                color=self.param_values["cured_color"],
                pattern="solid",
                period_ms=0,
            )
        else:  # sick
            self._cured_at.pop(robot.robot_id, None)
            self._set_robot_led(
                robot,
                color=self.param_values["sick_color"],
                pattern="pulse",
                period_ms=int(self.param_values["sick_pulse_ms"]),
            )
        if prev != state:
            logger.info("OrganSwap %s → %s", robot.robot_id, state)

    def _on_tick(self) -> None:
        """Periodic driver — runs every 60 ms. Per-robot:
        - SICK: nothing (touches drive inflation reactively).
        - CURED: update each chamber's target to follow the breathing sine."""
        period = max(100, int(self.param_values["breathe_period_ms"]))
        depth  = max(0, min(100, int(self.param_values["breathe_depth_pct"])))
        for robot in self._robots:
            if self._state.get(robot.robot_id) != STATE_CURED:
                continue
            self._breathe(robot, period, depth)

    def _breathe(self, robot: BaseRobot, period_ms: int, depth_pct: int) -> None:
        import math
        elapsed = (time.monotonic() - self._cured_at.get(robot.robot_id, 0)) * 1000
        phase = (elapsed % period_ms) / period_ms          # 0..1
        # Sine raised so it's always ≥0; full breath = depth_pct.
        target = int(depth_pct * 0.5 * (1 - math.cos(2 * math.pi * phase)))
        skins = getattr(robot, "skins", {})
        for skin in skins.values():
            for chamber_id in skin.chambers:
                skin.set_pressure(chamber_id, target)

    # ------------------------------------------------------------------
    # Event handlers — wired via _subscribe_robot
    # ------------------------------------------------------------------

    def _subscribe_robot(self, robot: BaseRobot) -> None:
        for ctrl in self._controllers_of(robot):
            on_organ = getattr(ctrl, "on_organ", None)
            if on_organ is not None:
                on_organ(lambda r, rb=robot: self._on_organ(rb, r))
        # Touch is subscribed **per skin**, bound to that skin's own touch
        # controller, so a touch on one skin only drives that skin's chambers
        # (no cross-skin leakage). Each skin has its own IMU (real node_imu or
        # a per-skin SimulatedIMU), so the binding is unambiguous.
        for skin in getattr(robot, "skins", {}).values():
            tc = getattr(skin, "touch_controller", None)
            on_imu = getattr(tc, "on_imu", None) if tc is not None else None
            if on_imu is not None:
                on_imu(lambda data, rb=robot, sk=skin: self._on_imu(rb, sk, data))

    def _on_organ(self, robot: BaseRobot, resistance_ohm: float) -> None:
        """Firmware reports the combined resistance for this robot. Re-evaluate
        the cure condition and transition if needed."""
        self._last_resistance[robot.robot_id] = float(resistance_ohm)
        if self._is_cured(resistance_ohm):
            if self._state.get(robot.robot_id) != STATE_CURED:
                self._enter_state(robot, STATE_CURED)
        else:
            if self._state.get(robot.robot_id) != STATE_SICK:
                self._enter_state(robot, STATE_SICK)

    def _on_imu(self, robot: BaseRobot, skin, data: dict[str, Any]) -> None:
        """Route this **skin's** IMU sensor activations to its chamber actions.
        The IMU streams the set of *currently active* sensors in ``act``; we
        edge-detect against the previous set so a sensor entering the set
        inflates its mapped chamber and a sensor leaving it (the release) starts
        the deflate countdown. Only active in SICK state; the cured robot
        ignores touches and just breathes."""
        if self._state.get(robot.robot_id) != STATE_SICK:
            return
        active = data.get("act") or []
        if not isinstance(active, list):
            return
        delta = int(self.param_values["sick_touch_inflate_pct"])
        new_set = {int(s) for s in active if isinstance(s, (int, str))
                   and str(s).lstrip("-").isdigit()}
        mapping = self._mapping_for(skin)
        prev_set = self._active_touch.get(skin.skin_id, set())
        for sensor_idx in new_set - prev_set:      # newly pressed
            self._on_sensor_press(skin, mapping, sensor_idx, delta)
        for sensor_idx in prev_set - new_set:      # released
            self._on_sensor_release(skin, mapping, sensor_idx)
        self._active_touch[skin.skin_id] = new_set

    def _mapping_for(self, skin) -> dict:
        """Activity preset takes precedence; 'auto' generates 1:1 fallback."""
        from_preset = self.param_values.get("sensor_to_chamber")
        if from_preset and from_preset != "auto":
            return from_preset if isinstance(from_preset, dict) else {}

        # Auto-generate 1:1 mapping based on skin's chamber count
        chamber_count = len(skin.chambers)
        touch = skin.touch or {}
        sensor_count = touch.get("sensor_count", chamber_count)
        return {str(i): i for i in range(min(chamber_count, sensor_count))}

    def _on_sensor_press(self, skin, mapping: dict, sensor_idx: Any,
                         delta_pct: int) -> None:
        """A sensor became active — inflate its chamber and cancel any pending
        deflate so the chamber stays up while the touch is held."""
        ch = self._chamber_for(mapping, sensor_idx)
        if ch is None:
            return
        timer = self._deflate_timers.get((skin.skin_id, ch))
        if timer is not None:
            timer.stop()
        try:
            skin.inflate(ch, delta_pct)
        except (TypeError, ValueError):
            return

    def _on_sensor_release(self, skin, mapping: dict, sensor_idx: Any) -> None:
        """A sensor was released — start the deflate countdown. After
        ``sick_touch_hold_ms`` from the *release* the chamber deflates to zero."""
        ch = self._chamber_for(mapping, sensor_idx)
        if ch is None:
            return
        hold_ms = int(self.param_values["sick_touch_hold_ms"])
        key = (skin.skin_id, ch)
        timer = self._deflate_timers.get(key)
        if timer is None:
            timer = QTimer(self._tick_owner)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda s=skin, c=ch: self._deflate_now(s, c))
            self._deflate_timers[key] = timer
        timer.start(hold_ms)

    @staticmethod
    def _chamber_for(mapping: dict, sensor_idx: Any) -> int | None:
        ch = mapping.get(str(sensor_idx), mapping.get(sensor_idx))
        if ch is None:
            return None
        try:
            return int(ch)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deflate_now(skin, chamber_idx: int) -> None:
        try:
            skin.set_pressure(chamber_idx, 0)
        except (TypeError, ValueError):
            return

    # ------------------------------------------------------------------
    # Cure logic
    # ------------------------------------------------------------------

    def _is_cured(self, resistance_ohm: float) -> bool:
        mode = self.param_values["organ_readout_mode"]
        if mode == "per_organ":
            return self._matches_per_organ(resistance_ohm)
        return self._matches_aggregate(resistance_ohm)

    def _matches_aggregate(self, resistance_ohm: float) -> bool:
        target = float(self.param_values["cured_total_resistance_ohm"])
        tol    = float(self.param_values["cured_tolerance_ohm"])
        return abs(resistance_ohm - target) <= tol

    def _matches_per_organ(self, resistance_ohm: float) -> bool:
        """Decompose the measured total against the catalogue (1/Rtot =
        Σ 1/Ri) and return True only when the best-matching subset is
        exactly the set of "good" organs.

        Convention: organ IDs ending in ``_good`` are required; anything
        else (e.g. ``_bad``) is forbidden. Empty catalogue or no good
        organs fall back to the aggregate check so the activity stays
        usable while the operator is still authoring the preset.
        """
        from itertools import combinations
        catalogue: dict[str, float] = self.param_values.get("organ_catalogue") or {}
        if not catalogue:
            return self._matches_aggregate(resistance_ohm)
        required = {k for k in catalogue if k.endswith("_good")}
        if not required:
            return self._matches_aggregate(resistance_ohm)
        tolerance = float(self.param_values["cured_tolerance_ohm"])
        best_subset: set[str] | None = None
        best_diff = float("inf")
        keys = list(catalogue.keys())
        for size in range(1, len(keys) + 1):
            for combo in combinations(keys, size):
                r_total = self._parallel_resistance(
                    [catalogue[k] for k in combo]
                )
                diff = abs(r_total - resistance_ohm)
                if diff < best_diff:
                    best_diff = diff
                    best_subset = set(combo)
        if best_subset is None or best_diff > tolerance:
            return False
        return best_subset == required

    @staticmethod
    def _parallel_resistance(values: list[float]) -> float:
        """1 / Rtot = Σ 1 / Ri (parallel circuit). Ignores non-positive
        values; returns +inf for an empty (or all-zero) input."""
        inv_sum = sum(1.0 / v for v in values if v > 0)
        return 1.0 / inv_sum if inv_sum > 0 else float("inf")

    # ------------------------------------------------------------------
    # Controller helpers
    # ------------------------------------------------------------------

    def _controllers_of(self, robot: BaseRobot):
        """Yield each unique controller of the robot's skins. Robots expose
        their controllers privately, so we walk the skins instead — that
        works for both real and simulated robots without poking internals."""
        seen: set[int] = set()
        for skin in getattr(robot, "skins", {}).values():
            ctrl = getattr(skin, "_ctrl", None)
            if ctrl is not None and id(ctrl) not in seen:
                seen.add(id(ctrl))
                yield ctrl
            touch_ctrl = getattr(skin, "touch_controller", None)
            if touch_ctrl is not None and id(touch_ctrl) not in seen:
                seen.add(id(touch_ctrl))
                yield touch_ctrl

    def _set_robot_led(self, robot: BaseRobot, color: str,
                       pattern: str, period_ms: int) -> None:
        for ctrl in self._controllers_of(robot):
            set_led = getattr(ctrl, "set_led", None)
            if set_led is None:
                continue
            try:
                set_led(color, pattern=pattern, period_ms=period_ms)
            except Exception:   # noqa: BLE001 — controller errors are non-fatal
                logger.exception("set_led failed on %s", robot.robot_id)

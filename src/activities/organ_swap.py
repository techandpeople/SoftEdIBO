"""OrganSwap activity — heal a soft robot by plugging in the right organs.

State machine (per **patient**). A patient is the curable unit:

- a whole robot sharing one organ circuit (Turtle: the group cures one
  patient together; Thymio: one robot per child), or
- a single skin with its own organ circuit — declared via the skin's
  ``organ: {slot, node_mac?}`` config block (Tree: one patient per branch).

The silicone cover closes the organ sensing circuit, so the firmware
reports an open circuit while the cover is off:

    ┌──────┐  cover off   ┌──────┐  cover on +    ┌───────┐
    │ SICK │ ───────────► │ OPEN │  organs match  │ CURED │
    └──────┘ ◄─────────── └──────┘ ─────────────► └───────┘
       │ cover on, wrong organs        ▲    cover off │
       │                               └──────────────┘
       │ SICK:  LED pulsing red, touch → inflate
       │ OPEN:  LED pulsing blue ("surgery"), chambers deflated
       │ CURED: LED solid green, breathing animation

Responsibilities are split across collaborators:

- :class:`~src.hardware.organ_sensor.OrganSensor` — turns the raw controller
  readings into cover / resistance events.
- :class:`~src.activities.organ_matching.OrganMatcher` — decides whether a
  resistance means "cured" (aggregate or per-organ catalogue decomposition).
- This class — per-patient state machine, LED / chamber reactions, and
  behavioral event logging via ``BaseActivity.log_event``.

Hardware support is wired through getattr so the activity runs against any
controller that exposes the relevant slice (``set_led`` / ``on_organ`` /
``on_magnet``) — real or simulated. Transitions can also be forced via
:meth:`force_state` for GUI testing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer

from src.activities.base_activity import BaseActivity, Param
from src.activities.organ_matching import OrganMatcher
from src.hardware.organ_sensor import OrganSensor
from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session

logger = logging.getLogger(__name__)


STATE_SICK  = "sick"
STATE_OPEN  = "open"     # cover off — "surgery" in progress
STATE_CURED = "cured"


@dataclass
class _Patient:
    """One curable unit: a whole robot sharing a single organ circuit, or a
    single skin with its own circuit (``skin.organ``). Holds the skins it
    animates and the OrganSensor(s) watching its circuit."""
    patient_id: str
    robot: BaseRobot
    skins: list[Any]
    sensors: list[OrganSensor] = field(default_factory=list)


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
            name="open_color", type="color", default="#3498db",
            label="Open (cover off) LED colour",
            description="Shown while the silicone cover is off and the "
                        "organ circuit is open — the 'surgery' phase.",
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
            description="Maps an magnet sensor sensor index to the chamber that should "
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
        # Curable units, keyed by patient_id (robot_id for whole-robot
        # patients, "robot_id/skin_id" for per-skin patients like Tree
        # branches). The state machine runs per patient.
        self._patients: dict[str, _Patient] = {}
        self._skin_patient: dict[str, str] = {}   # skin_id → patient_id
        self._state: dict[str, str] = {}
        self._cured_at: dict[str, float] = {}
        self._last_resistance: dict[str, float] = {}
        # Cure decision logic, built from the preset params in _setup.
        self._matcher: OrganMatcher | None = None
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
        # detection against the magnet sensor's ``act`` set.
        self._active_touch: dict[str, set[int]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        self._robots = robots
        self._matcher = OrganMatcher.from_params(self.param_values)
        for robot in robots:
            for patient in self._build_patients(robot):
                self._patients[patient.patient_id] = patient
                self._state[patient.patient_id] = STATE_SICK
                self._last_resistance[patient.patient_id] = float("inf")
                for skin in patient.skins:
                    self._skin_patient[skin.skin_id] = patient.patient_id
            self._subscribe_touch(robot)
        logger.info("OrganSwap set up with %d robots / %d patients",
                    len(robots), len(self._patients))

    def start(self) -> None:
        # Kick every patient into its initial state (sick) so the LED and any
        # other on_enter actions fire immediately.
        for patient in self._patients.values():
            self._enter_state(patient, STATE_SICK)
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
            for ctrl in self._controllers_of_skins(
                    getattr(robot, "skins", {}).values()):
                set_led = getattr(ctrl, "set_led", None)
                if set_led is not None:
                    set_led("#000000", pattern="off", period_ms=0)
        self._robots = []
        self._patients.clear()
        self._skin_patient.clear()
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

    def force_state(self, patient_id: str, state: str) -> None:
        """Set a patient's state from outside (debug button or scripted demo).
        ``patient_id`` is the robot_id for whole-robot patients."""
        if state not in (STATE_SICK, STATE_OPEN, STATE_CURED):
            return
        patient = self._patients.get(patient_id)
        if patient is None:
            return
        self._enter_state(patient, state)

    def robot_state(self, patient_id: str) -> str:
        """Current state of a patient (robot_id for whole-robot patients)."""
        return self._state.get(patient_id, STATE_SICK)

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _enter_state(self, patient: _Patient, state: str) -> None:
        prev = self._state.get(patient.patient_id)
        self._state[patient.patient_id] = state
        if state == STATE_CURED:
            self._cured_at[patient.patient_id] = time.monotonic()
            self._set_patient_led(
                patient,
                color=self.param_values["cured_color"],
                pattern="solid",
                period_ms=0,
            )
        elif state == STATE_OPEN:
            # Cover off — "surgery" phase: blue pulse, patient deflated so
            # the chambers don't fight the child working on the organs.
            self._cured_at.pop(patient.patient_id, None)
            self._set_patient_led(
                patient,
                color=self.param_values["open_color"],
                pattern="pulse",
                period_ms=int(self.param_values["sick_pulse_ms"]),
            )
            for skin in patient.skins:
                for chamber_id in skin.chambers:
                    skin.set_pressure(chamber_id, 0)
        else:  # sick
            self._cured_at.pop(patient.patient_id, None)
            self._set_patient_led(
                patient,
                color=self.param_values["sick_color"],
                pattern="pulse",
                period_ms=int(self.param_values["sick_pulse_ms"]),
            )
        if prev != state:
            logger.info("OrganSwap %s → %s", patient.patient_id, state)
            self.log_event(
                "activity", "state", target=patient.patient_id,
                metadata=json.dumps({"from": prev, "to": state}),
            )

    def _on_tick(self) -> None:
        """Periodic driver — runs every 60 ms. Per-patient:
        - SICK: nothing (touches drive inflation reactively).
        - CURED: update each chamber's target to follow the breathing sine."""
        period = max(100, int(self.param_values["breathe_period_ms"]))
        depth  = max(0, min(100, int(self.param_values["breathe_depth_pct"])))
        for patient in self._patients.values():
            if self._state.get(patient.patient_id) != STATE_CURED:
                continue
            self._breathe(patient, period, depth)

    def _breathe(self, patient: _Patient, period_ms: int, depth_pct: int) -> None:
        import math
        elapsed = (time.monotonic()
                   - self._cured_at.get(patient.patient_id, 0)) * 1000
        phase = (elapsed % period_ms) / period_ms          # 0..1
        # Sine raised so it's always ≥0; full breath = depth_pct.
        target = int(depth_pct * 0.5 * (1 - math.cos(2 * math.pi * phase)))
        for skin in patient.skins:
            for chamber_id in skin.chambers:
                skin.set_pressure(chamber_id, target)

    # ------------------------------------------------------------------
    # Patient construction
    # ------------------------------------------------------------------

    def _build_patients(self, robot: BaseRobot) -> list[_Patient]:
        """Split a robot into curable patients.

        A skin carrying its own ``organ: {slot, node_mac?}`` block becomes its
        own patient (Tree branch); every skin without one is folded into a
        single whole-robot patient sharing the robot's organ circuit
        (Turtle / Thymio). Each patient gets the OrganSensor(s) bound to its
        circuit so its cover/resistance events are independent."""
        skins = list(getattr(robot, "skins", {}).values())
        per_skin = [s for s in skins if getattr(s, "organ", None)]
        shared = [s for s in skins if not getattr(s, "organ", None)]

        patients: list[_Patient] = []

        # Per-skin patients (own circuit on a named slot).
        for skin in per_skin:
            organ_cfg = skin.organ or {}
            slot = int(organ_cfg.get("slot", 0))
            mac = organ_cfg.get("node_mac")
            ctrl = self._organ_controller(skin, mac)
            patient = _Patient(
                patient_id=f"{robot.robot_id}/{skin.skin_id}",
                robot=robot, skins=[skin])
            self._bind_sensor(patient, ctrl, slot)
            patients.append(patient)

        # One whole-robot patient for the rest (shared circuit, slot 0).
        if shared:
            patient = _Patient(patient_id=robot.robot_id,
                               robot=robot, skins=shared)
            for ctrl in self._controllers_of_skins(shared):
                self._bind_sensor(patient, ctrl, 0)
            patients.append(patient)

        return patients

    @staticmethod
    def _organ_controller(skin: Any, node_mac: str | None) -> Any:
        """Resolve the controller carrying a per-skin organ circuit. Defaults
        to the skin's own chamber controller; ``node_mac`` (rarely needed)
        points at the touch controller when the circuit lives elsewhere."""
        ctrl = getattr(skin, "_ctrl", None)
        if node_mac and getattr(ctrl, "mac_address", None) != node_mac:
            touch = getattr(skin, "touch_controller", None)
            if getattr(touch, "mac_address", None) == node_mac:
                return touch
        return ctrl

    def _bind_sensor(self, patient: _Patient, ctrl: Any, slot: int) -> None:
        """Attach an OrganSensor on ``ctrl``/``slot`` to this patient."""
        if ctrl is None or getattr(ctrl, "on_organ", None) is None:
            return
        sensor = OrganSensor(ctrl, slot=slot)
        sensor.on_cover(
            lambda closed, p=patient, s=sensor: self._on_cover(p, s, closed))
        sensor.on_resistance(
            lambda ohm, p=patient: self._on_resistance(p, ohm))
        patient.sensors.append(sensor)

    def _subscribe_touch(self, robot: BaseRobot) -> None:
        """Subscribe to each skin's magnet board for touch reactions. Bound
        per skin so a touch on one skin only drives that skin's chambers."""
        for skin in getattr(robot, "skins", {}).values():
            tc = getattr(skin, "touch_controller", None)
            on_magnet = getattr(tc, "on_magnet", None) if tc is not None else None
            if on_magnet is not None:
                on_magnet(lambda data, sk=skin: self._on_magnet(sk, data))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_cover(self, patient: _Patient, sensor: OrganSensor,
                  closed: bool) -> None:
        """The silicone cover opened or closed this patient's organ circuit."""
        self.log_event("cover", "close" if closed else "open",
                       target=patient.patient_id)
        if not closed:
            self._last_resistance[patient.patient_id] = float("inf")
            if self._state.get(patient.patient_id) != STATE_OPEN:
                self._enter_state(patient, STATE_OPEN)
        else:
            self._evaluate(patient, sensor.resistance_ohm)

    def _on_resistance(self, patient: _Patient, resistance_ohm: float) -> None:
        """The organ network's resistance changed (cover is on)."""
        self.log_event("organ", "reading", target=patient.patient_id,
                       metadata=f"{resistance_ohm:.1f}")
        self._evaluate(patient, resistance_ohm)

    def _evaluate(self, patient: _Patient, resistance_ohm: float) -> None:
        """Re-run the cure decision and transition if needed."""
        self._last_resistance[patient.patient_id] = float(resistance_ohm)
        cured = self._matcher is not None and self._matcher.is_cured(resistance_ohm)
        target = STATE_CURED if cured else STATE_SICK
        if self._state.get(patient.patient_id) != target:
            self._enter_state(patient, target)

    def _on_magnet(self, skin, data: dict[str, Any]) -> None:
        """Route this **skin's** magnet sensor activations to its chamber actions.
        The magnet sensor streams the set of *currently active* sensors in ``act``; we
        edge-detect against the previous set so a sensor entering the set
        inflates its mapped chamber and a sensor leaving it (the release) starts
        the deflate countdown. Only active while this skin's patient is SICK;
        a cured/open patient ignores touches and just breathes (or rests)."""
        patient_id = self._skin_patient.get(skin.skin_id)
        if patient_id is None or self._state.get(patient_id) != STATE_SICK:
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
    # Controller helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _controllers_of_skins(skins):
        """Yield each unique controller (chamber + touch) of the given skins.
        Robots expose their controllers privately, so we walk the skins — that
        works for both real and simulated robots without poking internals."""
        seen: set[int] = set()
        for skin in skins:
            for attr in ("_ctrl", "touch_controller"):
                ctrl = getattr(skin, attr, None)
                if ctrl is not None and id(ctrl) not in seen:
                    seen.add(id(ctrl))
                    yield ctrl

    def _set_patient_led(self, patient: _Patient, color: str,
                         pattern: str, period_ms: int) -> None:
        """Drive the LED on every controller backing this patient's skins.

        Whole-robot patients light their whole robot; a per-skin (branch)
        patient lights only its own skin's node, so each Tree branch shows its
        own state independently."""
        for ctrl in self._controllers_of_skins(patient.skins):
            set_led = getattr(ctrl, "set_led", None)
            if set_led is None:
                continue
            try:
                set_led(color, pattern=pattern, period_ms=period_ms)
            except Exception:   # noqa: BLE001 — controller errors are non-fatal
                logger.exception("set_led failed on %s", patient.patient_id)

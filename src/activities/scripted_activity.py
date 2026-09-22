"""ScriptedActivity - runtime for the declarative behaviour engine.

Interprets a behaviour *spec* (see :mod:`src.activities.catalog`) as a
per-unit finite state machine. A **unit** is one skin: it owns its chambers,
its node's LED ring, and its touch board, so every skin of a robot runs the
spec independently (the Thymio has one skin/3 chambers; a Turtle several).

Each state runs a **program** (its ``do`` steps) as a cooperatively-scheduled
sequence: instantaneous verbs apply at once, while ``wait`` / ``wait_for_touch``
suspend the sequence until satisfied. This is what makes hand-authored
sequences like "inflate 1, wait, deflate 1, inflate 2 ..." express literally.
Transitions are re-checked every tick; the first whose ``when`` condition is
true switches the unit's state (time- and/or touch-driven).

The activity is hardware-agnostic: it drives skins via the same ``inflate`` /
``set_pressure`` / ``set_led(_halves)`` slice that real and simulated
controllers both expose, so a whole session can be rehearsed in simulation.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generator

from PySide6.QtCore import QObject, QTimer

from src.activities import catalog
from src.activities.base_activity import BaseActivity
from src.activities.led_canvas import LedZoneCanvas, HOLD_MODES, HoldFeedback
from src.activities.organ_resolver import OrganResolver
from src.core.touch_zones import TouchZoneMap
from src.activities.touch_rhythm import (MODE_AUTO, MODE_FIXED,
                                         GroupTouchSyncTracker,
                                         MagnitudeCompressionTracker,
                                         SyncScoreTracker,
                                         TouchRhythmTracker)
from src.hardware.fill_scaling import (
    MIN_PUMP_DUTY,
    POWER_MAX_LEVEL,
    duty_for_power,
)
from src.hardware.organ_sensor import OrganSensor
from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session

logger = logging.getLogger(__name__)

# Cooperative scheduler cadence. 50 ms is smooth enough for LED/chamber
# changes while leaving the GUI thread idle between ticks.
_TICK_MS = 50

# A suspended step yields one of these tokens to the driver:
#   ("ms", duration)        - resume after duration ms
#   ("touch", chamber|None) - resume on the next touch (of that chamber)
WaitToken = tuple

# Default match tolerance (ohm) for decomposing the organ circuit reading when a
# spec uses an `organs` condition. Overridable per-spec via
# ``spec["organ_tolerance_ohm"]``.
_DEFAULT_ORGAN_TOLERANCE_OHM = 80.0


@dataclass
class _Unit:
    """One skin running the spec independently."""
    unit_id: str
    robot: BaseRobot
    skin: Any
    ctrl: Any
    chambers: list[int]
    state: str = ""
    state_entered: float = 0.0
    touch_count: int = 0
    # Impacts (Thymio knocks) counted in the current state, plus the subscription
    # we installed on the robot's link so `stop` can remove it. impact_levels holds
    # one intensity (1 touch/2 knock/3 slap) per knock so `on_impact` can filter by
    # level. lifted_count / lifted_cb mirror this for the ground (lift) sensor.
    impact_count: int = 0
    impact_levels: list = field(default_factory=list)
    impact_cb: Any = None
    lifted_count: int = 0
    lifted_cb: Any = None
    touch_seq: int = 0
    touch_seq_by_chamber: dict[int, int] = field(default_factory=dict)
    active_touch: set[int] = field(default_factory=set)
    rhythm: TouchRhythmTracker = field(default_factory=TouchRhythmTracker)
    rhythm_by_sensor: dict[int, TouchRhythmTracker] = field(default_factory=dict)
    rhythm_last_press_ms: dict[int, float] = field(default_factory=dict)
    group_sync: GroupTouchSyncTracker = field(default_factory=GroupTouchSyncTracker)
    # CPR-specific live monitor payload. Present only for behaviours using the
    # `group_touch_sync` condition; the Skin exposes its current copy to Qt.
    cpr_sync_params: dict[str, Any] | None = None
    magnitude_by_sensor: dict[int, MagnitudeCompressionTracker] = field(default_factory=dict)
    # Classified gestures (tap/stroke/...) counted in the current state, by label,
    # plus the live classifier feeding them (kept alive here). Empty/None when the
    # skin has no trained model - raw-touch `gesture_count` still works.
    gesture_counts: dict[str, int] = field(default_factory=dict)
    gesture_clf: Any = None
    # The compensated-magnet callback this activity subscribed (undone on stop).
    magnet_cb: Any = None
    # A phase jump requested from inside a per-press handler (touch_progress),
    # applied at the next tick so it never mutates the running aux list mid-run.
    pending_state: str | None = None
    # Lit-pixel model of this skin's LED strip, painted by the `zone_fill` /
    # `sync_fill` blocks and sent as one `set_led_pixels` frame per change.
    # None when the skin has no strip geometry / sensor placements to join.
    canvas: LedZoneCanvas | None = None
    # Active `sync_fill` block params in the current state (None = off), the
    # pixel count it last lit and when the last decay step happened.
    sync_fill: dict[str, Any] | None = None
    sync_lit: int = 0
    sync_decay_at: float = 0.0
    # Live hold feedback of the current state's `zone_fill` (held zones tinted
    # by press strength); rebuilt on every sensor frame, cleared on phase entry.
    hold: HoldFeedback = field(default_factory=HoldFeedback)
    # Running-score whole-strip display (`score_fill`): its tracker, the
    # block params of the current state (None = off) and the pixels last lit.
    score: SyncScoreTracker = field(default_factory=SyncScoreTracker)
    score_fill: dict[str, Any] | None = None
    score_lit: int = 0
    # This skin type's power-level-1 PWM floor (see fill_scaling.duty_for_power);
    # resolved from settings at setup, defaults to the global stall floor.
    min_duty: int = MIN_PUMP_DUTY
    # Organ status, for `organs` conditions: per-organ good/bad/absent verdict
    # resolved from this skin's organ circuit, plus the sensor(s) feeding it.
    organ_verdicts: dict[str, str] = field(default_factory=dict)
    resolver: Any = None
    organ_sensors: list = field(default_factory=list)
    # Body program: a generator yielding WaitTokens, plus the wait it is
    # currently blocked on (None = ready to advance).
    runner: Generator | None = None
    wait: tuple | None = None
    # One-shot generators spawned by on_touch handlers, advanced alongside body.
    aux: list[tuple[Generator, tuple | None]] = field(default_factory=list)


class ScriptedActivity(BaseActivity):
    """Run a declarative behaviour spec against any robot."""

    robot_type = BaseRobot

    @staticmethod
    def _robot_type_for(kind: str) -> type[BaseRobot]:
        """Resolve an activity kind to its robot class (lazy import to avoid a
        cycle: robots import activities). The kind->class-name registry lives in
        :mod:`activity_kind`; only the name->class resolution happens here.
        Unknown -> BaseRobot (accepts anything)."""
        from src.activities.activity_kind import robot_type_name
        from src.robots.thymio.thymio_robot import ThymioRobot
        from src.robots.tree.tree_robot import TreeRobot
        from src.robots.turtle.turtle_robot import TurtleRobot
        by_name: dict[str, type[BaseRobot]] = {
            "ThymioRobot": ThymioRobot, "TurtleRobot": TurtleRobot,
            "TreeRobot": TreeRobot}
        return by_name.get(robot_type_name(kind) or "", BaseRobot)

    def __init__(self, name: str, description: str, spec: dict[str, Any]):
        super().__init__(name=name, description=description)
        catalog.validate_spec(spec)
        self._spec = spec
        # Declared target, or None for a target-less "any" behaviour.
        # New-style targets carry a skin condition ({"skin": ...}) and run on any
        # robot - `if_robot` blocks gate the robot-specific steps. Only a
        # LEGACY {"kind": ...} target narrows robot_type to that kind's class.
        self.target = catalog.spec_target(spec)
        # Skin condition ("natural"/"wrinkles"/"organs") the behaviour is
        # written for, or None. The session setup pre-selects by it and warns
        # when the chosen robot's configured skins don't match.
        self.skin = catalog.spec_skin(spec)
        if self.target is not None and "kind" in self.target:
            self.robot_type = self._robot_type_for(self.target["kind"])
        self._states: dict[str, Any] = spec["states"]
        self._initial: str = spec["initial"]
        self._organ_tolerance = float(
            spec.get("organ_tolerance_ohm", _DEFAULT_ORGAN_TOLERANCE_OHM))
        self._units: dict[str, _Unit] = {}
        self._tick_owner: QObject | None = None
        self._tick: QTimer | None = None
        # Callbacks fired whenever any unit changes phase (state) - manual,
        # timed or touch-driven - so a GUI can keep phase controls in sync.
        self._phase_listeners: list = []
        self._cpr_sync_params = self._find_cpr_sync_params()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        self._units.clear()
        self._phase_listeners.clear()
        settings_data = self._load_settings_data()
        for robot in robots:
            skins = getattr(robot, "skins", {})
            for skin in skins.values():
                ctrl = getattr(skin, "_ctrl", None)
                unit = _Unit(
                    unit_id=f"{robot.robot_id}/{skin.skin_id}",
                    robot=robot, skin=skin, ctrl=ctrl,
                    chambers=sorted(skin.chambers.keys()),
                    min_duty=self._resolve_min_duty(settings_data, skin),
                    cpr_sync_params=self._cpr_sync_params,
                    canvas=self._build_canvas(skin),
                )
                self._units[unit.unit_id] = unit
                self._subscribe_touch(unit)
                self._subscribe_gestures(unit)
                self._subscribe_impact(unit)
                self._subscribe_lifted(unit)
                self._setup_organs(unit, skin)
                self._publish_cpr_status(unit)
            if not skins:
                # A robot without skins (e.g. a bare Thymio) still runs the
                # spec - its unit just has no chambers/LED ring/touch board,
                # so only robot-level verbs (thymio_drive/thymio_leds) act.
                unit = _Unit(unit_id=robot.robot_id, robot=robot,
                             skin=None, ctrl=None, chambers=[])
                self._units[unit.unit_id] = unit
                self._subscribe_impact(unit)
                self._subscribe_lifted(unit)
        logger.info("ScriptedActivity %r set up: %d units",
                    self.name, len(self._units))

    @staticmethod
    def _load_settings_data() -> dict:
        """The app settings dict (for per-skin-type pump-duty floors), or {}."""
        try:
            from src.config.settings import Settings
            return Settings().data
        except Exception:   # noqa: BLE001 - settings must never block a session
            logger.exception("could not load settings for pump-duty floor")
            return {}

    @staticmethod
    def _resolve_min_duty(settings_data: dict, skin: Any) -> int:
        """This skin type's power-level-1 PWM floor, or the global default."""
        if skin is None:
            return MIN_PUMP_DUTY
        from src.hardware.fill_calibration import get_type_min_duty
        return get_type_min_duty(settings_data,
                                 getattr(skin, "skin_type", ""),
                                 getattr(skin, "skin_variant", ""))

    def start(self) -> None:
        for unit in self._units.values():
            self._enter_state(unit, self._initial)
        self._tick_owner = QObject()
        self._tick = QTimer(self._tick_owner)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        logger.info("ScriptedActivity %r started", self.name)

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
        self._tick_owner = None
        for unit in self._units.values():
            set_led = getattr(unit.ctrl, "set_led", None)
            if set_led is not None:
                try:
                    set_led("#000000", pattern="off", period_ms=0)
                except Exception:   # noqa: BLE001
                    logger.exception("set_led off failed on %s", unit.unit_id)
            # Wheeled bases (Thymio) must not keep driving past the activity.
            self._thymio_call(unit, "set_motors", 0, 0)
            self._unsubscribe_impact(unit)
            self._unsubscribe_lifted(unit)
            self._unsubscribe_touch(unit)
        self._units.clear()
        logger.info("ScriptedActivity %r stopped", self.name)

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "states": {u.unit_id: u.state for u in self._units.values()},
        }

    def force_state(self, unit_id: str, state: str) -> None:
        """Jump a unit to a state (debug / scripted demo)."""
        unit = self._units.get(unit_id)
        if unit is not None and state in self._states:
            self._enter_state(unit, state)

    def unit_state(self, unit_id: str) -> str:
        unit = self._units.get(unit_id)
        return unit.state if unit is not None else ""

    def has_phases(self) -> bool:
        """True when the spec is a multi-phase timeline (any state has a
        transition) - i.e. there is a 'next phase' to advance to. Used by the
        GUI to decide whether to offer the manual 'Next Phase' control."""
        return any(state.get("transitions")
                   for state in self._states.values())

    def advance_phase(self) -> str | None:
        """Manually advance every unit to its current state's next phase.

        The 'next phase' is the destination of the current state's first
        transition (the study conditions are linear phase1 -> phase2 -> phase3),
        so this fires the same transition the timer would, just now. Units
        already in a terminal state (no transition) are left untouched.

        Returns the state advanced into (for logging/UI), or None when no unit
        had a next phase - i.e. the timeline has reached its end.
        """
        return self._step_phase(self._next_phase)

    def rewind_phase(self) -> str | None:
        """Manually rewind every unit to its current state's previous phase.

        The 'previous phase' is the state whose first transition leads into the
        unit's current state - the inverse of :meth:`advance_phase` along the
        linear timeline. Units at the initial phase have no predecessor and are
        left untouched. Returns the state rewound into, or None when no unit
        could go back.
        """
        return self._step_phase(self._prev_phase)

    def _step_phase(self, pick) -> str | None:
        """Move every unit to the phase ``pick(state)`` selects (next/prev)."""
        moved: str | None = None
        for unit in self._units.values():
            target = pick(unit.state)
            if target is not None:
                self._enter_state(unit, target)
                moved = target
        return moved

    def can_advance_phase(self) -> bool:
        """True when at least one unit still has a next phase to advance into."""
        return any(self._next_phase(u.state) is not None
                   for u in self._units.values())

    def can_rewind_phase(self) -> bool:
        """True when at least one unit has a previous phase to rewind to."""
        return any(self._prev_phase(u.state) is not None
                   for u in self._units.values())

    def _next_phase(self, state: str) -> str | None:
        """Destination of ``state``'s first valid transition, or None."""
        for tr in self._states.get(state, {}).get("transitions") or []:
            to = tr.get("to")
            if to in self._states:
                return to
        return None

    def _prev_phase(self, state: str) -> str | None:
        """The state whose first transition leads into ``state`` (its
        predecessor in the linear timeline), or None if nothing leads to it."""
        for name in self._states:
            if name != state and self._next_phase(name) == state:
                return name
        return None

    def add_phase_listener(self, callback) -> None:
        """Register ``callback()`` to fire whenever any unit changes phase.

        Lets a GUI keep its phase controls in sync with the timeline whether a
        transition was manual, timed or touch-driven."""
        self._phase_listeners.append(callback)

    def _notify_phase_listeners(self) -> None:
        for cb in self._phase_listeners:
            try:
                cb()
            except Exception:   # noqa: BLE001 - a bad listener must not kill a tick
                logger.exception("phase listener failed")

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _enter_state(self, unit: _Unit, state: str) -> None:
        prev = unit.state
        unit.state = state
        unit.state_entered = time.monotonic()
        unit.touch_count = 0
        unit.impact_count = 0
        unit.impact_levels.clear()
        unit.lifted_count = 0
        unit.gesture_counts.clear()
        unit.rhythm.reset()
        unit.group_sync.reset()
        for tracker in unit.rhythm_by_sensor.values():
            tracker.reset()
        for tracker in unit.magnitude_by_sensor.values():
            tracker.reset()
        self._publish_cpr_status(unit)
        unit.pending_state = None
        unit.aux.clear()
        unit.sync_fill = None
        unit.sync_lit = 0
        unit.hold = HoldFeedback()
        unit.score.reset()
        unit.score_fill = None
        unit.score_lit = 0
        if unit.canvas is not None:
            unit.canvas.reset()
        body = self._states.get(state, {}).get("do", [])
        unit.runner = self._run_steps(unit, body, {})
        unit.wait = None
        if prev != state:
            logger.info("Scripted %s: %s -> %s", unit.unit_id, prev, state)
            self.log_event("activity", "state", target=unit.unit_id,
                           metadata=f'{{"from": "{prev}", "to": "{state}"}}')
        # Run the body's first slice immediately so on-enter visuals (LED) show
        # without waiting a tick.
        self._advance(unit)
        self._notify_phase_listeners()

    def _on_tick(self) -> None:
        for unit in self._units.values():
            # A per-press handler (touch_progress) may have requested a jump last
            # tick; apply it here, at a clean tick boundary, before anything else.
            if unit.pending_state is not None:
                target, unit.pending_state = unit.pending_state, None
                if target in self._states:
                    self._enter_state(unit, target)
                continue
            if self._check_transitions(unit):
                continue                       # state changed; body restarted
            self._advance(unit)
            self._advance_aux(unit)
            self._refresh_sync_fill(unit)
            self._refresh_score_fill(unit)

    def _check_transitions(self, unit: _Unit) -> bool:
        for tr in self._states.get(unit.state, {}).get("transitions", []) or []:
            when = tr.get("when", {"always": True})
            if self._eval_cond(unit, when):
                self._enter_state(unit, tr["to"])
                return True
        return False

    def _eval_cond(self, unit: _Unit, cond: dict) -> bool:
        """Evaluate a single-key condition dict (see :mod:`catalog`)."""
        if not isinstance(cond, dict) or not cond:
            return False
        name = next(iter(cond))
        val = cond[name]
        name = catalog.COND_ALIASES.get(name, name)
        if name == "elapsed_ms":
            ms = val.get("ms", val) if isinstance(val, dict) else val
            return (time.monotonic() - unit.state_entered) * 1000 >= int(ms)
        if name == "touch_count":
            need = val.get("min", val) if isinstance(val, dict) else val
            return unit.touch_count >= int(need)
        if name == "gesture_count":
            return self._eval_gesture_count(unit, val)
        if name == "touch_rhythm":
            return self._eval_touch_rhythm(unit, val)
        if name == "group_touch_rhythm":
            return self._eval_group_touch_rhythm(unit, val)
        if name == "group_touch_sync":
            return self._eval_group_touch_sync(unit, val)
        if name == "on_impact":
            if isinstance(val, dict):
                need = int(val.get("min", 1))
                level = int(val.get("level", 1))
            else:
                need = int(val) if val is not None else 1
                level = 1
            return sum(1 for lv in unit.impact_levels if lv >= level) >= need
        if name == "on_lifted":
            if isinstance(val, dict):
                need = val.get("min", 1)
            else:
                need = val if val is not None else 1
            return unit.lifted_count >= int(need)
        if name == "any":
            return any(self._eval_cond(unit, c) for c in (val or []))
        if name == "all":
            return all(self._eval_cond(unit, c) for c in (val or []))
        if name == "not":
            return not self._eval_cond(unit, val)
        if name == "organs":
            return self._eval_organs(unit, val)
        if name == "robot_is":
            want = val.get("robot", val) if isinstance(val, dict) else val
            return self._unit_kind(unit) == want
        if name == "always":
            return bool(val)
        return False

    @staticmethod
    def _eval_gesture_count(unit: _Unit, val: Any) -> bool:
        """`gesture_count` condition: N presses (kind 'touch') or N of an
        ML-classified gesture. Classified kinds read the per-label counter fed by
        the live classifier; without a trained model that counter stays empty, so
        the condition simply never fires (raw 'touch' always works)."""
        if isinstance(val, dict):
            kind = str(val.get("kind", "touch"))
            need = int(val.get("min", 1))
        else:
            kind, need = "touch", int(val) if val is not None else 1
        if kind == "touch":
            return unit.touch_count >= need
        return unit.gesture_counts.get(kind, 0) >= need

    @staticmethod
    def _eval_touch_rhythm(unit: _Unit, val: Any) -> bool:
        """Evaluate cadence parameters against the unit's touch tracker."""
        params = val if isinstance(val, dict) else {}
        return unit.rhythm.matches(
            target_interval_ms=float(params.get("target_interval_ms", 550)),
            tolerance_ms=float(params.get("tolerance_ms", 150)),
            min_gap_ms=float(params.get("min_gap_ms", 250)),
            required_intervals=int(params.get("intervals", 1)),
        )

    @staticmethod
    def _eval_group_touch_rhythm(unit: _Unit, val: Any) -> bool:
        """Check that several sensor streams have converged on one frequency."""
        params = val if isinstance(val, dict) else {}
        participants = max(1, int(params.get("participants", 3)))
        tolerance_hz = params.get("tolerance_hz")
        min_gap_ms = max(0.0, float(params.get("min_gap_ms", 20)))
        required = max(1, int(params.get("intervals", 5)))
        candidates = []
        for sensor_idx, tracker in unit.rhythm_by_sensor.items():
            if not tracker.has_matching_intervals(min_gap_ms, required):
                continue
            latest = tracker.latest_frequency_hz(min_gap_ms)
            press_ms = unit.rhythm_last_press_ms.get(sensor_idx)
            if latest is not None and press_ms is not None:
                candidates.append((sensor_idx, latest, press_ms))
        if len(candidates) < participants:
            return False
        candidates.sort(key=lambda candidate: candidate[1])
        selected = candidates[:participants]
        median = selected[len(selected) // 2][1]
        if tolerance_hz is None:
            # Keep old saved activities working after the condition became Hz-based.
            tolerance_hz = median * max(0.0, float(
                params.get("tolerance_pct", 20))) / 100.0
        allowed = max(0.0, float(tolerance_hz))
        if median <= 0 or not all(abs(interval - median) <= allowed
                                  for _, interval, _ in selected):
            return False
        touch = getattr(unit.skin, "touch", None) or {}
        sync_tolerance_ms = touch.get("rhythm_sync_tolerance_ms",
                                      params.get("sync_tolerance_ms", 150))
        sync_tolerance_ms = max(0.0, float(sync_tolerance_ms))
        press_times = [press_ms for _, _, press_ms in selected]
        return max(press_times) - min(press_times) <= sync_tolerance_ms

    @staticmethod
    def _eval_group_touch_sync(unit: _Unit, val: Any) -> bool:
        """Require consecutive lockstep multi-child compression rounds.

        Unlike ``group_touch_rhythm``, this pairs presses into individual group
        beats.  A success therefore proves that every selected child was within
        the phase window on *each* of the requested rounds, rather than merely
        having a similar latest frequency.
        """
        params = val if isinstance(val, dict) else {}
        return unit.group_sync.matches(
            **ScriptedActivity._sync_kwargs(params),
            now_ms=time.monotonic() * 1000.0,
        )

    @staticmethod
    def _sync_kwargs(params: dict) -> dict[str, Any]:
        """The ``GroupTouchSyncTracker`` arguments a `group_touch_sync` block's
        params encode - shared by the condition, the live CPR status and the
        `sync_fill` display so the three can never disagree.

        ``mode`` defaults to *fixed* when the spec lists ``sensors`` or says
        nothing (older hand-authored specs), *auto* only when it asks for it."""
        sensor_ids = params.get("sensors")
        if not isinstance(sensor_ids, (list, tuple)):
            sensor_ids = None
        mode = str(params.get("mode") or "")
        if mode != MODE_AUTO or sensor_ids is not None:
            mode = MODE_FIXED
        return {
            "participants": max(1, int(params.get("participants", 3))),
            "sensor_ids": list(sensor_ids) if sensor_ids is not None else None,
            "target_interval_ms": float(params.get("target_interval_ms", 550)),
            "cadence_tolerance_ms": max(
                0.0, float(params.get("cadence_tolerance_ms", 100))),
            "phase_tolerance_ms": max(
                0.0, float(params.get("phase_tolerance_ms", 150))),
            "min_gap_ms": max(0.0, float(params.get("min_gap_ms", 250))),
            "required_rounds": max(1, int(params.get("rounds", 6))),
            "mode": mode,
        }

    def _state_sync_params(self, unit: _Unit) -> dict[str, Any] | None:
        """The `group_touch_sync` params of the unit's CURRENT state (its
        first such transition), else the behaviour-wide one, else None."""
        for tr in self._states.get(unit.state, {}).get("transitions", []) or []:
            when = tr.get("when") if isinstance(tr, dict) else None
            params = when.get("group_touch_sync") if isinstance(when, dict) else None
            if isinstance(params, dict):
                return params
        return unit.cpr_sync_params

    def _find_cpr_sync_params(self) -> dict[str, Any] | None:
        """Find this behaviour's CPR condition configuration, if it has one."""
        for state in self._states.values():
            for transition in state.get("transitions", []) or []:
                when = transition.get("when", {}) if isinstance(transition, dict) else {}
                params = when.get("group_touch_sync") if isinstance(when, dict) else None
                if isinstance(params, dict):
                    return dict(params)
        return None

    def _publish_cpr_status(self, unit: _Unit) -> None:
        """Expose CPR progress on the Skin for the live touch-sensor window."""
        if unit.skin is None or unit.cpr_sync_params is None:
            return
        if unit.state.lower() in {"complete", "success", "done"}:
            status = {"active": True, "complete": True,
                      "rounds": unit.cpr_sync_params.get("rounds", 6),
                      "rounds_required": unit.cpr_sync_params.get("rounds", 6),
                      "reason": "CPR synchronized - LED is green"}
        else:
            status = unit.group_sync.status(
                **self._sync_kwargs(unit.cpr_sync_params),
                now_ms=time.monotonic() * 1000.0,
            )
            status["active"] = True
        # The UI only reads this immutable-at-replacement dict on its queued
        # sensor callback; assignment is atomic under CPython's GIL.
        unit.skin.cpr_sync_status = status

    @staticmethod
    def _unit_kind(unit: _Unit) -> str:
        """The unit's robot kind ("thymio"/"turtle"/"tree", "" if unknown) -
        what `if_robot` steps and `robot_is` conditions test against."""
        return getattr(unit.robot, "robot_kind", "") or ""

    def _eval_organs(self, unit: _Unit, params: Any) -> bool:
        """Evaluate an ``organs`` condition against the unit's resolved organs.

        ``all_good`` / ``all_bad`` short-circuit to 'every organ matches';
        ``count`` compares the good and bad counts via their operators. A unit
        with no organs (or no reading yet) is never satisfied."""
        if not isinstance(params, dict):
            params = {}
        verdicts = unit.organ_verdicts or {}
        total = len(verdicts)
        good = sum(1 for v in verdicts.values() if v == "good")
        bad = sum(1 for v in verdicts.values() if v == "bad")
        scope = params.get("scope", "count")
        if scope == "all_good":
            return total > 0 and good == total
        if scope == "all_bad":
            return total > 0 and bad == total
        return (self._cmp(good, params.get("good_op", ">="),
                          int(params.get("good", 0)))
                and self._cmp(bad, params.get("bad_op", "<="),
                              int(params.get("bad", 0))))

    @staticmethod
    def _cmp(actual: int, op: str, target: int) -> bool:
        if op == "<=":
            return actual <= target
        if op == "==":
            return actual == target
        return actual >= target   # default ">="

    # ------------------------------------------------------------------
    # Cooperative scheduler
    # ------------------------------------------------------------------

    def _advance(self, unit: _Unit) -> None:
        """Step the body generator past any satisfied wait."""
        if unit.runner is None:
            return
        if unit.wait is not None and not self._wait_done(unit, unit.wait):
            return
        unit.wait = None
        try:
            token = next(unit.runner)
        except StopIteration:
            unit.runner = None
            return
        except Exception:   # noqa: BLE001 - a bad step shouldn't kill the tick
            logger.exception("ScriptedActivity step failed on %s", unit.unit_id)
            unit.runner = None
            return
        unit.wait = self._arm_wait(unit, token)

    def _advance_aux(self, unit: _Unit) -> None:
        survivors: list[tuple[Generator, tuple | None]] = []
        for gen, wait in unit.aux:
            if wait is not None and not self._wait_done(unit, wait):
                survivors.append((gen, wait))
                continue
            try:
                token = next(gen)
            except StopIteration:
                continue
            except Exception:   # noqa: BLE001
                logger.exception("on_touch step failed on %s", unit.unit_id)
                continue
            survivors.append((gen, self._arm_wait(unit, token)))
        unit.aux = survivors

    def _arm_wait(self, unit: _Unit, token: WaitToken) -> tuple:
        """Turn a yielded wait token into a (kind, key, threshold) the driver
        polls. ``ms`` -> deadline timestamp; ``touch`` -> the touch counter the
        wait must see exceeded (whole-unit or per-chamber)."""
        kind = token[0]
        if kind == "ms":
            return ("ms", None, time.monotonic() + token[1] / 1000.0)
        ch = token[1]
        if ch is None:
            return ("touch", None, unit.touch_seq)
        ch = int(ch)
        return ("touch", ch, unit.touch_seq_by_chamber.get(ch, 0))

    def _wait_done(self, unit: _Unit, wait: tuple) -> bool:
        kind, ch, threshold = wait
        if kind == "ms":
            return time.monotonic() >= threshold
        if ch is None:
            return unit.touch_seq > threshold
        return unit.touch_seq_by_chamber.get(ch, 0) > threshold

    # ------------------------------------------------------------------
    # Step execution (generators)
    # ------------------------------------------------------------------

    def _run_steps(self, unit: _Unit, steps: list, ctx: dict
                   ) -> Generator:
        for step in steps or []:
            yield from self._run_step(unit, step, ctx)

    def _run_step(self, unit: _Unit, step: dict, ctx: dict) -> Generator:
        if not isinstance(step, dict) or not step:
            return
        verb = next(iter(step))
        params = step[verb]
        if not isinstance(params, dict):
            params = {"_value": params}

        if verb == "wait":
            yield ("ms", int(params.get("ms", params.get("_value", 0))))
        elif verb == "wait_for_touch":
            yield ("touch", self._resolve_chamber(unit, params, ctx))
        elif verb == "sequence":
            yield from self._run_steps(unit, params.get("do", []), ctx)
        elif verb == "if_robot":
            branch = ("do" if self._unit_kind(unit) == params.get("robot")
                      else "else")
            yield from self._run_steps(unit, params.get(branch) or [], ctx)
        elif verb == "repeat":
            yield from self._run_repeat(unit, params, ctx)
        elif verb == "for_each_chamber":
            for c in unit.chambers:
                yield from self._run_steps(unit, params.get("do", []),
                                           {**ctx, "chamber": c})
        elif verb == "beat":
            yield from self._run_beat(unit, params)
        elif verb == "fade":
            yield from self._run_fade(unit, params)
        elif verb == "thymio_drive":
            yield from self._run_thymio_drive(unit, params)
        else:
            self._apply_action(unit, verb, params, ctx)
            # instantaneous - no yield

    def _run_repeat(self, unit: _Unit, params: dict, ctx: dict) -> Generator:
        body = params.get("do", [])
        forever = bool(params.get("forever")) or \
            params.get("times", params.get("_value")) in ("forever", None)
        if forever:
            while True:
                yield from self._run_steps(unit, body, ctx)
                yield ("ms", 0)   # guarantees a yield so a wait-less body
                                  # can't spin forever inside one tick
        else:
            for _ in range(int(params.get("times", params.get("_value", 1)))):
                yield from self._run_steps(unit, body, ctx)

    def _run_beat(self, unit: _Unit, params: dict) -> Generator:
        """One heartbeat cycle. Authors wrap this in 'repeat forever'."""
        mode = params.get("mode", "sync")
        pct = int(params.get("pct", 60))
        pct2 = int(params.get("pct2", 20))
        period = max(100, int(params.get("period_ms", 2000)))
        # An optional duty makes every up-stroke gentler/slower - the beat's
        # "energy". The release back to 0 stays at full speed (a normal vent).
        duty = self._duty(unit, params)
        chambers = list(unit.chambers)
        if not chambers:
            yield ("ms", period)
            return

        if mode in ("sequential", "random"):
            order = chambers[:]
            if mode == "random":
                random.shuffle(order)
            slot = max(50, period // (2 * len(order)))
            for c in order:
                self._set_pressure(unit, c, pct, duty=duty)
                yield ("ms", slot)
                self._set_pressure(unit, c, 0)
                yield ("ms", slot)
        elif mode == "aligned":
            n_aligned = max(0, min(len(chambers), int(params.get("aligned", 2))))
            for i, c in enumerate(chambers):
                self._set_pressure(unit, c, pct if i < n_aligned else pct2,
                                   duty=duty)
            yield ("ms", period // 2)
            for c in chambers:
                self._set_pressure(unit, c, 0)
            yield ("ms", period - period // 2)
        else:  # sync
            for c in chambers:
                self._set_pressure(unit, c, pct, duty=duty)
            yield ("ms", period // 2)
            for c in chambers:
                self._set_pressure(unit, c, 0)
            yield ("ms", period - period // 2)

    def _run_fade(self, unit: _Unit, params: dict) -> Generator:
        """One smooth colour1 -> colour2 -> colour1 cross-fade across a ring
        (``ring`` selects one of the multiplexed board's three, default all).
        Authors wrap this in
        'repeat forever' for a continuous fade.

        The node runs the interpolation itself (the ``fade`` LED pattern), so
        this emits a single ``set_led`` frame per cycle instead of streaming a
        colour every tick. That is far less ESP-NOW traffic - the per-frame
        stream was heavy enough to drop/corrupt frames and flip the odd pixel to
        a stray colour. ``count=1`` runs exactly one cycle and rests on colour1,
        preserving the one-shot-unless-repeated semantics."""
        c1 = str(params.get("color1", "#000000"))
        c2 = str(params.get("color2", "#ffffff"))
        period = max(200, int(params.get("period_ms", 2000)))
        ring = self._parse_ring(params)
        self._set_led(unit, c1, "fade", period, ring=ring,
                      fade_ms=self._fade_ms(params), color2=c2, count=1)
        yield ("ms", period)

    def _run_thymio_drive(self, unit: _Unit, params: dict) -> Generator:
        """Set the Thymio's wheel targets; with ``ms`` set, drive that long
        then stop (a timed stroke), else leave them running."""
        self._thymio_call(unit, "set_motors",
                          int(params.get("left", 0)), int(params.get("right", 0)))
        ms = int(params.get("ms", 0) or 0)
        if ms > 0:
            yield ("ms", ms)
            self._thymio_call(unit, "set_motors", 0, 0)

    def _thymio_call(self, unit: _Unit, method: str, *args, **kwargs) -> None:
        """Invoke a wheeled-base method on the unit's robot, if it has one.

        Duck-typed (no robot-class import): non-Thymio robots simply lack
        ``set_motors``/``set_leds``/``play_sound`` and the verb is a logged no-op
        - that is what lets one skin-targeted behaviour run on every robot, with
        `if_robot` blocks (or this no-op) skipping the wheeled parts."""
        fn = getattr(unit.robot, method, None)
        if fn is None:
            logger.debug("Scripted %s: %s ignored (robot has no wheeled base)",
                         unit.unit_id, method)
            return
        try:
            fn(*args, **kwargs)
        except Exception:   # noqa: BLE001 - a bad link must not kill the tick
            logger.exception("%s%r failed on %s", method, args, unit.unit_id)

    # ------------------------------------------------------------------
    # Impact input (for `on_impact` conditions) - Thymio knocks
    # ------------------------------------------------------------------

    def _subscribe_impact(self, unit: _Unit) -> None:
        """Subscribe to the robot's knock stream, if it exposes one.

        Duck-typed like the wheeled-base calls: only a Thymio (with a link) has
        ``on_impact``; other robots skip it. Each knock records its intensity for the
        `on_impact` condition (which can filter by level)."""
        on_impact = getattr(unit.robot, "on_impact", None)
        if on_impact is None:
            return
        cb = lambda level=1, u=unit: self._on_impact(u, level)   # noqa: E731
        try:
            on_impact(cb)
        except Exception:   # noqa: BLE001
            logger.exception("on_impact subscribe failed on %s", unit.unit_id)
            return
        unit.impact_cb = cb

    def _unsubscribe_impact(self, unit: _Unit) -> None:
        if unit.impact_cb is None:
            return
        remove = getattr(unit.robot, "remove_impact_listener", None)
        if remove is not None:
            try:
                remove(unit.impact_cb)
            except Exception:   # noqa: BLE001
                logger.exception("on_impact unsubscribe failed on %s", unit.unit_id)
        unit.impact_cb = None

    def _on_impact(self, unit: _Unit, level: int = 1) -> None:
        """Robot read-thread callback: record a knock (with intensity) for `on_impact`."""
        unit.impact_count += 1
        unit.impact_levels.append(int(level))

    def _subscribe_lifted(self, unit: _Unit) -> None:
        """Subscribe to the robot's lift stream (ground sensors), if it exposes one."""
        on_lifted = getattr(unit.robot, "on_lifted", None)
        if on_lifted is None:
            return
        cb = lambda lifted, u=unit: self._on_lifted(u, lifted)   # noqa: E731
        try:
            on_lifted(cb)
        except Exception:   # noqa: BLE001
            logger.exception("on_lifted subscribe failed on %s", unit.unit_id)
            return
        unit.lifted_cb = cb

    def _unsubscribe_lifted(self, unit: _Unit) -> None:
        if unit.lifted_cb is None:
            return
        remove = getattr(unit.robot, "remove_lifted_listener", None)
        if remove is not None:
            try:
                remove(unit.lifted_cb)
            except Exception:   # noqa: BLE001
                logger.exception("on_lifted unsubscribe failed on %s", unit.unit_id)
        unit.lifted_cb = None

    def _on_lifted(self, unit: _Unit, lifted: bool) -> None:
        """Robot read-thread callback: count a lift-off event for `on_lifted`."""
        if lifted:
            unit.lifted_count += 1

    @staticmethod
    def _parse_rgb(hex_colour: str) -> tuple[int, int, int]:
        h = str(hex_colour).strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except (ValueError, IndexError):
            return 0, 0, 0

    # ------------------------------------------------------------------
    # Instantaneous actions
    # ------------------------------------------------------------------

    def _apply_action(self, unit: _Unit, verb: str, params: dict,
                      ctx: dict) -> None:
        if verb == "set_led":
            self._set_led(unit, params.get("color", "#000000"),
                          params.get("pattern", "solid"),
                          int(params.get("period_ms", 0)),
                          ring=self._parse_ring(params),
                          fade_ms=self._fade_ms(params),
                          angle=self._angle(params))
        elif verb == "set_led_halves":
            colors = params.get("colors", params.get("_value", []))
            self._set_led_halves(unit, list(colors),
                                 params.get("pattern", "solid"),
                                 int(params.get("period_ms", 0)),
                                 ring=self._parse_ring(params),
                                 fade_ms=self._fade_ms(params),
                                 angle=self._angle(params))
        elif verb == "touch_progress":
            self._touch_progress(unit, params)
        elif verb == "zone_fill":
            self._zone_fill(unit, params, ctx)
        elif verb == "sync_fill":
            self._sync_fill(unit, params)
        elif verb == "score_fill":
            self._score_fill(unit, params)
        elif verb in ("inflate", "set_pressure"):
            self._set_pressure(unit, self._resolve_chamber(unit, params, ctx),
                               int(params.get("pct", 60 if verb == "inflate" else 0)),
                               period_ms=int(params.get("period_ms", 0)),
                               duty=self._duty(unit, params))
        elif verb in ("deflate", "wrinkle"):
            self._set_pressure(unit, self._resolve_chamber(unit, params, ctx), 0)
        elif verb == "stop":
            for c in unit.chambers:
                hold = getattr(unit.skin, "hold", None)
                if hold is not None:
                    hold(c)
        elif verb == "thymio_leds":
            r, g, b = self._parse_rgb(params.get("color", "#000000"))
            # Aseba leds.top expects 0..32 per channel, not 0..255.
            self._thymio_call(unit, "set_leds",
                              r * 32 // 255, g * 32 // 255, b * 32 // 255)
        elif verb == "thymio_sound":
            # Priority: microSD track (>=0) -> tone (freq > 0) -> system sound.
            track = int(params.get("track", -1))
            freq = int(params.get("freq") or 0)
            if track >= 0:
                self._thymio_call(unit, "play_sound", track=track)
            elif freq > 0:
                self._thymio_call(unit, "play_sound", freq=freq,
                                  duration_ms=int(params.get("dur", 500) or 500))
            else:
                self._thymio_call(unit, "play_sound",
                                  system=int(params.get("sys", 2)))
        elif verb == "log":
            logger.info("Scripted %s: %s", unit.unit_id,
                        params.get("message", params.get("_value", "")))

    # ------------------------------------------------------------------
    # Hardware helpers
    # ------------------------------------------------------------------

    def _duty(self, unit: _Unit, params: dict) -> int | None:
        """Resolve a step's pump duty (1-255), or ``None`` for full-speed/auto.

        Prefers the friendly 1-5 ``power`` dial, mapped onto this skin type's
        calibrated duty range (level 1 = its minimum, 5 = full). Level 5 returns
        ``None`` so a full-power stroke still lets any ``over ms`` slow-fill act -
        keeping the historical default. Falls back to a raw ``duty`` (advanced /
        legacy specs); 0 / absent there means 'no duty - full speed'."""
        power = params.get("power")
        if power not in (None, ""):
            try:
                level = int(power)
            except (TypeError, ValueError):
                return None
            if level >= POWER_MAX_LEVEL:
                return None                    # full power - let period_ms act
            return duty_for_power(level, getattr(unit, "min_duty", MIN_PUMP_DUTY))
        try:
            duty = int(params.get("duty") or 0)
        except (TypeError, ValueError):
            return None
        return duty if duty > 0 else None

    def _touch_progress(self, unit: _Unit, params: dict) -> None:
        """Paint the LED ring as a touch-fill progress bar; advance when full.

        Lights ``touch_count // per`` of ``segments`` equal arcs in ``on_color``
        (the rest ``bg_color``). Dropped in a phase's ``on_touch`` it repaints on
        each press; once every arc is lit and a ``to`` phase is set it schedules
        the jump via ``pending_state`` (applied next tick, so it never disturbs
        the running on_touch handler list)."""
        segments = max(1, int(params.get("segments", 4) or 4))
        per = max(1, int(params.get("per", 1) or 1))
        on_color = str(params.get("on_color", "#2ecc71"))
        bg_color = str(params.get("bg_color", "#222222"))
        filled = max(0, min(segments, unit.touch_count // per))
        colors = [on_color] * filled + [bg_color] * (segments - filled)
        self._set_led_halves(unit, colors, ring=self._parse_ring(params),
                             fade_ms=self._fade_ms(params))
        to = str(params.get("to", "") or "")
        if to and filled >= segments and to in self._states:
            unit.pending_state = to

    # ------------------------------------------------------------------
    # Zone-aware LED fills (strip pixels grouped by touch sensor)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_canvas(skin: Any) -> LedZoneCanvas | None:
        """A lit-pixel canvas for skins that can join their LED strip to
        their sensor placements (``Skin.touch_zone_map``); None otherwise, and
        then the zone/sync fill verbs are logged no-ops on that unit."""
        get_map = getattr(skin, "touch_zone_map", None)
        if not callable(get_map):
            return None
        try:
            zone_map = get_map()
        except Exception:   # noqa: BLE001 - a bad layout must not block a session
            logger.exception("touch zone map failed on %s",
                             getattr(skin, "skin_id", "?"))
            return None
        if not isinstance(zone_map, TouchZoneMap):
            return None
        return LedZoneCanvas(zone_map)

    def _zone_fill(self, unit: _Unit, params: dict, ctx: dict) -> None:
        """Light more of the zone the current press landed in.

        Runs from a phase's ``on_touch`` handler: ``ctx["sensor"]`` is the
        sensor that fired. ``kind`` 'touch' counts every press; 'rhythmic'
        only a press whose interval to the previous press in the same zone is
        within tolerance of the target (the zone's own ``TouchRhythmTracker``,
        one interval). Once every zone is full, ``to`` schedules the phase
        jump exactly as `touch_progress` does."""
        canvas = unit.canvas
        sensor = ctx.get("sensor")
        if canvas is None or sensor is None:
            logger.debug("zone_fill ignored on %s (no canvas / sensor)",
                         unit.unit_id)
            return
        kind = str(params.get("kind", "touch") or "touch")
        if kind == "rhythmic":
            tracker = unit.rhythm_by_sensor.get(int(sensor))
            if tracker is None or not tracker.matches(
                    target_interval_ms=float(params.get("target_interval_ms", 550)),
                    tolerance_ms=float(params.get("tolerance_ms", 150)),
                    min_gap_ms=float(params.get("min_gap_ms", 250)),
                    required_intervals=1):
                return
        elif kind != "touch":
            logger.debug("zone_fill kind %r unsupported on %s", kind, unit.unit_id)
            return
        canvas.set_fill(str(params.get("fill", "") or ""))
        try:
            step = float(params.get("step_pct", 25) or 0) / 100.0
        except (TypeError, ValueError):
            step = 0.25
        canvas.fill_zone_fraction(int(sensor), step)
        self._send_canvas(unit, params)
        to = str(params.get("to", "") or "")
        if to and canvas.all_full() and to in self._states:
            unit.pending_state = to

    def _sync_fill(self, unit: _Unit, params: dict) -> None:
        """Arm the whole-strip group-progress display for this state and
        paint its current value; `_refresh_sync_fill` keeps it live."""
        if unit.canvas is None:
            logger.debug("sync_fill ignored on %s (no canvas)", unit.unit_id)
            return
        unit.sync_fill = dict(params)
        unit.canvas.set_fill(str(params.get("fill", "") or ""))
        unit.sync_lit = -1                     # force the first paint
        unit.sync_decay_at = time.monotonic()
        self._refresh_sync_fill(unit)

    def _refresh_sync_fill(self, unit: _Unit) -> None:
        """Per tick: lit share of the strip = rounds / rounds required of the
        state's `group_touch_sync`. Growth shows at once; a broken streak
        (rounds back to 0) decays one pixel per ``decay_ms`` so the drop reads
        as a fade rather than a snap (0 = drop immediately)."""
        params = unit.sync_fill
        canvas = unit.canvas
        if params is None or canvas is None:
            return
        cond = self._state_sync_params(unit)
        if cond is None:
            return
        status = unit.group_sync.status(**self._sync_kwargs(cond),
                                        now_ms=time.monotonic() * 1000.0)
        required = max(1, int(status.get("rounds_required", 1) or 1))
        rounds = max(0, int(status.get("rounds", 0) or 0))
        total = canvas.total_size
        target = total if status.get("complete") else \
            min(total, round(total * rounds / required))
        current = max(0, unit.sync_lit)
        now = time.monotonic()
        if target >= current:
            new = target
            unit.sync_decay_at = now
        else:
            try:
                decay_ms = max(0, int(params.get("decay_ms", 150) or 0))
            except (TypeError, ValueError):
                decay_ms = 150
            if decay_ms == 0:
                new = target
            elif (now - unit.sync_decay_at) * 1000.0 >= decay_ms:
                new = current - 1
                unit.sync_decay_at = now
            else:
                new = current
        if new == unit.sync_lit:
            return
        unit.sync_lit = new
        canvas.set_total_lit(new)
        self._send_canvas(unit, params)

    def _score_fill(self, unit: _Unit, params: dict) -> None:
        """Arm the running-score whole-strip display for this state: the
        tracker starts from zero with the block's round rules and the strip
        is painted dark; `_refresh_score_fill` keeps it live."""
        if unit.canvas is None:
            logger.debug("score_fill ignored on %s (no canvas)", unit.unit_id)
            return
        unit.score_fill = dict(params)
        unit.score.configure(**self._score_kwargs(params))
        unit.score.reset()
        unit.canvas.set_fill(str(params.get("fill", "") or ""))
        unit.score_lit = -1                    # force the first paint
        self._refresh_score_fill(unit)

    def _refresh_score_fill(self, unit: _Unit) -> None:
        """Per tick: close an expired round, then light the strip share equal
        to the score. Growth and shrink both show at once (a miss costs a
        visible step). A full score jumps to ``to`` like the other fills."""
        params = unit.score_fill
        canvas = unit.canvas
        if params is None or canvas is None:
            return
        unit.score.tick(time.monotonic() * 1000.0)
        target = round(canvas.total_size * unit.score.score)
        if target != unit.score_lit:
            unit.score_lit = target
            canvas.set_total_lit(target)
            self._send_canvas(unit, params)
        to = str(params.get("to", "") or "")
        if to and unit.score.complete and to in self._states \
                and unit.pending_state is None:
            unit.pending_state = to

    @staticmethod
    def _score_kwargs(params: dict) -> dict[str, Any]:
        """The ``SyncScoreTracker`` rules a `score_fill` block encodes: the
        group-sync round rules plus the gain / penalty per round (block
        percentages of the strip -> fractions)."""
        sync = ScriptedActivity._sync_kwargs(params)
        sync.pop("required_rounds", None)
        try:
            gain = float(params.get("gain_pct", 15)) / 100.0
        except (TypeError, ValueError):
            gain = 0.15
        try:
            penalty = float(params.get("penalty_pct", 5)) / 100.0
        except (TypeError, ValueError):
            penalty = 0.05
        return {**sync, "gain": gain, "penalty": penalty}

    def _send_canvas(self, unit: _Unit, params: dict) -> None:
        """Render the unit's canvas as ONE `set_led_pixels` frame."""
        canvas = unit.canvas
        send = getattr(unit.ctrl, "set_led_pixels", None)
        if canvas is None or send is None:
            return
        colors, mask = canvas.frame(str(params.get("on_color", "#2ecc71")),
                                    str(params.get("bg_color", "#222222")),
                                    unit.hold)
        try:
            send(colors, mask, pattern="solid", ring=self._parse_ring(params),
                 fade_ms=self._fade_ms(params))
        except Exception:   # noqa: BLE001
            logger.exception("set_led_pixels failed on %s", unit.unit_id)

    def _set_pressure(self, unit: _Unit, chamber, pct: int,
                      period_ms: int = 0, duty: int | None = None) -> None:
        if unit.skin is None:          # skinless unit (bare wheeled robot)
            return
        pct = max(0, min(100, int(pct)))
        try:
            if chamber == "all" or chamber is None:
                unit.skin.set_pressure(None, pct, period_ms=period_ms, duty=duty)
            else:
                unit.skin.set_pressure(int(chamber), pct,
                                       period_ms=period_ms, duty=duty)
        except (TypeError, ValueError):
            logger.debug("set_pressure(%s, %s) ignored on %s",
                         chamber, pct, unit.unit_id)

    @staticmethod
    def _fade_ms(params: dict) -> int | None:
        """Read an optional LED ``fade_ms`` (cross-fade time) from a step's params.
        Absent / invalid means 'send no fade_ms - use the node default (~250 ms)'."""
        val = params.get("fade_ms")
        if val is None:
            return None
        try:
            return max(0, int(val))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _angle(params: dict) -> float | None:
        """Read an optional LED ``angle`` (split/comet rotation, degrees) from a
        step's params. Absent / invalid / 0 means 'send no angle' (default)."""
        try:
            angle = float(params.get("angle") or 0)
        except (TypeError, ValueError):
            return None
        return angle if angle else None

    @staticmethod
    def _parse_ring(params: dict) -> int | None:
        """Read an optional LED ``ring`` (0..2) from a step's params. ``"all"`` /
        absent / invalid means 'every ring' (sent as ``None``), matching the
        prior whole-ring behaviour."""
        val = params.get("ring", "all")
        if val in (None, "all"):
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def _set_led(self, unit: _Unit, color: str, pattern: str,
                 period_ms: int, ring: int | None = None,
                 fade_ms: int | None = None, angle: float | None = None,
                 color2: str | None = None, count: int | None = None) -> None:
        set_led = getattr(unit.ctrl, "set_led", None)
        if set_led is None:
            return
        try:
            set_led(color, pattern=pattern, period_ms=period_ms, ring=ring,
                    fade_ms=fade_ms, angle=angle, color2=color2, count=count)
        except Exception:   # noqa: BLE001
            logger.exception("set_led failed on %s", unit.unit_id)

    def _set_led_halves(self, unit: _Unit, colors: list[str],
                        pattern: str = "solid", period_ms: int = 0,
                        ring: int | None = None,
                        fade_ms: int | None = None,
                        angle: float | None = None) -> None:
        if not colors:
            return
        halves = getattr(unit.ctrl, "set_led_halves", None)
        if halves is not None:
            try:
                halves(colors, pattern=pattern, period_ms=period_ms, ring=ring,
                       fade_ms=fade_ms, angle=angle)
                return
            except Exception:   # noqa: BLE001
                logger.exception("set_led_halves failed on %s", unit.unit_id)
        # Fallback: a controller without the helper at least shows one colour.
        self._set_led(unit, colors[0], pattern, period_ms, ring=ring,
                      fade_ms=fade_ms, angle=angle)

    @staticmethod
    def _resolve_chamber(unit: _Unit, params: dict, ctx: dict):
        val = params.get("chamber", params.get("_value"))
        if val in (None, "current", "c"):
            return ctx.get("chamber", unit.chambers[0] if unit.chambers else 0)
        if val == "all":
            return "all"
        try:
            return int(val)
        except (TypeError, ValueError):
            return ctx.get("chamber")

    # ------------------------------------------------------------------
    # Organ input (for `organs` conditions)
    # ------------------------------------------------------------------

    def _setup_organs(self, unit: _Unit, skin: Any) -> None:
        """Wire this skin's organ circuit so `organs` conditions can read it.

        Builds an OrganResolver from the skin's declared organ shapes and binds
        an OrganSensor to the controller/slot carrying the circuit. Cover-off
        marks every organ absent; each finite reading re-resolves the per-organ
        good/bad/absent verdicts. Skins without organs are left inert."""
        organs = list(getattr(skin, "organs", []) or [])
        if not organs:
            return
        unit.resolver = OrganResolver.from_organ_configs(
            organs, self._organ_tolerance)
        unit.organ_verdicts = {str(o.get("id", i)): "absent"
                               for i, o in enumerate(organs)}
        ctrl, slot = self._organ_controller_and_slot(skin)
        if ctrl is None or getattr(ctrl, "on_organ", None) is None:
            return
        sensor = OrganSensor(ctrl, slot=slot)
        sensor.on_cover(lambda closed, u=unit: self._on_cover(u, closed))
        sensor.on_resistance(lambda ohm, u=unit: self._on_resistance(u, ohm))
        unit.organ_sensors.append(sensor)

    @staticmethod
    def _organ_controller_and_slot(skin: Any) -> tuple[Any, int]:
        """Controller + slot carrying this skin's organ circuit."""
        ctrl = getattr(skin, "_ctrl", None)
        cfg = getattr(skin, "organ", None) or {}
        slot = int(cfg.get("slot", 0))
        mac = cfg.get("node_mac")
        if mac and getattr(ctrl, "mac_address", None) != mac:
            tc = getattr(skin, "touch_controller", None)
            if getattr(tc, "mac_address", None) == mac:
                ctrl = tc
        return ctrl, slot

    def _on_cover(self, unit: _Unit, closed: bool) -> None:
        if not closed:                      # open circuit -> every organ absent
            unit.organ_verdicts = dict.fromkeys(unit.organ_verdicts, "absent")

    def _on_resistance(self, unit: _Unit, resistance_ohm: float) -> None:
        if unit.resolver is not None:
            unit.organ_verdicts = unit.resolver.resolve(resistance_ohm)

    # ------------------------------------------------------------------
    # Touch input
    # ------------------------------------------------------------------

    def _subscribe_touch(self, unit: _Unit) -> None:
        from src.hardware.touch_source import subscribe_skin_magnet
        def cb(data: dict[str, Any], u: _Unit = unit) -> None:
            self._on_magnet(u, data)
        if subscribe_skin_magnet(unit.skin, cb):
            unit.magnet_cb = cb

    @staticmethod
    def _unsubscribe_touch(unit: _Unit) -> None:
        """Detach every sensor listener this activity attached to the unit, so a
        stopped session's handlers (and its ML classifier) stop running on the
        gateway thread - robots and skins are reused across sessions."""
        if unit.magnet_cb is not None and unit.skin is not None:
            from src.hardware.touch_source import unsubscribe_skin_magnet
            unsubscribe_skin_magnet(unit.skin, unit.magnet_cb)
            unit.magnet_cb = None
        if unit.gesture_clf is not None:
            unit.gesture_clf.detach()
            unit.gesture_clf = None
        for sensor in unit.organ_sensors:
            sensor.detach()
        unit.organ_sensors.clear()

    def _subscribe_gestures(self, unit: _Unit) -> None:
        """Attach a live ML gesture classifier so `gesture_count` can count
        classified gestures (tap/stroke/...).

        Inert and cheap when the skin has no trained model for its type - the
        classifier's ``attach()`` returns False and never subscribes - so
        raw-touch `gesture_count` keeps working while classified kinds simply
        never fire. Mirrors :meth:`_subscribe_touch`; the classifier taps the same
        compensated magnet stream itself (both subscribers coexist)."""
        if unit.skin is None:
            return
        try:
            from src.ml.touch_classifier import LiveTouchClassifier
            clf = LiveTouchClassifier(
                unit.skin,
                lambda sid, label, seg, u=unit: self._on_gesture(u, label))
            if clf.attach():
                unit.gesture_clf = clf
        except Exception:   # noqa: BLE001 - ML deps optional; must not block a session
            logger.debug("gesture classifier unavailable on %s",
                         unit.unit_id, exc_info=True)

    def _on_gesture(self, unit: _Unit, label: str) -> None:
        """Gateway-thread callback: count one classified gesture (by label).

        Plain int increment mirrors `touch_count`/`impact_count` - read on the
        GUI tick in `_eval_gesture_count`, so no lock is needed."""
        unit.gesture_counts[label] = unit.gesture_counts.get(label, 0) + 1

    def _on_magnet(self, unit: _Unit, data: dict[str, Any]) -> None:
        active = data.get("act") or []
        if not isinstance(active, list):
            return
        new_set = {int(s) for s in active
                   if str(s).lstrip("-").isdigit()}
        mapping = self._touch_mapping(unit.skin)
        now_ms = time.monotonic() * 1000.0
        magnitudes = data.get("mag")
        if isinstance(magnitudes, (list, tuple)):
            enter, exit, spike_delta = self._magnitude_thresholds(unit.skin)
            for sensor_idx, raw in enumerate(magnitudes):
                try:
                    magnitude = float(raw)
                except (TypeError, ValueError):
                    continue
                tracker = unit.magnitude_by_sensor.setdefault(
                    sensor_idx, MagnitudeCompressionTracker(
                        enter, exit, spike_delta=spike_delta))
                tracker.enter = enter
                tracker.exit = exit
                tracker.spike_delta = spike_delta
                if tracker.update(magnitude):
                    self._record_compression(unit, sensor_idx, now_ms)
        elif new_set and not unit.active_touch:
            # Backward-compatible fallback for old/binary sensor messages.
            unit.rhythm.record(now_ms)
        for sensor_idx in new_set - unit.active_touch:      # newly pressed
            if not isinstance(magnitudes, (list, tuple)):
                self._record_compression(unit, sensor_idx, now_ms)
            self._on_press(unit, mapping, sensor_idx)
        unit.active_touch = new_set
        self._refresh_hold(unit, magnitudes, new_set)
        self._publish_cpr_status(unit)

    # Press-strength levels are quantised to this many steps so a steady hold
    # does not re-send a frame on every 10 Hz sensor message.
    HOLD_LEVELS = 10

    def _state_zone_fill_params(self, unit: _Unit) -> dict[str, Any] | None:
        """The first `zone_fill` step of the current state's ``on_touch``
        handler (top level), else None."""
        for step in self._states.get(unit.state, {}).get("on_touch", []) or []:
            params = step.get("zone_fill") if isinstance(step, dict) else None
            if isinstance(params, dict):
                return params
        return None

    def _state_hold_params(self, unit: _Unit) -> dict[str, Any] | None:
        """The fill block whose ``hold`` setting (and colours) drive the live
        hold feedback: the state's `zone_fill`, else its armed `score_fill`,
        else its armed `sync_fill`."""
        return (self._state_zone_fill_params(unit)
                or unit.score_fill or unit.sync_fill)

    def _refresh_hold(self, unit: _Unit, magnitudes: Any,
                      active: set[int]) -> None:
        """Repaint the strip's live hold feedback from this sensor frame.

        The current state's `zone_fill` chooses the mode (``hold``: glow the
        held zone's lit pixels / dim its unlit ones / none) and the field
        strength read as full effect (``hold_full_ut``; the touch threshold
        is zero effect). Held zones = the active sensors; level = the
        strongest of them. Only a change of zones or (quantised) level sends
        a frame, and a frame is sent on release so the tint clears."""
        params = self._state_hold_params(unit)
        mode = str((params or {}).get("hold", "none") or "none")
        if params is None or unit.canvas is None or mode not in HOLD_MODES \
                or mode == "none":
            unit.hold = HoldFeedback()
            return
        enter, _exit, _spike = self._magnitude_thresholds(unit.skin)
        try:
            full = float(params.get("hold_full_ut", 300) or 0)
        except (TypeError, ValueError):
            full = 300.0
        if full <= enter:
            full = enter * 3 if enter > 0 else 300.0
        level = 0.0
        for sensor in active:
            if isinstance(magnitudes, (list, tuple)) and sensor < len(magnitudes):
                try:
                    strength = (float(magnitudes[sensor]) - enter) / (full - enter)
                except (TypeError, ValueError):
                    strength = 0.0
            else:
                strength = 1.0             # binary sensor: full effect while held
            level = max(level, max(0.0, min(1.0, strength)))
        level = round(level * self.HOLD_LEVELS) / self.HOLD_LEVELS
        new = HoldFeedback(frozenset(int(s) for s in active), mode, level)
        if new == unit.hold:
            return
        was_active = unit.hold.active
        unit.hold = new
        if new.active or was_active:
            self._send_canvas(unit, params)

    @staticmethod
    def _record_compression(unit: _Unit, sensor_idx: int, now_ms: float) -> None:
        """Fan one accepted physical compression into all rhythm consumers."""
        unit.rhythm_by_sensor.setdefault(
            sensor_idx, TouchRhythmTracker()).record(now_ms)
        unit.rhythm_last_press_ms[sensor_idx] = now_ms
        unit.rhythm.record(now_ms)
        unit.score.record(sensor_idx, now_ms)
        unit.group_sync.record(sensor_idx, now_ms)

    @staticmethod
    def _magnitude_thresholds(skin: Any) -> tuple[float, float, float]:
        """Read compression hysteresis thresholds from the skin touch config."""
        touch = getattr(skin, "touch", None) or {}
        enter = touch.get("rhythm_enter_ut")
        if enter is None:
            enter = touch.get("act_threshold_ut")
        if enter is None:
            thresholds = touch.get("quadrant_thresholds") or []
            enter = (sum(float(v) for v in thresholds) / len(thresholds)
                     if thresholds else 100.0)
        enter = max(0.0, float(enter))
        exit = touch.get("rhythm_exit_ut")
        exit = enter * 0.5 if exit is None else max(0.0, float(exit))
        spike_delta = touch.get("rhythm_spike_ut")
        if spike_delta is None:
            spike_delta = max(20.0, enter * 0.25)
        return enter, min(exit, enter), max(0.0, float(spike_delta))

    def _on_press(self, unit: _Unit, mapping: dict, sensor_idx: int) -> None:
        unit.touch_count += 1
        unit.touch_seq += 1
        ch = mapping.get(str(sensor_idx), mapping.get(sensor_idx))
        if ch is not None:
            try:
                ch = int(ch)
                unit.touch_seq_by_chamber[ch] = \
                    unit.touch_seq_by_chamber.get(ch, 0) + 1
            except (TypeError, ValueError):
                pass
        # Spawn the state's on_touch handler, if any, to run concurrently. The
        # context names the chamber the sensor routes to AND the sensor itself,
        # so zone-aware verbs (zone_fill) know which strip zone to paint.
        handler = self._states.get(unit.state, {}).get("on_touch")
        if handler:
            gen = self._run_steps(unit, handler,
                                  {"chamber": ch if ch is not None else None,
                                   "sensor": int(sensor_idx)})
            unit.aux.append((gen, None))

    @staticmethod
    def _touch_mapping(skin) -> dict:
        touch = getattr(skin, "touch", None) or {}
        mapping = touch.get("sensor_to_chamber")
        if isinstance(mapping, dict) and mapping:
            return mapping
        # 1:1 fallback
        n = min(len(skin.chambers), touch.get("sensor_count", len(skin.chambers)))
        return {str(i): i for i in range(n)}

"""Tests for the declarative behaviour engine.

Covers spec validation, the per-unit state machine (time + touch transitions),
the cooperative sequence scheduler (inflate / wait / deflate), beat modes, and
the declarative-activity DB round-trip. Runs entirely without hardware or a Qt
event loop: ticks are pumped manually and the clock is monkeypatched.
"""

import os
import tempfile
from datetime import datetime
from typing import Any, cast

import pytest

from src.activities import scripted_activity as sa
from src.activities.catalog import SpecError, validate_spec
from src.activities.scripted_activity import ScriptedActivity
from src.activities.seed_behaviors import SEED_CONDITIONS
from src.core.session import Session
from src.data.database import Database
from src.data.models import DeclarativeActivity


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _FakeCtrl:
    def __init__(self):
        self.led = None
        self.halves = None
        self.led_ring = None       # ring of the last set_led
        self.halves_ring = None    # ring of the last set_led_halves
        self.led_kw = {}           # extra kwargs of the last set_led (color2, ...)
        self.led_calls = 0         # how many set_led frames were sent

    def set_led(self, color, pattern="solid", period_ms=0, ring=None, **kw):
        self.led = (color, pattern)
        self.led_ring = ring
        self.led_kw = {"period_ms": period_ms, **kw}
        self.led_calls += 1
        return True

    def set_led_halves(self, colors, ring=None, **kw):
        self.halves = list(colors)
        self.halves_ring = ring
        return True


class _FakeSkin:
    # Organ-sensing extras: only set by tests that exercise organ conditions
    # (annotations only - no runtime attribute unless a test assigns them).
    organ: dict
    organs: list[dict]

    def __init__(self, skin_id="skin-1", controller=None, n_chambers=3):
        self.skin_id = skin_id
        self.chambers = {i: object() for i in range(n_chambers)}
        self.pressures: list[tuple] = []
        self.duties: list[tuple] = []
        self.touch: dict | None = None
        self.touch_controller: Any = None
        self._ctrl = controller

    def set_pressure(self, chamber_id, value, period_ms=0, duty=None):
        self.pressures.append((chamber_id, value))
        self.duties.append((chamber_id, duty))
        return True

    def hold(self, chamber_id):
        return True


class _FakeRobot:
    def __init__(self, skins, robot_kind=""):
        self.robot_id = "robot-1"
        self.robot_kind = robot_kind
        self.skins = {s.skin_id: s for s in skins}


class _Clock:
    """Monkeypatched monotonic clock so time-based waits are deterministic."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(sa.time, "monotonic", c)
    return c


def _start(activity, robot):
    """Set up + enter the initial state for every unit, without QTimer."""
    activity._setup(Session("S001", activity), [robot])
    for unit in activity._units.values():
        activity._enter_state(unit, activity._initial)
    return activity


def _unit(activity):
    return next(iter(activity._units.values()))


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

def test_seed_specs_validate():
    for _name, _desc, spec in SEED_CONDITIONS:
        validate_spec(spec)   # must not raise


def test_validate_rejects_unknown_verb():
    spec = {"initial": "s", "states": {"s": {"do": [{"explode": {}}]}}}
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_validate_rejects_missing_initial():
    spec = {"initial": "nope", "states": {"s": {"do": []}}}
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_validate_rejects_transition_to_unknown_state():
    spec = {"initial": "s", "states": {"s": {
        "do": [], "transitions": [{"to": "ghost", "when": {"always": True}}]}}}
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_validate_rejects_bad_target_kind():
    spec = {"initial": "s", "states": {"s": {"do": []}}, "target": {"kind": "nope"}}
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_legacy_kind_target_derives_robot_type():
    from src.robots.tree.tree_robot import TreeRobot
    spec = {"initial": "s", "states": {"s": {"do": []}},
            "target": {"kind": "tree"}}
    act = ScriptedActivity("Tree legacy", "d", spec)
    assert act.target == {"kind": "tree"}
    assert act.skin is None
    assert act.robot_type is TreeRobot


def test_skin_target_runs_on_any_robot():
    from src.robots.base_robot import BaseRobot
    spec = {"initial": "s", "states": {"s": {"do": []}},
            "target": {"skin": "organs"}}
    act = ScriptedActivity("Organs", "d", spec)
    assert act.target == {"skin": "organs"}
    assert act.skin == "organs"
    assert act.robot_type is BaseRobot


def test_validate_rejects_bad_target_skin():
    spec = {"initial": "s", "states": {"s": {"do": []}},
            "target": {"skin": "furry"}}
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_thymio_verbs_allowed_in_skin_targeted_spec():
    # No kind restriction outside legacy kind targets: the verb no-ops on
    # robots without a wheeled base instead of failing validation.
    spec = {"initial": "s", "states": {"s": {"do": [
        {"thymio_drive": {"left": 100, "right": 100}}]}},
        "target": {"skin": "natural"}}
    validate_spec(spec)   # must not raise


def test_legacy_spec_without_target_runs_on_any_robot():
    from src.robots.base_robot import BaseRobot
    act = ScriptedActivity("Legacy", "d", {"initial": "s", "states": {"s": {}}})
    assert act.target is None
    assert act.skin is None
    assert act.robot_type is BaseRobot


# ---------------------------------------------------------------------------
# State machine - on enter, time + touch transitions
# ---------------------------------------------------------------------------

def _condition_a():
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl)
    robot = _FakeRobot([skin])
    name, desc, spec = SEED_CONDITIONS[0]
    return ScriptedActivity(name, desc, spec), ctrl, skin, robot


def test_enter_initial_sets_purple_led(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    assert activity.unit_state(_unit(activity).unit_id) == "phase1"
    assert ctrl.led is not None
    assert ctrl.led[0] == "#8e44ad"     # purple


def test_time_advances_through_phases(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    uid = _unit(activity).unit_id

    clock.advance(121)                  # > 2 min in phase1
    activity._on_tick()
    assert activity.unit_state(uid) == "phase2"
    assert ctrl.halves == ["#8e44ad", "#f1c40f"]

    clock.advance(181)                  # > 3 min more (5 min total)
    activity._on_tick()
    assert activity.unit_state(uid) == "phase3"
    assert ctrl.led is not None
    assert ctrl.led[0] == "#f1c40f"     # yellow


def test_touch_count_shortcut_advances_before_time(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    unit = _unit(activity)

    # No time passes; enough touches must still advance the phase.
    for _ in range(20):
        activity._on_magnet(unit, {"act": [0]})
        activity._on_magnet(unit, {"act": []})
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "phase2"


def test_touch_rhythm_transition_uses_one_event_per_press(clock):
    spec = {"initial": "listening", "states": {
        "listening": {"do": [], "transitions": [{
            "to": "complete", "when": {"touch_rhythm": {
                "target_interval_ms": 100, "tolerance_ms": 5,
                "min_gap_ms": 50, "intervals": 2,
            }},
        }]},
        "complete": {"do": [], "transitions": []},
    }}
    activity = ScriptedActivity("rhythm", "", spec)
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    for _ in range(3):
        activity._on_magnet(unit, {"act": [0, 1]})
        activity._on_magnet(unit, {"act": []})
        clock.advance(0.1)

    assert unit.touch_count == 6
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "complete"


def test_touch_rhythm_outlier_resets_streak(clock):
    spec = {"initial": "s", "states": {
        "s": {"do": [], "transitions": [{
            "to": "done", "when": {"touch_rhythm": {
                "target_interval_ms": 100, "tolerance_ms": 5,
                "min_gap_ms": 50, "intervals": 2,
            }},
        }]},
        "done": {"do": [], "transitions": []},
    }}
    activity = ScriptedActivity("rhythm", "", spec)
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    for delay in (0.1, 0.25, 0.1):
        activity._on_magnet(unit, {"act": [0]})
        activity._on_magnet(unit, {"act": []})
        clock.advance(delay)

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"


def test_group_touch_rhythm_matches_three_independent_sensors(clock):
    spec = {"initial": "s", "states": {
        "s": {"do": [], "transitions": [{
            "to": "done", "when": {"group_touch_rhythm": {
                "participants": 3, "tolerance_hz": 1,
                "min_gap_ms": 50, "intervals": 2,
            }},
        }]},
        "done": {"do": [], "transitions": []},
    }}
    activity = ScriptedActivity("group rhythm", "", spec)
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    for _ in range(3):
        for sensor in (0, 1, 2):
            activity._on_magnet(unit, {"act": [sensor]})
            activity._on_magnet(unit, {"act": []})
        clock.advance(0.1)

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "done"


def test_magnitude_rhythm_counts_force_crossings_without_release(clock):
    spec = {"initial": "s", "states": {
        "s": {"do": [], "transitions": [{
            "to": "done", "when": {"touch_rhythm": {
                "target_interval_ms": 100, "tolerance_ms": 5,
                "min_gap_ms": 50, "intervals": 2,
            }},
        }]},
        "done": {"do": [], "transitions": []},
    }}
    activity = ScriptedActivity("force rhythm", "", spec)
    skin = _FakeSkin(controller=_FakeCtrl())
    skin.touch = {"rhythm_enter_ut": 70, "rhythm_exit_ut": 40}
    robot = _FakeRobot([skin])
    _start(activity, robot)
    unit = _unit(activity)

    for magnitude in (30, 70, 30):
        activity._on_magnet(unit, {"act": [], "mag": [magnitude]})
    clock.advance(0.1)
    for magnitude in (70, 30, 70):
        activity._on_magnet(unit, {"act": [], "mag": [magnitude]})
    clock.advance(0.1)
    for magnitude in (30, 70, 30):
        activity._on_magnet(unit, {"act": [], "mag": [magnitude]})

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "done"


def test_group_touch_rhythm_rejects_different_sensor_cadence(clock):
    spec = {"initial": "s", "states": {
        "s": {"do": [], "transitions": [{
            "to": "done", "when": {"group_touch_rhythm": {
                "participants": 3, "tolerance_hz": 1,
                "min_gap_ms": 50, "intervals": 2,
            }},
        }]},
        "done": {"do": [], "transitions": []},
    }}
    activity = ScriptedActivity("group rhythm", "", spec)
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    for sensor in (0, 1, 2):
        activity._on_magnet(unit, {"act": [sensor]})
        activity._on_magnet(unit, {"act": []})
    for _ in range(1, 7):
        clock.advance(0.1)
        for sensor in (0, 1):
            activity._on_magnet(unit, {"act": [sensor]})
            activity._on_magnet(unit, {"act": []})
        if _ in (3, 6):
            activity._on_magnet(unit, {"act": [2]})
            activity._on_magnet(unit, {"act": []})

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"


def _group_sync_spec(**overrides):
    params = {
        "participants": 3,
        "sensors": [0, 1, 2],
        "target_interval_ms": 100,
        "cadence_tolerance_ms": 10,
        "phase_tolerance_ms": 40,
        "min_gap_ms": 30,
        "rounds": 4,
    }
    params.update(overrides)
    return {"initial": "s", "states": {
        "s": {"do": [], "transitions": [{
            "to": "done", "when": {"group_touch_sync": params},
        }]},
        "done": {"do": [], "transitions": []},
    }}


def _emit_group_round(activity, unit, clock, offsets_ms=(0, 10, 20)):
    """Emit one three-child group beat and leave the clock at its last press."""
    for sensor, offset in enumerate(offsets_ms):
        if sensor:
            clock.advance((offset - offsets_ms[sensor - 1]) / 1000)
        activity._on_magnet(unit, {"act": [sensor]})
        activity._on_magnet(unit, {"act": []})


def test_group_touch_sync_requires_consecutive_complete_lockstep_rounds(clock):
    activity = ScriptedActivity("CPR", "", _group_sync_spec())
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    # The group medians are exactly 100 ms apart. Each group has a 20 ms
    # participant spread, well within its 40 ms phase window.
    for index in range(4):
        _emit_group_round(activity, unit, clock)
        if index < 3:
            clock.advance(0.08)  # 100 ms from one median to the next

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "done"


def test_group_touch_sync_rejects_a_late_participant_in_any_round(clock):
    activity = ScriptedActivity("CPR", "", _group_sync_spec())
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl())])
    _start(activity, robot)
    unit = _unit(activity)

    _emit_group_round(activity, unit, clock)
    clock.advance(0.08)
    # Sensor 2 arrives 80 ms after the first child, outside the 40 ms window.
    _emit_group_round(activity, unit, clock, offsets_ms=(0, 10, 80))
    clock.advance(0.02)
    _emit_group_round(activity, unit, clock)
    clock.advance(0.08)
    _emit_group_round(activity, unit, clock)

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"


def test_group_touch_sync_uses_the_configured_sensor_positions(clock):
    activity = ScriptedActivity(
        "CPR", "", _group_sync_spec(sensors=[1, 2, 3], rounds=2))
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl(), n_chambers=4)])
    _start(activity, robot)
    unit = _unit(activity)

    # Pressing 0, 1 and 2 twice must not impersonate children at zones 1, 2, 3.
    for _ in range(2):
        _emit_group_round(activity, unit, clock)
        clock.advance(0.08)

    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"


def test_advance_phase_skips_timer_and_stops_at_terminal(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    uid = _unit(activity).unit_id
    assert activity.has_phases()                  # condition A is multi-phase
    assert activity.can_advance_phase() is True
    assert activity.can_rewind_phase() is False   # at the initial phase

    # No time passes; manual advance must still move through the timeline.
    assert activity.advance_phase() == "phase2"
    assert activity.unit_state(uid) == "phase2"
    assert ctrl.halves == ["#8e44ad", "#f1c40f"]

    assert activity.advance_phase() == "phase3"
    assert activity.unit_state(uid) == "phase3"
    assert ctrl.led is not None
    assert ctrl.led[0] == "#f1c40f"               # yellow

    # phase3 is terminal (no transitions) -> nothing left to advance.
    assert activity.can_advance_phase() is False
    assert activity.advance_phase() is None
    assert activity.unit_state(uid) == "phase3"


def test_rewind_phase_goes_back_and_stops_at_initial(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    uid = _unit(activity).unit_id

    activity.advance_phase()                       # phase1 -> phase2
    activity.advance_phase()                       # phase2 -> phase3
    assert activity.unit_state(uid) == "phase3"

    assert activity.can_rewind_phase() is True
    assert activity.rewind_phase() == "phase2"
    assert activity.unit_state(uid) == "phase2"
    assert activity.rewind_phase() == "phase1"
    assert activity.unit_state(uid) == "phase1"

    # Back at the initial phase -> nothing earlier to rewind to.
    assert activity.can_rewind_phase() is False
    assert activity.rewind_phase() is None
    assert activity.unit_state(uid) == "phase1"


def test_phase_listener_fires_on_timed_and_manual_moves(clock):
    activity, ctrl, skin, robot = _condition_a()
    _start(activity, robot)
    seen: list[bool] = []
    activity.add_phase_listener(lambda: seen.append(True))

    clock.advance(121)                             # timed transition phase1->2
    activity._on_tick()
    assert len(seen) == 1

    activity.advance_phase()                       # manual transition phase2->3
    assert len(seen) == 2


def test_single_phase_spec_has_no_phases(clock):
    spec = {"initial": "s", "states": {"s": {"do": [], "transitions": []}}}
    ctrl = _FakeCtrl()
    robot = _FakeRobot([_FakeSkin(controller=ctrl)])
    activity = _start(ScriptedActivity("flat", "", spec), robot)
    assert activity.has_phases() is False
    assert activity.can_advance_phase() is False
    assert activity.can_rewind_phase() is False
    assert activity.advance_phase() is None
    assert activity.rewind_phase() is None


# ---------------------------------------------------------------------------
# Sequence scheduler - inflate / wait / deflate plays out over time
# ---------------------------------------------------------------------------

def test_sequence_inflate_wait_deflate(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"set_pressure": {"chamber": 0, "pct": 60}},
        {"wait": {"ms": 1000}},
        {"set_pressure": {"chamber": 0, "pct": 0}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("seq", "", spec)
    _start(activity, robot)

    assert skin.pressures == [(0, 60)]   # ran up to the wait

    activity._on_tick()                  # still waiting (no time passed)
    assert skin.pressures == [(0, 60)]

    clock.advance(1.0)
    activity._on_tick()                  # wait satisfied -> deflate runs
    assert skin.pressures == [(0, 60), (0, 0)]


def test_for_each_chamber_and_repeat(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"for_each_chamber": {"do": [{"inflate": {"pct": 50}}]}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl, n_chambers=3)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("fe", "", spec)
    _start(activity, robot)
    assert skin.pressures == [(0, 50), (1, 50), (2, 50)]


def test_beat_sync_sets_all_chambers(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"beat": {"mode": "sync", "pct": 70, "period_ms": 2000}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl, n_chambers=3)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("beat", "", spec)
    _start(activity, robot)
    # First half-cycle drives every chamber to the peak.
    assert set(skin.pressures) == {(0, 70), (1, 70), (2, 70)}


def test_duty_flows_through_beat_and_set_pressure(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"set_pressure": {"chamber": 0, "pct": 50, "duty": 120}},
        {"beat": {"mode": "sync", "pct": 70, "period_ms": 2000, "duty": 200}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl, n_chambers=3)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("duty", "", spec)
    _start(activity, robot)
    # The explicit set_pressure duty and every beat up-stroke carry the duty;
    # the release back to 0 stays at full speed (duty None).
    assert (0, 120) in skin.duties
    assert {(0, 200), (1, 200), (2, 200)}.issubset(set(skin.duties))
    assert (0, None) not in skin.duties[:1]   # the 50 % stroke used its duty


def test_fade_emits_one_firmware_fade_frame(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"fade": {"color1": "#000000", "color2": "#ffffff", "period_ms": 2000}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("fade", "", spec)
    _start(activity, robot)
    # The node runs the interpolation now: a single "fade" frame carries both
    # colours and the period, instead of a per-tick colour stream over ESP-NOW.
    assert ctrl.led == ("#000000", "fade")
    assert ctrl.led_kw.get("color2") == "#ffffff"
    assert ctrl.led_kw.get("period_ms") == 2000
    # No further frames stream while the fade plays out on the node.
    calls_after_start = ctrl.led_calls
    for _ in range(40):                  # drive ~4 s of ticks
        clock.advance(0.1)
        activity._on_tick()
    assert ctrl.led_calls == calls_after_start


# ---------------------------------------------------------------------------
# LED ring selection - multiplexed board's four independent rings
# ---------------------------------------------------------------------------

def _ring_activity(do_steps):
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl)
    robot = _FakeRobot([skin])
    spec = {"initial": "s", "states": {"s": {"do": do_steps, "transitions": []}}}
    activity = ScriptedActivity("ring", "", spec)
    _start(activity, robot)
    return ctrl


def test_set_led_ring_passes_through(clock):
    ctrl = _ring_activity([{"set_led": {"color": "#ff0000", "ring": 2}}])
    assert ctrl.led == ("#ff0000", "solid")
    assert ctrl.led_ring == 2


def test_set_led_without_ring_means_all(clock):
    # Omitted ring -> None (every ring), matching the prior whole-ring behaviour.
    ctrl = _ring_activity([{"set_led": {"color": "#ff0000"}}])
    assert ctrl.led_ring is None


def test_set_led_ring_all_is_none(clock):
    ctrl = _ring_activity([{"set_led": {"color": "#ff0000", "ring": "all"}}])
    assert ctrl.led_ring is None


def test_set_led_halves_ring_passes_through(clock):
    ctrl = _ring_activity([{"set_led_halves":
                            {"colors": ["#111111", "#222222"], "ring": 3}}])
    assert ctrl.halves == ["#111111", "#222222"]
    assert ctrl.halves_ring == 3


def test_fade_ring_passes_through(clock):
    ctrl = _ring_activity([{"fade": {"color1": "#000000", "color2": "#ffffff",
                                     "period_ms": 2000, "ring": 1}}])
    # The first fade frame already drives the selected ring.
    assert ctrl.led_ring == 1


def test_wait_for_touch_blocks_until_touched(clock):
    spec = {"initial": "s", "states": {"s": {"do": [
        {"wait_for_touch": {"chamber": 0}},
        {"set_pressure": {"chamber": 0, "pct": 80}},
    ], "transitions": []}}}
    ctrl = _FakeCtrl()
    skin = _FakeSkin(controller=ctrl)
    robot = _FakeRobot([skin])
    activity = ScriptedActivity("wt", "", spec)
    _start(activity, robot)
    unit = _unit(activity)

    clock.advance(10)
    activity._on_tick()
    assert skin.pressures == []          # still waiting for the touch

    activity._on_magnet(unit, {"act": [0]})   # touch chamber 0
    activity._on_tick()
    assert skin.pressures == [(0, 80)]


# ---------------------------------------------------------------------------
# Organ conditions
# ---------------------------------------------------------------------------

class _FakeOrganCtrl(_FakeCtrl):
    """Controller that also streams organ-resistance readings (on_organ)."""
    def __init__(self):
        super().__init__()
        self._organ_cbs = []

    def on_organ(self, cb):
        self._organ_cbs.append(cb)

    def fire_organ(self, resistance_ohm, slot=0):
        for cb in self._organ_cbs:
            cb(float(resistance_ohm), slot)


def _organ_spec(cond):
    """A 2-phase spec that leaves 'sick' for 'cured' when ``cond`` holds."""
    return {"initial": "sick", "states": {
        "sick": {"do": [], "transitions": [{"to": "cured", "when": cond}]},
        "cured": {"do": []}}}


def _organ_skin(ctrl):
    skin = _FakeSkin(controller=ctrl, n_chambers=2)
    skin.organ = {"slot": 0}
    skin.organs = [{"id": "1", "good_ohm": 1000, "bad_ohm": 3000},
                   {"id": "2", "good_ohm": 1000, "bad_ohm": 3000}]
    return skin


def test_organs_all_good_transition(clock):
    ctrl = _FakeOrganCtrl()
    skin = _organ_skin(ctrl)
    robot = _FakeRobot([skin])
    activity = _start(ScriptedActivity("t", "d", _organ_spec(
        {"organs": {"scope": "all_good"}})), robot)
    unit = _unit(activity)

    activity._on_tick()
    assert unit.state == "sick"                  # no reading yet -> not cured

    ctrl.fire_organ(500.0)                        # two 1000ohm good organs || = 500ohm
    assert unit.organ_verdicts == {"1": "good", "2": "good"}
    activity._on_tick()
    assert unit.state == "cured"


def test_organs_count_requires_no_bad(clock):
    ctrl = _FakeOrganCtrl()
    skin = _organ_skin(ctrl)
    robot = _FakeRobot([skin])
    activity = _start(ScriptedActivity("t", "d", _organ_spec(
        {"organs": {"scope": "count", "good_op": ">=", "good": 1,
                    "bad_op": "<=", "bad": 0}})), robot)
    unit = _unit(activity)

    # One good (1000ohm) || one bad (3000ohm) = 750ohm: a bad organ is still plugged.
    ctrl.fire_organ(750.0)
    activity._on_tick()
    assert unit.state == "sick"                  # bad_op <= 0 fails

    ctrl.fire_organ(1000.0)                       # a single good organ, no bad
    activity._on_tick()
    assert unit.state == "cured"


def test_organs_cover_off_marks_absent(clock):
    ctrl = _FakeOrganCtrl()
    skin = _organ_skin(ctrl)
    activity = _start(ScriptedActivity("t", "d", _organ_spec(
        {"organs": {"scope": "all_good"}})), _FakeRobot([skin]))
    unit = _unit(activity)

    ctrl.fire_organ(500.0)
    assert unit.organ_verdicts == {"1": "good", "2": "good"}
    ctrl.fire_organ(float("inf"))                 # cover lifted -> open circuit
    assert set(unit.organ_verdicts.values()) == {"absent"}


# ---------------------------------------------------------------------------
# DB round-trip
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = Database(path)
    database.connect()
    yield database
    database.close()
    os.unlink(path)


def test_available_activities_includes_db_behaviour(db):
    from src.activities import available_activities, get_activity

    da = DeclarativeActivity(
        activity_id=db.next_declarative_activity_id(),
        name="My Saved Behaviour", spec=SEED_CONDITIONS[1][2],
        created_at=datetime.now())
    db.save_declarative_activity(da)

    names = [a.name for a in available_activities(db)]
    assert "My Saved Behaviour" in names
    # No code-defined activities ship anymore - only the DB behaviour is offered.
    assert names == ["My Saved Behaviour"]
    assert available_activities() == []        # nothing without a database

    resolved = get_activity("My Saved Behaviour", db)
    assert resolved is not None
    assert resolved.__class__.__name__ == "ScriptedActivity"


def test_invalid_db_spec_is_skipped(db):
    from src.activities import available_activities

    bad = DeclarativeActivity(
        activity_id=db.next_declarative_activity_id(),
        name="Broken", spec={"initial": "x", "states": {}},  # empty states
        created_at=datetime.now())
    db.save_declarative_activity(bad)

    names = [a.name for a in available_activities(db)]
    assert "Broken" not in names                 # skipped, didn't crash


def test_declarative_activity_round_trip(db):
    spec = SEED_CONDITIONS[0][2]
    da = DeclarativeActivity(
        activity_id=db.next_declarative_activity_id(),
        name="My Behaviour", description="custom", spec=spec,
        created_at=datetime.now(),
    )
    assert da.activity_id == "DA001"
    db.save_declarative_activity(da)

    loaded = db.get_declarative_activity("DA001")
    assert loaded is not None
    assert loaded.name == "My Behaviour"
    assert loaded.spec == spec

    assert len(db.get_declarative_activities()) == 1
    assert db.next_declarative_activity_id() == "DA002"

    db.delete_declarative_activity("DA001")
    assert db.get_declarative_activities() == []


# ---------------------------------------------------------------------------
# Thymio verbs (thymio_drive / thymio_leds) - wheeled base
# ---------------------------------------------------------------------------

class _FakeThymioBase(_FakeRobot):
    """Fake robot with a wheeled base (duck-typed like ThymioRobot)."""

    def __init__(self, skins=()):
        super().__init__(list(skins))
        self.motors: list[tuple] = []
        self.leds: list[tuple] = []
        self.sounds: list[tuple] = []
        self._impact_cbs: list = []
        self._lifted_cbs: list = []

    def set_motors(self, left, right):
        self.motors.append((left, right))
        return True

    def set_leds(self, r, g, b):
        self.leds.append((r, g, b))
        return True

    def play_sound(self, system=None, freq=None, duration_ms=500, track=None):
        self.sounds.append((system, freq, duration_ms, track))
        return True

    def on_impact(self, callback):
        self._impact_cbs.append(callback)

    def remove_impact_listener(self, callback):
        if callback in self._impact_cbs:
            self._impact_cbs.remove(callback)

    def fire_impact(self, level=2):
        for cb in list(self._impact_cbs):
            cb(level)

    def on_lifted(self, callback):
        self._lifted_cbs.append(callback)

    def remove_lifted_listener(self, callback):
        if callback in self._lifted_cbs:
            self._lifted_cbs.remove(callback)

    def fire_lifted(self, lifted=True):
        for cb in list(self._lifted_cbs):
            cb(lifted)


def _thymio_spec(do):
    return {"initial": "s", "states": {"s": {"do": do}},
            "target": {"kind": "thymio"}}


def test_validate_thymio_verbs_gated_only_for_legacy_kind_targets():
    drive = [{"thymio_drive": {"left": 100, "right": 100}}]
    base = {"initial": "s", "states": {"s": {"do": drive}}}
    with pytest.raises(SpecError):
        validate_spec({**base, "target": {"kind": "turtle"}})  # wrong kind
    validate_spec({**base, "target": {"kind": "thymio"}})      # must not raise
    # Target-less and skin-targeted specs allow the verbs anywhere: they no-op
    # on robots without a wheeled base (wrap in if_robot to be explicit).
    validate_spec(base)
    validate_spec({**base, "target": {"skin": "natural"}})


def test_validate_thymio_verb_nested_in_control_is_gated_too():
    nested = [{"repeat": {"times": 2,
                          "do": [{"thymio_leds": {"color": "#ff0000"}}]}}]
    with pytest.raises(SpecError):
        validate_spec({"initial": "s", "states": {"s": {"do": nested}},
                       "target": {"kind": "turtle"}})


def test_bare_robot_gets_a_skinless_unit():
    robot = _FakeThymioBase()          # no skins at all
    act = ScriptedActivity("t", "d", _thymio_spec([]))
    _start(act, robot)
    assert list(act._units) == ["robot-1"]
    assert _unit(act).chambers == []


def test_thymio_drive_sets_motors_then_timed_stop(clock):
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_drive": {"left": 150, "right": -150, "ms": 500}}]))
    _start(act, robot)
    assert robot.motors == [(150, -150)]   # driving, stop still pending
    clock.advance(0.6)
    act._on_tick()
    assert robot.motors[-1] == (0, 0)      # timed stop fired


def test_thymio_drive_without_ms_keeps_driving(clock):
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_drive": {"left": 200, "right": 200}}]))
    _start(act, robot)
    clock.advance(5)
    act._on_tick()
    assert robot.motors == [(200, 200)]    # no auto-stop


def test_thymio_leds_scaled_to_aseba_0_32():
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_leds": {"color": "#ff0080"}}]))
    _start(act, robot)
    assert robot.leds == [(32, 0, 16)]     # 255->32, 0->0, 128->16


def test_thymio_sound_system_tone_and_track():
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_sound": {"sys": 3, "freq": 0, "dur": 200, "track": -1}}]))
    _start(act, robot)
    assert robot.sounds == [(3, None, 500, None)]     # freq 0, track <0 -> system

    robot2 = _FakeThymioBase()
    act2 = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_sound": {"sys": 2, "freq": 700, "dur": 250}}]))
    _start(act2, robot2)
    assert robot2.sounds == [(None, 700, 250, None)]  # freq set -> tone path

    robot3 = _FakeThymioBase()
    act3 = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_sound": {"sys": 2, "freq": 700, "track": 5}}]))
    _start(act3, robot3)
    assert robot3.sounds == [(None, None, 500, 5)]    # track >=0 wins over tone/system


def test_thymio_sound_noop_on_non_thymio_robot():
    ctrl = _FakeCtrl()
    robot = _FakeRobot([_FakeSkin(controller=ctrl)])   # no play_sound
    act = ScriptedActivity("t", "d",
                           {"initial": "s",
                            "states": {"s": {"do": [{"thymio_sound": {"sys": 1}}]}}})
    _start(act, robot)          # must not raise - duck-typed no-op


def _impact_spec(min_impacts):
    return {"initial": "p1",
            "states": {
                "p1": {"transitions": [
                    {"to": "p2", "when": {"on_impact": {"min": min_impacts}}}]},
                "p2": {}},
            "target": {"kind": "thymio"}}


def test_on_impact_condition_advances_after_enough_knocks(clock):
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _impact_spec(2))
    _start(act, robot)
    uid = _unit(act).unit_id

    robot.fire_impact()
    act._on_tick()
    assert act.unit_state(uid) == "p1"      # one knock < 2 -> still here

    robot.fire_impact()
    act._on_tick()
    assert act.unit_state(uid) == "p2"      # second knock -> advance


def test_impact_count_resets_on_state_enter(clock):
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _impact_spec(1))
    _start(act, robot)
    unit = _unit(act)
    robot.fire_impact()
    assert unit.impact_count == 1
    act._enter_state(unit, "p1")            # re-enter resets the counter
    assert unit.impact_count == 0


def test_impact_listener_removed_on_stop():
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _impact_spec(1))
    _start(act, robot)
    assert robot._impact_cbs               # subscribed
    act.stop()
    assert robot._impact_cbs == []         # unsubscribed on stop


def test_on_impact_condition_filters_by_level(clock):
    # Needs 1 hit at level >= 3 (slap). A touch/knock must not satisfy it.
    spec = {"initial": "p1",
            "states": {
                "p1": {"transitions": [
                    {"to": "p2", "when": {"on_impact": {"min": 1, "level": 3}}}]},
                "p2": {}},
            "target": {"kind": "thymio"}}
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", spec)
    _start(act, robot)
    uid = _unit(act).unit_id

    robot.fire_impact(level=1)               # a touch
    robot.fire_impact(level=2)               # a knock
    act._on_tick()
    assert act.unit_state(uid) == "p1"       # neither is a slap -> stay
    robot.fire_impact(level=3)               # a slap
    act._on_tick()
    assert act.unit_state(uid) == "p2"


def _lifted_spec(min_lifts):
    return {"initial": "p1",
            "states": {
                "p1": {"transitions": [
                    {"to": "p2", "when": {"on_lifted": {"min": min_lifts}}}]},
                "p2": {}},
            "target": {"kind": "thymio"}}


def test_on_lifted_condition_advances(clock):
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _lifted_spec(2))
    _start(act, robot)
    uid = _unit(act).unit_id

    robot.fire_lifted(True)
    robot.fire_lifted(False)                 # set-down doesn't count
    act._on_tick()
    assert act.unit_state(uid) == "p1"       # one lift < 2
    robot.fire_lifted(True)
    act._on_tick()
    assert act.unit_state(uid) == "p2"       # second lift -> advance


def test_lifted_listener_removed_on_stop():
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _lifted_spec(1))
    _start(act, robot)
    assert robot._lifted_cbs
    act.stop()
    assert robot._lifted_cbs == []


def test_activity_stop_zeroes_the_wheels():
    robot = _FakeThymioBase()
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_drive": {"left": 300, "right": 300}}]))
    _start(act, robot)
    act.stop()
    assert robot.motors[-1] == (0, 0)


def test_thymio_verbs_noop_on_robot_without_base(clock):
    """A wrong pairing (thymio spec, skin-only robot) must not crash a tick."""
    skin = _FakeSkin()
    robot = _FakeRobot([skin])
    act = ScriptedActivity("t", "d", _thymio_spec(
        [{"thymio_drive": {"left": 100, "right": 100, "ms": 100}},
         {"thymio_leds": {"color": "#00ff00"}}]))
    _start(act, robot)
    clock.advance(0.2)
    act._on_tick()                          # no AttributeError


# ---------------------------------------------------------------------------
# if_robot / robot_is - one behaviour, per-robot branches
# ---------------------------------------------------------------------------

def _if_robot_spec(robot="thymio", do=None, els=None):
    return {"initial": "s", "states": {"s": {"do": [
        {"if_robot": {"robot": robot, "do": do or [], "else": els or []}}]}},
        "target": {"skin": "natural"}}


def test_if_robot_runs_do_branch_on_matching_kind(clock):
    skin = _FakeSkin()
    robot = _FakeRobot([skin], robot_kind="turtle")
    act = ScriptedActivity("t", "d", _if_robot_spec(
        "turtle", do=[{"inflate": {"chamber": 0, "pct": 70}}],
        els=[{"inflate": {"chamber": 0, "pct": 10}}]))
    _start(act, robot)
    assert skin.pressures == [(0, 70)]


def test_if_robot_runs_else_branch_on_other_kind(clock):
    skin = _FakeSkin()
    robot = _FakeRobot([skin], robot_kind="tree")
    act = ScriptedActivity("t", "d", _if_robot_spec(
        "turtle", do=[{"inflate": {"chamber": 0, "pct": 70}}],
        els=[{"inflate": {"chamber": 0, "pct": 10}}]))
    _start(act, robot)
    assert skin.pressures == [(0, 10)]


def test_if_robot_gates_thymio_drive_to_the_thymio(clock):
    """One spec, two robots: only the Thymio's unit drives its wheels."""
    thymio = _FakeThymioBase([_FakeSkin("t-skin")])
    thymio.robot_kind = "thymio"
    turtle_skin = _FakeSkin("u-skin")
    turtle = _FakeRobot([turtle_skin], robot_kind="turtle")
    turtle.robot_id = "robot-2"
    act = ScriptedActivity("t", "d", _if_robot_spec(
        "thymio", do=[{"thymio_drive": {"left": 100, "right": 100}}],
        els=[{"inflate": {"chamber": 0, "pct": 40}}]))
    from src.robots.base_robot import BaseRobot
    act._setup(Session("S001", act), cast("list[BaseRobot]", [thymio, turtle]))
    for unit in act._units.values():
        act._enter_state(unit, act._initial)
    assert thymio.motors == [(100, 100)]
    assert turtle_skin.pressures == [(0, 40)]


def test_if_robot_missing_else_is_noop(clock):
    skin = _FakeSkin()
    robot = _FakeRobot([skin], robot_kind="tree")
    spec = {"initial": "s", "states": {"s": {"do": [
        {"if_robot": {"robot": "thymio",
                      "do": [{"inflate": {"chamber": 0, "pct": 70}}]}}]}}}
    act = ScriptedActivity("t", "d", spec)
    _start(act, robot)
    assert skin.pressures == []


def test_robot_is_condition_gates_transition(clock):
    spec = {"initial": "s1", "states": {
        "s1": {"do": [], "transitions": [
            {"to": "s2", "when": {"robot_is": {"robot": "tree"}}}]},
        "s2": {"do": []}}}
    tree_unit = _FakeRobot([_FakeSkin()], robot_kind="tree")
    act = ScriptedActivity("t", "d", spec)
    _start(act, tree_unit)
    act._on_tick()
    assert act.unit_state(_unit(act).unit_id) == "s2"

    turtle_unit = _FakeRobot([_FakeSkin()], robot_kind="turtle")
    act2 = ScriptedActivity("t", "d", spec)
    _start(act2, turtle_unit)
    act2._on_tick()
    assert act2.unit_state(_unit(act2).unit_id) == "s1"


def test_stop_detaches_magnet_listener(clock):
    """Robots and skins outlive a session: stop() must unsubscribe the touch
    handler, or every past session keeps reacting on the gateway thread."""
    from src.hardware.simulated_magnet_sensor import SimulatedMagnetSensor
    activity, _ctrl, skin, robot = _condition_a()
    board = SimulatedMagnetSensor("AA:BB:CC:DD:EE:FF")
    skin.touch_controller = board
    _start(activity, robot)
    assert len(board._magnet_callbacks) == 1

    activity.stop()

    assert board._magnet_callbacks == []


# ---------------------------------------------------------------------------
# Zone-aware LED fills (zone_fill / sync_fill) + auto group mode
# ---------------------------------------------------------------------------

class _FakePixelCtrl(_FakeCtrl):
    """A controller that also takes the one-frame pixel mask."""
    def __init__(self):
        super().__init__()
        self.pixels: list[tuple[list[str], str]] = []

    def set_led_pixels(self, colors, mask, pattern="solid", ring=None, **kw):
        self.pixels.append((list(colors), str(mask)))
        return True


class _ZoneSkin(_FakeSkin):
    """A skin whose LED strip and sensor placements can be joined."""
    def __init__(self, controller, sensor_quadrants=None):
        super().__init__(controller=controller, n_chambers=3)
        from src.core.led_geometry import LedStripGeometry
        from src.core.touch_zones import TouchZoneMap, quadrant_placements
        self.touch = {"sensor_count": 4}
        if sensor_quadrants:
            self.touch["sensor_quadrants"] = sensor_quadrants
        self._zone_map = TouchZoneMap(
            LedStripGeometry(count=68, gap=1),
            quadrant_placements(4, sensor_quadrants))

    def touch_zone_map(self):
        return self._zone_map


def _lit_pixels(ctrl: _FakePixelCtrl) -> set[int]:
    from src.core.led_geometry import decode_pixel_mask
    _colors, mask = ctrl.pixels[-1]
    return {i for i, c in enumerate(decode_pixel_mask(mask, 68)) if c}


def _press(activity, unit, sensor):
    activity._on_magnet(unit, {"act": [sensor]})
    activity._on_magnet(unit, {"act": []})


def _zone_fill_spec(**overrides):
    params = {"kind": "touch", "step_pct": 50, "fill": "contiguous",
              "on_color": "#00ff00", "bg_color": "#000000", "to": "done"}
    params.update(overrides)
    return {"initial": "s", "states": {
        "s": {"do": [], "on_touch": [{"zone_fill": params}], "transitions": []},
        "done": {"do": [], "transitions": []},
    }}


def test_zone_fill_lights_only_the_touched_zone_and_advances_when_full(clock):
    ctrl = _FakePixelCtrl()
    skin = _ZoneSkin(ctrl)
    activity = ScriptedActivity("Zones", "", _zone_fill_spec())
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    assert unit.canvas is not None
    zone_map = skin.touch_zone_map()

    _press(activity, unit, 2)
    activity._on_tick()                       # runs the on_touch handler
    lit = _lit_pixels(ctrl)
    assert lit and lit <= set(zone_map.zone_pixels(2))
    assert len(lit) == -(-len(zone_map.zone_pixels(2)) // 2)   # half, rounded up
    assert ctrl.pixels[-1][0] == ["#000000", "#00ff00"]

    # Filling every zone (two half-steps each) schedules the jump.
    for sensor in (0, 1, 2, 3):
        for _ in range(2):
            _press(activity, unit, sensor)
            activity._on_tick()
    assert unit.canvas.all_full()
    activity._on_tick()                       # pending_state applied at a tick
    assert activity.unit_state(unit.unit_id) == "done"
    assert not unit.canvas.all_full()         # reset on phase entry


def test_zone_fill_rhythmic_ignores_the_first_press_and_off_beat_presses(clock):
    ctrl = _FakePixelCtrl()
    skin = _ZoneSkin(ctrl)
    activity = ScriptedActivity("Zones", "", _zone_fill_spec(
        kind="rhythmic", step_pct=10, target_interval_ms=500,
        tolerance_ms=100, min_gap_ms=50, to=""))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)

    _press(activity, unit, 1)                 # first press: starts the clock
    activity._on_tick()
    assert not ctrl.pixels
    clock.advance(0.5)
    _press(activity, unit, 1)                 # on the beat
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) > 0
    lit_before = len(_lit_pixels(ctrl))
    clock.advance(1.5)
    _press(activity, unit, 1)                 # far too late: no growth
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == lit_before
    # Another zone keeps its own cadence.
    _press(activity, unit, 3)
    clock.advance(0.45)
    _press(activity, unit, 3)
    activity._on_tick()
    lit = _lit_pixels(ctrl)
    assert lit & set(skin.touch_zone_map().zone_pixels(3))


def test_zone_fill_follows_the_configured_sensor_quadrants(clock):
    ctrl = _FakePixelCtrl()
    swapped = _ZoneSkin(ctrl, sensor_quadrants={"0": "Q4", "3": "Q1"})
    activity = ScriptedActivity("Zones", "", _zone_fill_spec(to=""))
    _start(activity, _FakeRobot([swapped]))
    unit = _unit(activity)
    _press(activity, unit, 0)
    activity._on_tick()
    default_map = _ZoneSkin(_FakePixelCtrl()).touch_zone_map()
    # Sensor 0 now lives in Q4, so its pixels are the default layout's zone 3.
    assert _lit_pixels(ctrl) <= set(default_map.zone_pixels(3))


def test_zone_fill_is_a_no_op_without_a_zone_map(clock):
    ctrl = _FakePixelCtrl()
    activity = ScriptedActivity("Zones", "", _zone_fill_spec())
    _start(activity, _FakeRobot([_FakeSkin(controller=ctrl)]))
    unit = _unit(activity)
    assert unit.canvas is None
    _press(activity, unit, 0)
    activity._on_tick()
    assert not ctrl.pixels
    assert activity.unit_state(unit.unit_id) == "s"


def test_on_touch_context_names_the_sensor(clock):
    seen = []
    activity = ScriptedActivity("Ctx", "", {"initial": "s", "states": {
        "s": {"do": [], "on_touch": [{"log": "x"}], "transitions": []}}})
    _start(activity, _FakeRobot([_FakeSkin(controller=_FakeCtrl())]))
    unit = _unit(activity)
    original = activity._run_steps

    def spy(u, steps, ctx):
        seen.append(dict(ctx))
        return original(u, steps, ctx)
    activity._run_steps = spy   # type: ignore[method-assign]
    _press(activity, unit, 2)
    assert seen and seen[-1]["sensor"] == 2 and seen[-1]["chamber"] == 2


def _sync_fill_spec(rounds=4, decay_ms=0, **cond):
    params = {"participants": 3, "sensors": [0, 1, 2],
              "target_interval_ms": 100, "cadence_tolerance_ms": 10,
              "phase_tolerance_ms": 40, "min_gap_ms": 30, "rounds": rounds}
    params.update(cond)
    return {"initial": "s", "states": {
        "s": {"do": [{"sync_fill": {"fill": "contiguous", "decay_ms": decay_ms,
                                    "on_color": "#ffffff", "bg_color": "#000000"}}],
              "transitions": [{"to": "done", "when": {"group_touch_sync": params}}]},
        "done": {"do": [], "transitions": []},
    }}


def test_sync_fill_lights_the_whole_strip_share_of_completed_rounds(clock):
    ctrl = _FakePixelCtrl()
    skin = _ZoneSkin(ctrl)
    activity = ScriptedActivity("CPR", "", _sync_fill_spec(rounds=4))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    assert ctrl.pixels and not _lit_pixels(ctrl)      # painted dark on enter

    _emit_group_round(activity, unit, clock)          # round 1 -> streak 1
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == round(68 * 1 / 4)
    clock.advance(0.08)
    _emit_group_round(activity, unit, clock)          # streak 2
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == round(68 * 2 / 4)
    # Pixels are spread over the WHOLE strip (contiguous here: 0..33), not one zone.
    assert _lit_pixels(ctrl) == set(range(34))


def test_sync_fill_drops_when_the_streak_goes_stale_with_optional_decay(clock):
    ctrl = _FakePixelCtrl()
    activity = ScriptedActivity("CPR", "", _sync_fill_spec(rounds=4, decay_ms=100))
    _start(activity, _FakeRobot([_ZoneSkin(ctrl)]))
    unit = _unit(activity)
    _emit_group_round(activity, unit, clock)
    clock.advance(0.08)
    _emit_group_round(activity, unit, clock)
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == 34
    clock.advance(1.0)                                 # stale: rounds -> 0
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == 33                # one pixel per decay step
    clock.advance(0.05)
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == 33                # not yet
    clock.advance(0.06)
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == 32


def test_sync_fill_without_decay_drops_at_once(clock):
    ctrl = _FakePixelCtrl()
    activity = ScriptedActivity("CPR", "", _sync_fill_spec(rounds=4, decay_ms=0))
    _start(activity, _FakeRobot([_ZoneSkin(ctrl)]))
    unit = _unit(activity)
    _emit_group_round(activity, unit, clock)
    activity._on_tick()
    assert len(_lit_pixels(ctrl)) == 17
    clock.advance(1.0)
    activity._on_tick()
    assert not _lit_pixels(ctrl)


def test_group_sync_auto_mode_takes_whoever_presses_and_a_joiner_restarts(clock):
    activity = ScriptedActivity("CPR", "", _group_sync_spec(
        mode="auto", sensors=None, participants=3, rounds=2))
    activity._states["s"]["transitions"][0]["when"]["group_touch_sync"].pop("sensors")
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl(), n_chambers=4)])
    _start(activity, robot)
    unit = _unit(activity)

    # Children at zones 1, 2, 3 (not 0..2) synchronize: auto mode accepts them.
    def round_at(sensors):
        for i, sensor in enumerate(sensors):
            if i:
                clock.advance(0.01)
            activity._on_magnet(unit, {"act": [sensor]})
            activity._on_magnet(unit, {"act": []})

    round_at((1, 2, 3))
    clock.advance(0.08)
    round_at((1, 2, 3))
    status = unit.group_sync.status(**activity._sync_kwargs(
        activity._states["s"]["transitions"][0]["when"]["group_touch_sync"]),
        now_ms=sa.time.monotonic() * 1000.0)
    assert status["sensors"] == [1, 2, 3] and status["rounds"] == 2
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "done"


def test_group_sync_auto_mode_fourth_child_joins_for_good(clock):
    activity = ScriptedActivity("CPR", "", _group_sync_spec(
        mode="auto", participants=3, rounds=2))
    activity._states["s"]["transitions"][0]["when"]["group_touch_sync"].pop("sensors")
    robot = _FakeRobot([_FakeSkin(controller=_FakeCtrl(), n_chambers=4)])
    _start(activity, robot)
    unit = _unit(activity)
    cond = activity._states["s"]["transitions"][0]["when"]["group_touch_sync"]

    def round_at(sensors):
        for i, sensor in enumerate(sensors):
            if i:
                clock.advance(0.01)
            activity._on_magnet(unit, {"act": [sensor]})
            activity._on_magnet(unit, {"act": []})

    round_at((0, 1, 2))
    clock.advance(0.08)
    round_at((0, 1, 2, 3))                     # a fourth child joins
    status = unit.group_sync.status(**activity._sync_kwargs(cond),
                                    now_ms=sa.time.monotonic() * 1000.0)
    assert status["sensors"] == [0, 1, 2, 3]
    assert status["rounds"] == 1               # the 3-child round no longer counts
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"
    clock.advance(0.07)
    round_at((0, 1, 2))                        # back to three: incomplete
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "s"
    clock.advance(0.07)
    round_at((0, 1, 2, 3))
    clock.advance(0.07)
    round_at((0, 1, 2, 3))
    activity._on_tick()
    assert activity.unit_state(unit.unit_id) == "done"


def test_group_sync_auto_mode_waits_for_enough_children(clock):
    activity = ScriptedActivity("CPR", "", _group_sync_spec(mode="auto", rounds=1))
    cond = activity._states["s"]["transitions"][0]["when"]["group_touch_sync"]
    cond.pop("sensors")
    _start(activity, _FakeRobot([_FakeSkin(controller=_FakeCtrl())]))
    unit = _unit(activity)
    activity._on_magnet(unit, {"act": [0]}); activity._on_magnet(unit, {"act": []})
    clock.advance(0.01)
    activity._on_magnet(unit, {"act": [1]}); activity._on_magnet(unit, {"act": []})
    status = unit.group_sync.status(**activity._sync_kwargs(cond),
                                    now_ms=sa.time.monotonic() * 1000.0)
    assert status["rounds"] == 0 and status["mode"] == "auto"
    assert "Waiting for 1 more child" in status["reason"]


def test_sync_kwargs_mode_defaults():
    kw = ScriptedActivity._sync_kwargs({"sensors": [0, 1, 2], "mode": "auto"})
    assert kw["mode"] == "fixed"               # a sensor list is always fixed
    assert ScriptedActivity._sync_kwargs({})["mode"] == "fixed"
    assert ScriptedActivity._sync_kwargs({"mode": "auto"})["mode"] == "auto"


# ---------------------------------------------------------------------------
# zone_fill live hold feedback (glow / dim by press strength)
# ---------------------------------------------------------------------------

def _hold_skin(ctrl, enter_ut=100.0):
    skin = _ZoneSkin(ctrl)
    skin.touch["act_threshold_ut"] = enter_ut
    return skin


def _frame(activity, unit, act, mag):
    activity._on_magnet(unit, {"act": act, "mag": mag})


def test_zone_fill_hold_glow_tints_the_held_zone_by_press_strength(clock):
    ctrl = _FakePixelCtrl()
    skin = _hold_skin(ctrl)
    activity = ScriptedActivity("Hold", "", _zone_fill_spec(
        kind="touch", step_pct=50, to="", hold="glow", hold_full_ut=300,
        on_color="#000000", bg_color="#8e44ad"))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    zone_map = skin.touch_zone_map()

    # Press zone 2 at threshold strength: lights half the zone, no tint yet.
    _frame(activity, unit, [2], [0, 0, 100, 0])
    activity._on_tick()
    assert ctrl.pixels[-1][0] == ["#8e44ad", "#000000"]
    lit = _lit_pixels(ctrl)
    assert lit <= set(zone_map.zone_pixels(2))

    # Still held, harder: half way between threshold and full -> grey tint on
    # exactly the lit pixels of zone 2, nothing else changes.
    n = len(ctrl.pixels)
    _frame(activity, unit, [2], [0, 0, 200, 0])
    assert len(ctrl.pixels) == n + 1
    colors, mask = ctrl.pixels[-1]
    assert colors == ["#8e44ad", "#000000", "#808080"]
    from src.core.led_geometry import decode_pixel_mask
    codes = decode_pixel_mask(mask, 68)
    assert {i for i, c in enumerate(codes) if c == 2} == lit

    # Same strength again: no new frame (quantised level unchanged).
    _frame(activity, unit, [2], [0, 0, 205, 0])
    assert len(ctrl.pixels) == n + 1

    # Release: one plain frame clears the tint; further idle frames send nothing.
    _frame(activity, unit, [], [0, 0, 0, 0])
    assert len(ctrl.pixels) == n + 2
    assert ctrl.pixels[-1][0] == ["#8e44ad", "#000000"]
    _frame(activity, unit, [], [0, 0, 0, 0])
    assert len(ctrl.pixels) == n + 2


def test_zone_fill_hold_dim_fades_the_held_zones_unlit_pixels(clock):
    ctrl = _FakePixelCtrl()
    skin = _hold_skin(ctrl)
    activity = ScriptedActivity("Hold", "", _zone_fill_spec(
        kind="touch", step_pct=50, to="", hold="dim", hold_full_ut=300,
        on_color="#f1c40f", bg_color="#8e44ad"))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    zone_map = skin.touch_zone_map()

    _frame(activity, unit, [1], [0, 300, 0, 0])      # full strength at once
    activity._on_tick()                              # lights half of zone 1
    # The on_touch repaint keeps the live tint (unit.hold) in the frame.
    colors, mask = ctrl.pixels[-1]
    assert colors == ["#8e44ad", "#f1c40f", "#000000"]
    from src.core.led_geometry import decode_pixel_mask
    codes = decode_pixel_mask(mask, 68)
    dimmed = {i for i, c in enumerate(codes) if c == 2}
    lit = {i for i, c in enumerate(codes) if c == 1}
    assert dimmed | lit == set(zone_map.zone_pixels(1)) and not dimmed & lit


def test_zone_fill_hold_none_sends_nothing_extra(clock):
    ctrl = _FakePixelCtrl()
    skin = _hold_skin(ctrl)
    activity = ScriptedActivity("Hold", "", _zone_fill_spec(kind="touch", to=""))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    _frame(activity, unit, [0], [300, 0, 0, 0])
    activity._on_tick()
    n = len(ctrl.pixels)
    _frame(activity, unit, [0], [300, 0, 0, 0])
    _frame(activity, unit, [], [0, 0, 0, 0])
    assert len(ctrl.pixels) == n
    assert all(len(colors) == 2 for colors, _m in ctrl.pixels)


# ---------------------------------------------------------------------------
# score_fill (running score of synchronized rounds on the whole strip)
# ---------------------------------------------------------------------------

def _score_fill_spec(**overrides):
    params = {"mode": "auto", "participants": 3, "target_interval_ms": 100,
              "cadence_tolerance_ms": 10, "phase_tolerance_ms": 40,
              "min_gap_ms": 30, "gain_pct": 25, "penalty_pct": 25,
              "fill": "contiguous", "on_color": "#f1c40f",
              "bg_color": "#8e44ad", "to": "done"}
    params.update(overrides)
    return {"initial": "s", "states": {
        "s": {"do": [{"score_fill": params}], "transitions": []},
        "done": {"do": [], "transitions": []},
    }}


def _beat(activity, unit, clock, t0, at_ms, sensors=(0, 1, 2)):
    """Group beat at absolute ``t0 + at_ms``: sensors press 10 ms apart."""
    for i, sensor in enumerate(sensors):
        clock.t = t0 + (at_ms + 10 * i) / 1000.0
        _press(activity, unit, sensor)


def _settle(activity, unit, clock, t0, at_ms):
    """Move past the phase window and tick so the open round is judged."""
    clock.t = t0 + at_ms / 1000.0
    activity._on_tick()


def test_score_fill_grows_on_synced_rounds_shrinks_on_misses_and_completes(clock):
    ctrl = _FakePixelCtrl()
    skin = _ZoneSkin(ctrl)
    activity = ScriptedActivity("Score", "", _score_fill_spec())
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    t0 = clock.t
    assert ctrl.pixels and not _lit_pixels(ctrl)          # painted dark on enter
    assert ctrl.pixels[-1][0] == ["#8e44ad", "#f1c40f"]

    _beat(activity, unit, clock, t0, 0); _settle(activity, unit, clock, t0, 70)
    assert len(_lit_pixels(ctrl)) == 17                    # 25 % of 68
    _beat(activity, unit, clock, t0, 100); _settle(activity, unit, clock, t0, 170)
    assert _lit_pixels(ctrl) == set(range(34))             # whole strip, in order

    _beat(activity, unit, clock, t0, 200, sensors=(0,))    # one child alone: miss
    _settle(activity, unit, clock, t0, 270)
    assert len(_lit_pixels(ctrl)) == 17                    # a step back to purple

    _beat(activity, unit, clock, t0, 300); _settle(activity, unit, clock, t0, 370)
    assert len(_lit_pixels(ctrl)) == 17                    # off cadence: neutral
    for at in (400, 500, 600):
        _beat(activity, unit, clock, t0, at); _settle(activity, unit, clock, t0, at + 70)
    assert len(_lit_pixels(ctrl)) == 68
    activity._on_tick()                                    # pending jump applied
    assert activity.unit_state(unit.unit_id) == "done"


def test_score_fill_hold_dims_the_touched_zones_unlit_pixels(clock):
    ctrl = _FakePixelCtrl()
    skin = _hold_skin(ctrl)
    activity = ScriptedActivity("Score", "", _score_fill_spec(
        hold="dim", hold_full_ut=300, to=""))
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    zone_map = skin.touch_zone_map()
    _frame(activity, unit, [1], [0, 300, 0, 0])
    colors, mask = ctrl.pixels[-1]
    assert colors == ["#8e44ad", "#f1c40f", "#000000"]
    from src.core.led_geometry import decode_pixel_mask
    codes = decode_pixel_mask(mask, 68)
    assert {i for i, c in enumerate(codes) if c == 2} == set(zone_map.zone_pixels(1))
    _frame(activity, unit, [], [0, 0, 0, 0])
    assert ctrl.pixels[-1][0] == ["#8e44ad", "#f1c40f"]


def test_sync_fill_hold_uses_the_armed_blocks_colours(clock):
    ctrl = _FakePixelCtrl()
    skin = _hold_skin(ctrl)
    spec = _sync_fill_spec(rounds=4)
    spec["states"]["s"]["do"][0]["sync_fill"].update(
        {"hold": "glow", "hold_full_ut": 300})
    activity = ScriptedActivity("CPR", "", spec)
    _start(activity, _FakeRobot([skin]))
    unit = _unit(activity)
    _emit_group_round(activity, unit, clock)                # streak 1 -> 17 px lit
    activity._on_tick()
    lit = _lit_pixels(ctrl)
    assert lit
    _frame(activity, unit, [0], [200, 0, 0, 0])             # held at half strength
    colors, mask = ctrl.pixels[-1]
    assert colors == ["#000000", "#ffffff", "#ffffff"]      # glow of white = white
    from src.core.led_geometry import decode_pixel_mask
    codes = decode_pixel_mask(mask, 68)
    zone0 = set(skin.touch_zone_map().zone_pixels(0))
    assert {i for i, c in enumerate(codes) if c == 2} == lit & zone0

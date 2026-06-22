"""Tests for the organ + cover sensing chain.

Covers, bottom-up:
- ``ESP32Controller`` dispatch of ``type:"organ"`` messages;
- ``OrganSensor`` splitting raw readings into cover / resistance events;
- ``OrganMatcher`` cure decisions (aggregate + per-organ);
- ``OrganSwapActivity`` sick / open / cured transitions and event logging.
"""

import math
from unittest.mock import MagicMock

from src.activities.organ_matching import OrganMatcher
from src.hardware.esp32_controller import ESP32Controller
from src.hardware.organ_sensor import OrganSensor
from src.hardware.simulated_controller import SimulatedController


MAC = "AA:BB:CC:DD:EE:01"


# ---------------------------------------------------------------------------
# ESP32Controller organ dispatch
# ---------------------------------------------------------------------------

def test_controller_dispatches_organ_resistance():
    controller = ESP32Controller(MAC, MagicMock())
    readings: list[tuple[float, int]] = []
    controller.on_organ(lambda ohm, slot: readings.append((ohm, slot)))

    controller._handle_message(
        {"source": MAC, "type": "organ", "resistance_ohm": 952.4, "open": False})
    assert readings == [(952.4, 0)]


def test_controller_dispatches_organ_slot():
    controller = ESP32Controller(MAC, MagicMock())
    readings: list[tuple[float, int]] = []
    controller.on_organ(lambda ohm, slot: readings.append((ohm, slot)))

    controller._handle_message(
        {"source": MAC, "type": "organ", "slot": 2,
         "resistance_ohm": 1500.0, "open": False})
    assert readings == [(1500.0, 2)]


def test_controller_dispatches_open_circuit_as_inf():
    controller = ESP32Controller(MAC, MagicMock())
    readings: list[tuple[float, int]] = []
    controller.on_organ(lambda ohm, slot: readings.append((ohm, slot)))

    controller._handle_message(
        {"source": MAC, "type": "organ", "resistance_ohm": -1, "open": True})
    assert len(readings) == 1
    assert math.isinf(readings[0][0])


def test_controller_ignores_organ_from_other_mac():
    controller = ESP32Controller(MAC, MagicMock())
    readings: list = []
    controller.on_organ(lambda ohm, slot: readings.append((ohm, slot)))

    controller._handle_message(
        {"source": "FF:FF:FF:FF:FF:FF", "type": "organ",
         "resistance_ohm": 100.0, "open": False})
    assert readings == []


# ---------------------------------------------------------------------------
# OrganSensor
# ---------------------------------------------------------------------------

class _StubController:
    """Minimal controller exposing the on_organ contract."""

    def __init__(self, mac=MAC):
        self.mac_address = mac
        self._cbs = []

    def on_organ(self, cb):
        self._cbs.append(cb)

    def fire(self, value, slot=0):
        for cb in self._cbs:
            cb(value, slot)


def test_organ_sensor_initial_state_unknown():
    sensor = OrganSensor(_StubController())
    assert sensor.cover_closed is None
    assert math.isinf(sensor.resistance_ohm)


def test_organ_sensor_cover_and_resistance_streams():
    ctrl = _StubController()
    sensor = OrganSensor(ctrl)
    covers: list[bool] = []
    resistances: list[float] = []
    sensor.on_cover(covers.append)
    sensor.on_resistance(resistances.append)

    ctrl.fire(float("inf"))          # first reading: cover off
    ctrl.fire(950.0)                 # cover on, organs read
    ctrl.fire(940.0)                 # value drifts — no cover event
    ctrl.fire(float("inf"))          # cover lifted

    assert covers == [False, True, False]
    assert resistances == [950.0, 940.0]
    assert sensor.cover_closed is False


def test_organ_sensor_filters_by_slot():
    ctrl = _StubController()
    sensor = OrganSensor(ctrl, slot=1)
    resistances: list[float] = []
    sensor.on_resistance(resistances.append)

    ctrl.fire(950.0, slot=0)         # other branch — ignored
    ctrl.fire(1500.0, slot=1)        # ours
    assert resistances == [1500.0]
    assert sensor.slot == 1


def test_organ_sensor_inert_without_on_organ():
    sensor = OrganSensor(object())   # controller without on_organ
    assert sensor.cover_closed is None


# ---------------------------------------------------------------------------
# SimulatedController organ simulation
# ---------------------------------------------------------------------------

def test_simulated_controller_sim_set_organ():
    ctrl = SimulatedController(MAC)
    readings: list[tuple[float, int]] = []
    ctrl.on_organ(lambda ohm, slot: readings.append((ohm, slot)))

    ctrl.sim_set_organ(1500.0)
    ctrl.sim_set_organ(None, slot=1)         # other branch, cover off
    assert readings[0] == (1500.0, 0)
    assert math.isinf(readings[1][0])
    assert readings[1][1] == 1


# ---------------------------------------------------------------------------
# OrganMatcher
# ---------------------------------------------------------------------------

def test_matcher_aggregate_within_tolerance():
    matcher = OrganMatcher("aggregate", target_ohm=952.4, tolerance_ohm=80.0)
    assert matcher.is_cured(950.0)
    assert not matcher.is_cured(700.0)
    assert not matcher.is_cured(float("inf"))


def test_matcher_per_organ_requires_exactly_good_set():
    catalogue = {"liver_good": 1500, "heart_good": 2200, "lung_good": 3300,
                 "liver_bad": 4700, "heart_bad": 5600, "lung_bad": 6800}
    matcher = OrganMatcher("per_organ", target_ohm=0.0, tolerance_ohm=80.0,
                           catalogue=catalogue)
    cured_r = OrganMatcher.parallel_resistance([1500, 2200, 3300])
    assert matcher.is_cured(cured_r)
    wrong_r = OrganMatcher.parallel_resistance([1500, 2200, 6800])
    assert not matcher.is_cured(wrong_r)


def test_matcher_per_organ_falls_back_to_aggregate_without_catalogue():
    matcher = OrganMatcher("per_organ", target_ohm=500.0, tolerance_ohm=50.0,
                           catalogue={})
    assert matcher.is_cured(510.0)


def test_matcher_from_params_uses_organ_swap_defaults():
    from src.activities.organ_swap import OrganSwapActivity
    activity = OrganSwapActivity()
    matcher = OrganMatcher.from_params(activity.param_values)
    assert matcher.is_cured(952.4)


# ---------------------------------------------------------------------------
# OrganSwapActivity state machine + event logging
# ---------------------------------------------------------------------------

class _FakeSkin:
    def __init__(self, skin_id="skin-1", controller=None, organ=None):
        self.skin_id = skin_id
        self.chambers = {0: object(), 1: object()}
        self.pressures: list[tuple[int, int]] = []
        self.touch = None
        self.touch_controller = None
        self._ctrl = controller
        self.organ = organ

    def set_pressure(self, chamber_id, value):
        self.pressures.append((chamber_id, value))

    def inflate(self, chamber_id, delta):
        pass


class _FakeRobot:
    def __init__(self, controller, skins=None):
        self.robot_id = "robot-1"
        if skins is None:
            skins = [_FakeSkin(controller=controller)]
        self.skins = {s.skin_id: s for s in skins}


def _make_activity():
    from src.activities.organ_swap import (
        STATE_CURED, STATE_OPEN, STATE_SICK, OrganSwapActivity,
    )
    from src.core.session import Session

    ctrl = _StubController()
    robot = _FakeRobot(ctrl)
    activity = OrganSwapActivity()
    activity.event_logger = MagicMock()
    activity._setup(Session("S001", activity), [robot])
    return activity, ctrl, robot, (STATE_SICK, STATE_OPEN, STATE_CURED)


def test_organ_swap_transitions_sick_open_cured():
    activity, ctrl, robot, (sick, open_, cured) = _make_activity()
    assert activity.robot_state(robot.robot_id) == sick

    ctrl.fire(float("inf"))          # cover lifted → surgery
    assert activity.robot_state(robot.robot_id) == open_
    # Entering OPEN deflates the patient
    assert (0, 0) in robot.skins["skin-1"].pressures

    ctrl.fire(4700.0)                # cover on, wrong organ
    assert activity.robot_state(robot.robot_id) == sick

    ctrl.fire(float("inf"))          # try again
    assert activity.robot_state(robot.robot_id) == open_

    ctrl.fire(952.4)                 # correct organs + cover on
    assert activity.robot_state(robot.robot_id) == cured


def test_organ_swap_logs_behavioral_events():
    activity, ctrl, robot, _ = _make_activity()
    log = activity.event_logger.log

    ctrl.fire(float("inf"))
    ctrl.fire(952.4)

    calls = [(c.args[1], c.args[2], c.args[3]) for c in log.call_args_list]
    assert ("cover", "open", robot.robot_id) in calls
    assert ("cover", "close", robot.robot_id) in calls
    assert ("organ", "reading", robot.robot_id) in calls
    assert ("activity", "state", robot.robot_id) in calls


def test_organ_swap_force_state_accepts_open():
    activity, _, robot, (_, open_, _) = _make_activity()
    activity.force_state(robot.robot_id, open_)
    assert activity.robot_state(robot.robot_id) == open_


def test_organ_swap_per_branch_patients():
    """A Tree-style robot: two skins each with their own organ circuit on
    distinct slots become two independent patients on one controller."""
    from src.activities.organ_swap import (
        STATE_CURED, STATE_SICK, OrganSwapActivity,
    )
    from src.core.session import Session

    ctrl = _StubController()
    branch_a = _FakeSkin("branch-a", controller=ctrl, organ={"slot": 0})
    branch_b = _FakeSkin("branch-b", controller=ctrl, organ={"slot": 1})
    robot = _FakeRobot(ctrl, skins=[branch_a, branch_b])

    activity = OrganSwapActivity()
    activity.event_logger = MagicMock()
    activity._setup(Session("S001", activity), [robot])

    pid_a = "robot-1/branch-a"
    pid_b = "robot-1/branch-b"
    assert activity.robot_state(pid_a) == STATE_SICK
    assert activity.robot_state(pid_b) == STATE_SICK

    # Curing branch A (slot 0) must not affect branch B (slot 1).
    ctrl.fire(952.4, slot=0)
    assert activity.robot_state(pid_a) == STATE_CURED
    assert activity.robot_state(pid_b) == STATE_SICK

    ctrl.fire(952.4, slot=1)
    assert activity.robot_state(pid_b) == STATE_CURED

"""Tests for the organ + cover sensing chain.

Covers, bottom-up:
- ``ESP32Controller`` dispatch of ``type:"organ"`` messages;
- ``OrganSensor`` splitting raw readings into cover / resistance events;
- ``OrganMatcher`` cure decisions (aggregate + per-organ).
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
    ctrl.fire(940.0)                 # value drifts - no cover event
    ctrl.fire(float("inf"))          # cover lifted

    assert covers == [False, True, False]
    assert resistances == [950.0, 940.0]
    assert sensor.cover_closed is False


def test_organ_sensor_filters_by_slot():
    ctrl = _StubController()
    sensor = OrganSensor(ctrl, slot=1)
    resistances: list[float] = []
    sensor.on_resistance(resistances.append)

    ctrl.fire(950.0, slot=0)         # other branch - ignored
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
    catalogue = {"liver_good": 1500.0, "heart_good": 2200.0, "lung_good": 3300.0,
                 "liver_bad": 4700.0, "heart_bad": 5600.0, "lung_bad": 6800.0}
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

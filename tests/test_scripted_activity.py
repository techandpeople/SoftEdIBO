"""Tests for the declarative behaviour engine.

Covers spec validation, the per-unit state machine (time + touch transitions),
the cooperative sequence scheduler (inflate / wait / deflate), beat modes, and
the declarative-activity DB round-trip. Runs entirely without hardware or a Qt
event loop: ticks are pumped manually and the clock is monkeypatched.
"""

import os
import tempfile
from datetime import datetime

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

    def set_led(self, color, pattern="solid", period_ms=0, **kw):
        self.led = (color, pattern)
        return True

    def set_led_halves(self, colors, **kw):
        self.halves = list(colors)
        return True


class _FakeSkin:
    def __init__(self, skin_id="skin-1", controller=None, n_chambers=3):
        self.skin_id = skin_id
        self.chambers = {i: object() for i in range(n_chambers)}
        self.pressures: list[tuple] = []
        self.touch = None
        self.touch_controller = None
        self._ctrl = controller

    def set_pressure(self, chamber_id, value):
        self.pressures.append((chamber_id, value))
        return True

    def hold(self, chamber_id):
        return True


class _FakeRobot:
    def __init__(self, skins):
        self.robot_id = "robot-1"
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


# ---------------------------------------------------------------------------
# State machine — on enter, time + touch transitions
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


# ---------------------------------------------------------------------------
# Sequence scheduler — inflate / wait / deflate plays out over time
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
    activity._on_tick()                  # wait satisfied → deflate runs
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
    assert unit.state == "sick"                  # no reading yet → not cured

    ctrl.fire_organ(500.0)                        # two 1000Ω good organs ‖ = 500Ω
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

    # One good (1000Ω) ‖ one bad (3000Ω) = 750Ω: a bad organ is still plugged.
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
    ctrl.fire_organ(float("inf"))                 # cover lifted → open circuit
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
    assert "Group Touch" in names                # static ones still present

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

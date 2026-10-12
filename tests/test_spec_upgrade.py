"""Older all-in-one blocks rewritten as composable ones: same behaviour."""

import copy
import json
from pathlib import Path

import pytest

from tests import test_scripted_activity as t
from src.activities import scripted_activity as sa
from src.activities.catalog import lint_spec, validate_spec
from src.activities.scripted_activity import ScriptedActivity
from src.activities.spec_upgrade import OLD_VERBS, needs_upgrade, upgrade_spec


@pytest.fixture
def clock(monkeypatch):
    c = t._Clock()
    monkeypatch.setattr(sa.time, "monotonic", c)
    return c


def _verbs(steps):
    return [next(iter(step)) for step in steps]


def _run(spec, clock, script, ctrl=None, skin=None):
    """Run ``script(activity, unit, clock, t0, observe)`` on a fresh activity
    and return what ``observe()`` recorded after each step."""
    clock.t = 1000.0
    ctrl = ctrl or t._FakePixelCtrl()
    activity = ScriptedActivity("any", "", spec)
    t._start(activity, t._FakeRobot([skin or t._ZoneSkin(ctrl)]))
    unit = t._unit(activity)
    seen = []

    def observe():
        lit = len(t._lit_pixels(ctrl)) if ctrl.pixels else None
        seen.append((activity.unit_state(unit.unit_id), lit))

    script(activity, unit, clock, clock.t, observe)
    return seen


def _same_behaviour(spec, clock, script, **kwargs):
    upgraded = upgrade_spec(spec)
    validate_spec(upgraded)
    assert not needs_upgrade(upgraded)
    old = _run(spec, clock, script, **kwargs)
    new = _run(upgraded, clock, script, **kwargs)
    assert new == old
    return old


def test_score_fill_becomes_detector_score_display_and_a_transition(clock):
    spec = t._score_fill_spec(hold="dim", hold_full_ut=400)
    state = upgrade_spec(spec)["states"]["s"]
    assert _verbs(state["do"]) == ["group_rhythm", "set_value", "when", "when",
                                   "show_value", "hold_feedback"]
    good, missed = state["do"][2]["when"], state["do"][3]["when"]
    assert (good["event"], good["do"][0]["change_value"]["by"]) == ("round_good", 25)
    assert (missed["event"], missed["do"][0]["change_value"]["by"]) == (
        "round_missed", -25)
    assert state["transitions"] == [{"to": "done", "when": {"value_is": {
        "name": "score", "op": "at_least", "value": 100}}}]
    assert "score_fill" not in json.dumps(state)
    assert spec["states"]["s"]["do"][0].get("score_fill")     # input untouched


def test_score_fill_upgrade_behaves_the_same(clock):
    def script(activity, unit, clock, t0, observe):
        observe()
        for at, sensors in ((0, (0, 1, 2)), (100, (0, 1, 2)), (200, (0,)),
                            (300, (0, 1, 2)), (400, (0, 1, 2)),
                            (500, (0, 1, 2)), (600, (0, 1, 2))):
            t._beat(activity, unit, clock, t0, at, sensors=sensors)
            t._settle(activity, unit, clock, t0, at + 70)
            activity._on_tick()        # let handlers and the display catch up
            observe()
        activity._on_tick()
        activity._on_tick()
        observe()

    seen = _same_behaviour(t._score_fill_spec(), clock, script)
    assert [lit for _state, lit in seen[:4]] == [0, 17, 34, 17]
    assert seen[-1][0] == "done"


def test_sync_fill_upgrade_behaves_the_same(clock):
    def script(activity, unit, clock, _t0, observe):
        observe()
        t._emit_group_round(activity, unit, clock)
        activity._on_tick()
        observe()
        clock.advance(0.08)
        t._emit_group_round(activity, unit, clock)
        activity._on_tick()
        observe()
        clock.advance(1.0)             # streak goes stale: decays step by step
        for _ in range(3):
            activity._on_tick()
            observe()
            clock.advance(0.11)

    spec = t._sync_fill_spec(rounds=4, decay_ms=100)
    seen = _same_behaviour(spec, clock, script)
    assert [lit for _state, lit in seen] == [0, 17, 34, 33, 32, 31]
    upgraded = upgrade_spec(spec)["states"]["s"]["do"]
    assert upgraded == [{"show_value": {
        "source": "group_sync", "arcs": 0, "decay_ms": 100,
        "fill": "contiguous", "on_color": "#ffffff", "bg_color": "#000000"}}]


def test_zone_fill_upgrade_behaves_the_same(clock):
    def script(activity, unit, clock, _t0, observe):
        for sensor in (2, 0, 0, 1, 1, 2, 3, 3):
            t._press(activity, unit, sensor)
            activity._on_tick()
            observe()
        activity._on_tick()
        activity._on_tick()
        observe()

    spec = t._zone_fill_spec()
    seen = _same_behaviour(spec, clock, script)
    assert seen[-1][0] == "done"
    state = upgrade_spec(spec)["states"]["s"]
    assert "on_touch" not in state
    assert state["do"][0]["when"]["event"] == "touch"
    assert _verbs(state["do"][0]["when"]["do"]) == ["zone_light"]
    assert state["transitions"] == [{"to": "done", "when": {"zones_full": {}}}]


def test_zone_fill_hold_moves_to_its_own_block(clock):
    state = upgrade_spec(t._zone_fill_spec(hold="glow", hold_full_ut=350))[
        "states"]["s"]
    assert _verbs(state["do"]) == ["when", "hold_feedback"]
    assert state["do"][1]["hold_feedback"]["hold"] == "glow"
    assert state["do"][1]["hold_feedback"]["hold_full_ut"] == 350


def _touch_progress_spec():
    return {"initial": "s", "states": {
        "s": {"do": [{"set_led": {"color": "#111111"}},
                     {"repeat": {"forever": True, "do": [{"wait": {"ms": 500}}]}}],
              "on_touch": [{"touch_progress": {
                  "segments": 4, "per": 2, "on_color": "#00ff00",
                  "bg_color": "#000000", "to": "done"}},
                  {"log": {"message": "touched"}}],
              "transitions": []},
        "done": {"do": [], "transitions": []},
    }}


def test_touch_progress_upgrade_behaves_the_same_on_a_ring(clock):
    """A ring without a pixel strip: the arcs go out as colour lists."""
    def run(spec):
        clock.t = 1000.0
        ctrl = t._FakeCtrl()
        activity = ScriptedActivity("any", "", spec)
        t._start(activity, t._FakeRobot([t._FakeSkin(controller=ctrl)]))
        unit = t._unit(activity)
        seen = [(activity.unit_state(unit.unit_id), ctrl.halves)]
        for _ in range(8):
            t._press(activity, unit, 0)
            activity._on_tick()
            activity._on_tick()
            seen.append((activity.unit_state(unit.unit_id),
                         list(ctrl.halves) if ctrl.halves else None))
        return seen

    spec = _touch_progress_spec()
    upgraded = upgrade_spec(spec)
    validate_spec(upgraded)
    old, new = run(spec), run(upgraded)
    assert new == old
    assert old[0][1] is None                         # nothing painted before a touch
    assert old[2][1] == ["#00ff00"] + ["#000000"] * 3   # two touches: one arc
    assert old[-1][0] == "done"
    # The handler is armed first, before the body's endless loop.
    state = upgraded["states"]["s"]
    assert _verbs(state["do"]) == ["when", "set_led", "repeat"]
    assert _verbs(state["do"][0]["when"]["do"]) == [
        "change_value", "show_value", "log"]


def test_nested_old_blocks_and_new_specs_are_left_alone():
    nested = {"initial": "s", "states": {"s": {"do": [{"repeat": {
        "forever": False, "times": 2,
        "do": [{"sync_fill": {"fill": "random"}}]}}], "transitions": []}}}
    assert not needs_upgrade(nested)
    assert upgrade_spec(nested) == nested
    clean = upgrade_spec(t._score_fill_spec())
    assert upgrade_spec(clean) == clean


def test_upgrade_does_not_mutate_its_input():
    spec = t._zone_fill_spec()
    before = copy.deepcopy(spec)
    upgrade_spec(spec)
    assert spec == before


def test_shipped_example_behaviours_use_only_the_composable_blocks():
    for path in sorted(Path("data").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        spec = payload.get("spec") if isinstance(payload, dict) else None
        if not isinstance(spec, dict) or "states" not in spec:
            continue
        validate_spec(spec)
        assert not needs_upgrade(spec), path.name
        text = json.dumps(spec)
        assert not any(f'"{verb}"' in text for verb in OLD_VERBS), path.name
        assert not [w for w in lint_spec(spec) if "unknown" in w.lower()], path.name

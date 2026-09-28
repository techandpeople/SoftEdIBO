"""Tests for the block editor page (src/gui/blockly/editor.html), run in a
headless Chromium through :mod:`tests.editor_harness`.

Skipped when no Chromium/Chrome is available (set ``SOFTEDIBO_CHROME`` to a
binary to force one).
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

from src.activities import catalog
from src.activities.catalog import editor_catalog, lint_spec, validate_spec
from src.activities.seed_behaviors import SEED_CONDITIONS
from tests.editor_harness import REPO, find_chromium, run_editor_js

pytestmark = pytest.mark.skipif(find_chromium() is None,
                                reason="no headless Chromium available")

PAYLOAD = {"catalog": editor_catalog(), "palette": ["#8e44ad", "#f1c40f"]}

_SCALAR_FIRST_FIELD = {"elapsed_ms": "ms", "touch_count": "min",
                       "on_lifted": "min", "on_impact": "min", "wait": "ms",
                       "robot_is": "robot"}


def _load_and_compile(spec: dict) -> str:
    return (f"w.loadSpec({json.dumps(json.dumps(spec))});"
            " return JSON.parse(w.getSpec());")


def _example_specs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(glob.glob(str(REPO / "config/examples/behaviours/*.json"))):
        data = json.loads(Path(path).read_text())
        out[Path(path).name] = data.get("spec", data)
    for path in ("data/mvp_quadrant_touch.json", "data/cpr_rhythm.json",
                 "data/sync_score.json"):
        full = REPO / path
        if full.is_file():
            data = json.loads(full.read_text())
            out[path] = data.get("spec", data)
    for name, _desc, spec in SEED_CONDITIONS:
        out[name] = spec
    return out


def _drop_editor_omitted(verb: str, params: dict) -> dict:
    """Keys the editor never emits: fields at their omit-default value, and
    fields hidden by a `show_when` whose controller says so."""
    v = catalog.verb(verb)
    if v is None:
        return params
    out = dict(params)
    for f in v.fields:
        if f.name not in out:
            continue
        if f.omit_default and out[f.name] == f.default:
            del out[f.name]
        elif f.show_when is not None:
            ctrl, values = f.show_when
            ctrl_field = v.field_named(ctrl)
            current = out.get(ctrl, ctrl_field.default if ctrl_field else None)
            if current not in values:
                del out[f.name]
    return out


def _canon_cond(cond):
    """Expand scalar shorthands and aliases so hand-written and compiled
    conditions compare equal where they mean the same."""
    if not isinstance(cond, dict):
        return cond
    (name, val), = cond.items()
    name = {"or": "any", "and": "all"}.get(str(name), str(name))
    if name in ("any", "all"):
        return {name: [_canon_cond(c) for c in val]}
    if name == "not":
        return {name: _canon_cond(val)}
    if not isinstance(val, dict):
        val = {_SCALAR_FIRST_FIELD.get(name, "_value"): val}
    return {name: _drop_editor_omitted(name, val)}


def _canon_step(step: dict) -> dict:
    (key, params), = step.items()
    verb = str(key)
    if not isinstance(params, dict):
        params = {_SCALAR_FIRST_FIELD.get(verb, "_value"): params}
    params = dict(params)
    if verb == "wrinkle":
        verb, params = "set_pressure", {**params, "pct": 0}
    for key in ("do", "else"):
        if key in params:
            params[key] = [_canon_step(s) for s in params[key]]
    if verb == "repeat" and params.get("forever"):
        params.pop("times", None)
    # A legacy raw pump `duty` comes back as the nearest 1-5 power level
    # (the editor shows the dial, not the PWM) - mirror that mapping here.
    if "duty" in params:
        duty = params.pop("duty")
        if "power" not in params and duty:
            clamped = max(190, min(255, duty))
            params["power"] = max(1, min(5, 1 + round((clamped - 190) / 65 * 4)))
    return {verb: _drop_editor_omitted(verb, params)}


def _assert_subset(expected, actual, where=""):
    """Every key the hand-written spec carries must come back unchanged;
    the editor may add the remaining defaults."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), where
        for k, v in expected.items():
            assert k in actual, f"{where}.{k} missing from {actual}"
            _assert_subset(v, actual[k], f"{where}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), where
        for i, (e, a) in enumerate(zip(expected, actual)):
            _assert_subset(e, a, f"{where}[{i}]")
    else:
        assert expected == actual, f"{where}: {expected!r} != {actual!r}"


def _assert_round_trip(original: dict, compiled: dict) -> None:
    compiled = dict(compiled)
    compiled.pop("_blockly", None)
    validate_spec(compiled)
    assert compiled["initial"] == original["initial"]
    assert set(compiled["states"]) == set(original["states"])
    for name, state in original["states"].items():
        got = compiled["states"][name]
        for key in ("do", "on_touch"):
            exp = [_canon_step(s) for s in state.get(key, [])]
            act = [_canon_step(s) for s in got.get(key, [])]
            _assert_subset(exp, act, f"{name}.{key}")
        exp_tr = [{"to": t["to"], "when": _canon_cond(t.get("when", {"always": True}))}
                  for t in state.get("transitions", [])]
        act_tr = [{"to": t["to"], "when": _canon_cond(t["when"])}
                  for t in got.get("transitions", [])]
        _assert_subset(exp_tr, act_tr, f"{name}.transitions")
        assert bool(state.get("final")) == bool(got.get("final")), name
    if original.get("target"):
        assert compiled.get("target") == original["target"]


def test_example_behaviours_round_trip_through_the_blocks():
    specs = _example_specs()
    names = list(specs)
    results = run_editor_js([_load_and_compile(specs[n]) for n in names], PAYLOAD)
    for name, result in zip(names, results):
        assert "error" not in result, (name, result)
        _assert_round_trip(specs[name], result)


def test_round_trip_keeps_target_final_flag_units_and_nested_conditions():
    spec = {
        "initial": "sick",
        "target": {"kind": "turtle", "skin": "organs"},
        "states": {
            "sick": {
                "do": [
                    {"set_led_halves": {"colors": ["#111111", "#222222",
                                                   "#333333", "#444444"],
                                        "pattern": "comet", "period_ms": 1500,
                                        "ring": 1}},
                    {"repeat": {"times": 3, "do": [
                        {"inflate": {"chamber": 2, "pct": 40, "power": 2,
                                     "period_ms": 90000}},
                        {"wait": 750},
                    ]}},
                    {"if_robot": {"robot": "tree", "do": [{"stop": {}}],
                                  "else": [{"log": {"message": "hi"}}]}},
                ],
                "on_touch": [{"zone_fill": {"kind": "rhythmic", "step_pct": 10,
                                            "target_interval_ms": 600,
                                            "to": "cured"}}],
                "transitions": [
                    {"to": "cured", "when": {"all": [
                        {"organs": {"scope": "all_good"}},
                        {"not": {"on_lifted": {"min": 2}}},
                        {"any": [{"elapsed_ms": 60000},
                                 {"gesture_count": {"kind": "compressions",
                                                    "min": 4}},
                                 {"touch_rhythm": {"intervals": 7}}]},
                    ]}},
                ],
            },
            "cured": {"final": True, "do": [{"set_led": {"color": "#00ff00"}}]},
        },
    }
    result, = run_editor_js([_load_and_compile(spec)], PAYLOAD)
    assert "error" not in result, result
    _assert_round_trip(spec, result)
    sick = result["states"]["sick"]
    inflate = sick["do"][1]["repeat"]["do"][0]["inflate"]
    assert inflate["period_ms"] == 90000        # 1.5 min survives the unit split
    assert sick["do"][0]["set_led_halves"]["ring"] == 1
    assert result["states"]["cured"]["final"] is True
    assert result["target"] == {"kind": "turtle", "skin": "organs"}
    assert lint_spec({k: v for k, v in result.items() if k != "_blockly"}) == []


def test_saved_workspace_round_trips_and_old_blobs_fall_back_to_the_spec():
    spec = {"initial": "a", "states": {
        "a": {"do": [{"set_led": {"color": "#123456"}}],
              "transitions": [{"to": "b", "when": {"touch_count": 3}}]},
        "b": {"do": [{"deflate": {"chamber": "all"}}]}}}
    legacy = {**spec, "_blockly": {"blocks": {"languageVersion": 0, "blocks": [
        {"type": "se_behaviour", "fields": {"SKIN": "any"}}]}}}
    scripts = [
        _load_and_compile(spec),                       # -> has a format-2 blob
        "return JSON.parse(w.getSpec())._blockly.format;",
        _load_and_compile(legacy),                     # old blob -> rebuilt
    ]
    first, fmt, rebuilt = run_editor_js(scripts, PAYLOAD)
    assert fmt == 2
    _assert_round_trip(spec, first)
    # Reload the editor's own output (with its blob) and compile again.
    second, = run_editor_js([_load_and_compile(first)], PAYLOAD)
    assert {k: v for k, v in second.items() if k != "_blockly"} == \
        {k: v for k, v in first.items() if k != "_blockly"}
    _assert_round_trip(spec, rebuilt)


def test_renaming_a_phase_updates_every_reference_and_warnings_report_duplicates():
    spec = {"initial": "a", "states": {
        "a": {"on_touch": [{"touch_progress": {"to": "b"}}],
              "transitions": [{"to": "b", "when": {"always": True}}]},
        "b": {"do": []}}}
    scripts = [
        _load_and_compile(spec) + "",
        """
        var b = w.workspace.getBlocksByType('se_state', true)
                 .filter(function (s) { return s.getFieldValue('NAME') === 'b'; })[0];
        b.setFieldValue('healed', 'NAME');
        return JSON.parse(w.getSpec());
        """,
        """
        var a = w.workspace.getBlocksByType('se_state', true)[0];
        a.setFieldValue('healed', 'NAME');
        return JSON.parse(w.getWarnings());
        """,
    ]
    _first, renamed, warnings = run_editor_js(scripts, PAYLOAD)
    assert "error" not in renamed, renamed
    assert set(renamed["states"]) == {"a", "healed"}
    assert renamed["states"]["a"]["transitions"][0]["to"] == "healed"
    assert renamed["states"]["a"]["on_touch"][0]["touch_progress"]["to"] == "healed"
    assert any("healed" in wmsg for wmsg in warnings)


def test_new_workspace_compiles_to_one_empty_phase_without_target():
    result, = run_editor_js(["w.newWorkspace(); return JSON.parse(w.getSpec());"],
                            PAYLOAD)
    assert result["initial"] == "phase1"
    assert "target" not in result
    assert result["states"]["phase1"]["do"] == []


def test_conditional_rows_hide_and_drop_their_fields():
    spec = {"initial": "a", "states": {"a": {"do": [
        {"beat": {"mode": "sync", "pct": 50, "pct2": 10, "aligned": 1}},
        {"beat": {"mode": "aligned", "pct": 50, "pct2": 10, "aligned": 1}},
    ]}}}
    result, shapes = run_editor_js([
        _load_and_compile(spec),
        """
        var beats = w.workspace.getAllBlocks(false)
                     .filter(function (b) { return b.type === 'se_beat'; });
        return beats.map(function (b) { return b.getField('pct2').isVisible(); });
        """,
    ], PAYLOAD)
    sync, aligned = result["states"]["a"]["do"]
    assert "pct2" not in sync["beat"] and "aligned" not in sync["beat"]
    assert aligned["beat"]["pct2"] == 10 and aligned["beat"]["aligned"] == 1
    assert shapes == [False, True]

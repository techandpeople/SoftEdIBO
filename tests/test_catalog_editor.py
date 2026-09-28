"""Tests for the catalogue's editor metadata, export, final flag and lint
(src.activities.catalog)."""

import json
import re

import pytest

from src.activities import catalog
from src.activities.catalog import (
    SpecError, editor_catalog, is_final_state, lint_spec, spec_kind,
    spec_target, validate_spec)


# ---------------------------------------------------------------------------
# Editor metadata consistency - every block is generated from this
# ---------------------------------------------------------------------------

def test_every_verb_has_a_template_naming_exactly_its_visible_fields():
    for v in catalog.all_verbs():
        visible = {f.name for f in v.fields if not f.hidden}
        placeholders = set(re.findall(r"{(\w+)}", v.template))
        assert placeholders == visible, (v.name, placeholders ^ visible)
        for variant in v.variants:
            vp = set(re.findall(r"{(\w+)}", variant.template))
            assert vp == visible, (v.name, variant.id, vp ^ visible)


def test_every_field_type_is_one_the_editor_renders():
    for v in catalog.all_verbs():
        for f in v.fields:
            assert f.type in catalog.EDITOR_FIELD_TYPES, (v.name, f.name, f.type)


def test_every_toolbox_verb_has_a_known_category():
    names = {n for n, _ in catalog.CATEGORIES}
    for v in catalog.all_verbs():
        assert v.category in names, v.name


def test_show_when_points_at_a_sibling_field_with_valid_values():
    for v in catalog.all_verbs():
        by_name = {f.name: f for f in v.fields}
        for f in v.fields:
            if f.show_when is None:
                continue
            ctrl, values = f.show_when
            assert ctrl in by_name, (v.name, f.name, ctrl)
            assert by_name[ctrl].choices, (v.name, ctrl)
            for val in values:
                assert val in by_name[ctrl].choices, (v.name, f.name, val)


def test_choice_labels_cover_only_real_choices():
    for v in catalog.all_verbs():
        for f in v.fields:
            for value, _label in f.labels:
                assert value in f.choices, (v.name, f.name, value)


def test_editor_catalog_is_json_serialisable_and_complete():
    data = editor_catalog()
    text = json.dumps(data)
    assert '"verbs"' in text
    names = [v["name"] for v in data["verbs"]]
    assert names == [v.name for v in catalog.all_verbs()]
    beat = next(v for v in data["verbs"] if v["name"] == "beat")
    mode = next(f for f in beat["fields"] if f["name"] == "mode")
    assert {"label": "all chambers together", "value": "sync"} in mode["choices"]
    pct2 = next(f for f in beat["fields"] if f["name"] == "pct2")
    assert pct2["show_when"] == {"field": "mode", "values": ["aligned"]}
    halves = next(v for v in data["verbs"] if v["name"] == "set_led_halves")
    assert halves["variants"][0]["id"] == "quarters"
    assert halves["variants"][0]["count"] == 4
    assert [c["name"] for c in data["categories"]][0] == "Phases"
    assert {r["value"] for r in data["robots"]} == {"thymio", "turtle", "tree"}
    assert {s["value"] for s in data["skins"]} == {"natural", "wrinkles", "organs"}


def test_hidden_duty_stays_out_of_the_editor_but_in_the_schema():
    inflate = catalog.verb("inflate")
    assert inflate is not None
    duty = inflate.field_named("duty")
    assert duty is not None and duty.hidden
    assert "{duty}" not in inflate.template


# ---------------------------------------------------------------------------
# Targets: robot kind and/or skin
# ---------------------------------------------------------------------------

def _spec(**states):
    return {"initial": "a", "states": states or {"a": {"do": []}}}


def test_target_may_carry_kind_and_skin_together():
    spec = {**_spec(), "target": {"kind": "turtle", "skin": "natural"}}
    validate_spec(spec)
    assert spec_target(spec) == {"kind": "turtle", "skin": "natural"}
    assert spec_kind(spec) == "turtle"


def test_kind_target_rejects_other_robots_verbs():
    spec = {"initial": "a", "target": {"kind": "turtle"},
            "states": {"a": {"do": [{"thymio_drive": {"left": 1, "right": 1}}]}}}
    with pytest.raises(SpecError):
        validate_spec(spec)


# ---------------------------------------------------------------------------
# Final flag
# ---------------------------------------------------------------------------

def test_final_flag_is_validated_and_read():
    spec = {"initial": "a", "states": {"a": {"do": []},
                                       "cured": {"final": True, "do": []}}}
    validate_spec(spec)
    assert is_final_state(spec, "cured")
    assert not is_final_state(spec, "a")
    bad = {"initial": "a", "states": {"a": {"final": "yes", "do": []}}}
    with pytest.raises(SpecError):
        validate_spec(bad)


def test_legacy_final_names_still_count():
    spec = {"initial": "a", "states": {"a": {"do": []}, "complete": {"do": []}}}
    assert is_final_state(spec, "complete")
    assert is_final_state(spec, "Done")


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

def test_lint_clean_spec_has_no_warnings():
    spec = {"initial": "sick", "states": {
        "sick": {"do": [{"repeat": {"forever": True, "do": [
                    {"beat": {"mode": "sync"}}]}}],
                 "transitions": [{"to": "cured", "when": {"elapsed_ms": 1000}}]},
        "cured": {"final": True, "do": [{"set_led": {"color": "#00ff00"}}]},
    }}
    validate_spec(spec)
    assert lint_spec(spec) == []


def test_lint_flags_unreachable_phase():
    spec = {"initial": "a", "states": {"a": {"do": []}, "b": {"do": []}}}
    assert any("'b' can never be reached" in w for w in lint_spec(spec))


def test_lint_counts_fill_jumps_as_reachability():
    spec = {"initial": "a", "states": {
        "a": {"on_touch": [{"zone_fill": {"to": "b"}}]},
        "b": {"do": []}}}
    assert lint_spec(spec) == []


def test_lint_flags_fill_jump_to_unknown_phase():
    spec = {"initial": "a", "states": {
        "a": {"on_touch": [{"touch_progress": {"to": "nowhere"}}]}}}
    assert any("'nowhere'" in w for w in lint_spec(spec))


def test_lint_flags_hot_repeat_forever():
    spec = {"initial": "a", "states": {
        "a": {"do": [{"repeat": {"forever": True, "do": [
            {"set_led": {"color": "#ff0000"}}]}}]}}}
    assert any("repeat forever" in w for w in lint_spec(spec))
    ok = {"initial": "a", "states": {
        "a": {"do": [{"repeat": {"forever": True, "do": [
            {"set_led": {"color": "#ff0000"}}, {"wait": 500}]}}]}}}
    assert lint_spec(ok) == []


def test_lint_flags_final_phase_with_transitions_and_shadowed_transitions():
    spec = {"initial": "a", "states": {
        "a": {"transitions": [{"to": "b", "when": {"always": True}},
                              {"to": "c", "when": {"elapsed_ms": 10}}]},
        "b": {"final": True, "transitions": [{"to": "c"}]},
        "c": {"do": []}}}
    warnings = lint_spec(spec)
    assert any("never get a chance" in w for w in warnings)
    assert any("marked final but has transitions" in w for w in warnings)


def test_lint_flags_always_self_transition():
    spec = {"initial": "a", "states": {
        "a": {"transitions": [{"to": "a", "when": {"always": True}}]}}}
    assert any("itself" in w for w in lint_spec(spec))

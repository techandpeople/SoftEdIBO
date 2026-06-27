"""Tests for portable activity import/export (src.activities.activity_io)."""

import json

import pytest

from src.activities.activity_io import (
    ACTIVITY_FILE_FORMAT, ActivityFileError, deserialize_activity,
    serialize_activity)


def _spec() -> dict:
    return {
        "initial": "phase1",
        "states": {
            "phase1": {
                "do": [{"set_led": {"color": "#8e44ad"}}],
                "on_touch": [],
                "transitions": [
                    {"to": "phase2", "when": {"elapsed_ms": {"ms": 1000}}}
                ],
            },
            "phase2": {"do": [{"deflate": {"chamber": "all"}}]},
        },
        # The editor stores its workspace under _blockly; the validator ignores
        # unknown top-level keys, so it must survive the round-trip.
        "_blockly": {"blocks": {"languageVersion": 0, "blocks": []}},
    }


def test_round_trip_preserves_name_description_and_spec():
    text = serialize_activity("My Activity", "A description", _spec())
    name, description, spec = deserialize_activity(text)
    assert name == "My Activity"
    assert description == "A description"
    assert spec == _spec()
    assert spec["_blockly"]            # editor blob round-trips


def test_export_writes_the_wrapped_format():
    data = json.loads(serialize_activity("X", "", _spec()))
    assert data["format"] == ACTIVITY_FILE_FORMAT
    assert data["version"] == 1
    assert data["name"] == "X"
    assert data["spec"]["initial"] == "phase1"


def test_export_requires_a_name():
    with pytest.raises(ActivityFileError):
        serialize_activity("   ", "", _spec())


def test_export_rejects_an_invalid_spec():
    with pytest.raises(Exception):       # SpecError (a ValueError subclass)
        serialize_activity("Bad", "", {"states": {}})


def test_import_accepts_a_bare_spec():
    name, description, spec = deserialize_activity(json.dumps(_spec()))
    assert name == ""                    # no wrapper → no name
    assert spec["initial"] == "phase1"


def test_import_rejects_non_json():
    with pytest.raises(ActivityFileError):
        deserialize_activity("not json {")


def test_import_rejects_unknown_format():
    blob = json.dumps({"format": "something.else", "spec": _spec()})
    with pytest.raises(ActivityFileError):
        deserialize_activity(blob)


def test_import_rejects_an_unrunnable_behaviour():
    blob = json.dumps({"format": ACTIVITY_FILE_FORMAT,
                       "spec": {"initial": "nope", "states": {"a": {}}}})
    with pytest.raises(ActivityFileError):
        deserialize_activity(blob)

"""Tests for the activity-kind domain (robot/skin profile of an activity)."""

from src.activities import activity_kind as ak
from src.hardware.skin_geometry import known_skin_types


def test_every_known_skin_type_maps_to_exactly_one_kind():
    # Guards drift between the activity-kind map and the skin-geometry registry:
    # every configured skin shape must belong to exactly one kind.
    for st in known_skin_types():
        assert ak.kind_for_skin_type(st) is not None, st
    # And no kind lists a shape the registry doesn't know.
    known = set(known_skin_types())
    for kind in ak.KINDS:
        for st in ak.skin_types_for(kind):
            assert st in known, st


def test_kind_for_skin_type_and_shapes():
    assert ak.kind_for_skin_type("tree_round") == ak.TREE
    assert ak.kind_for_skin_type("turtle_square") == ak.TURTLE
    assert ak.kind_for_skin_type("turtle_side") == ak.TURTLE
    assert ak.kind_for_skin_type("thymio") == ak.THYMIO
    assert ak.kind_for_skin_type("nope") is None
    assert ak.skin_types_for(ak.TURTLE) == (
        "turtle_square", "turtle_side", "turtle_triangle")


def test_topology_metadata():
    assert ak.robots_expected(ak.THYMIO) == 3
    assert ak.robots_expected(ak.TURTLE) == 1
    assert ak.robots_expected(ak.TREE) == 1
    # Each child gets their own skin on Thymio/Tree; Turtle shares its chambers.
    assert ak.per_child(ak.THYMIO) is True
    assert ak.per_child(ak.TREE) is True
    assert ak.per_child(ak.TURTLE) is False


def test_robot_type_name_and_label():
    assert ak.robot_type_name(ak.THYMIO) == "ThymioRobot"
    assert ak.robot_type_name(ak.TURTLE) == "TurtleTreeRobot"
    assert ak.robot_type_name(ak.TREE) == "TurtleTreeRobot"
    assert ak.label(ak.TREE) == "Tree"
    assert ak.label("weird") == "weird"


def test_is_kind_and_unknown_defaults():
    assert ak.is_kind(ak.TREE) and not ak.is_kind("nope")
    assert ak.skin_types_for("nope") == ()
    assert ak.robots_expected("nope") == 1
    assert ak.per_child("nope") is False
    assert ak.robot_type_name("nope") is None

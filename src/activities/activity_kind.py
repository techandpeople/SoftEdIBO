"""Activity kind — the robot/skin profile an activity targets.

The study runs three activity kinds, each bound to one robot topology and the
skin shapes it is built from:

  * ``thymio`` — 3 robots (one per child), skin shape ``thymio``; the study is the
    interaction between the children.
  * ``turtle`` — 1 robot, shapes ``turtle_square/side/triangle``, chambers shared
    by the ~3 children; the study is their interaction.
  * ``tree``   — 1 robot, ``tree_round`` branches (one per child); the study is
    cross-touch (a child touching another child's branch).

An activity declares its ``kind`` (plus a skin variant) in its spec ``target``
(see :mod:`src.activities.catalog`). The session setup uses this to derive which
robots/skins are valid, how many are expected, and whether each child maps to their
own skin. Pure data + helpers — Qt-free and with no robot-class imports — so it
unit-tests trivially; callers that need the concrete robot_type CLASS resolve the
kind's :func:`robot_type_name` themselves.
"""

from __future__ import annotations

from typing import Any

THYMIO = "thymio"
TURTLE = "turtle"
TREE = "tree"
KINDS: tuple[str, ...] = (THYMIO, TURTLE, TREE)

# kind -> the skin shapes (skin_type) it is built from. Mirrors the registry in
# src/hardware/skin_geometry.py; test_activity_kind asserts every known skin_type
# is covered by exactly one kind so the two can't drift apart.
_SKIN_TYPES: dict[str, tuple[str, ...]] = {
    THYMIO: ("thymio",),
    TURTLE: ("turtle_square", "turtle_side", "turtle_triangle"),
    TREE: ("tree_round",),
}
# kind -> robots the activity expects (thymio: one per child; turtle/tree: one).
_ROBOTS_EXPECTED: dict[str, int] = {THYMIO: 3, TURTLE: 1, TREE: 1}
# kind -> is each child mapped to their own skin? Turtle shares its chambers, so no.
_PER_CHILD: dict[str, bool] = {THYMIO: True, TURTLE: False, TREE: True}
# kind -> robot class NAME. Kept as a string so this module imports no robot/Qt
# code; callers that filter live robots resolve the class themselves.
_ROBOT_TYPE_NAME: dict[str, str] = {
    THYMIO: "ThymioRobot", TURTLE: "TurtleTreeRobot", TREE: "TurtleTreeRobot"}
# kind -> human label for pickers.
_LABELS: dict[str, str] = {THYMIO: "Thymio", TURTLE: "Turtle", TREE: "Tree"}


def is_kind(kind: Any) -> bool:
    """Whether ``kind`` is a known activity kind."""
    return kind in KINDS


def skin_types_for(kind: Any) -> tuple[str, ...]:
    """The skin shapes a kind is built from (empty for an unknown kind)."""
    return _SKIN_TYPES.get(kind, ())


def kind_for_skin_type(skin_type: Any) -> str | None:
    """The activity kind a skin shape belongs to, or ``None`` if unknown."""
    for k, types in _SKIN_TYPES.items():
        if skin_type in types:
            return k
    return None


def robots_expected(kind: Any) -> int:
    """How many robots the kind's activity runs on (3 Thymios, else 1)."""
    return _ROBOTS_EXPECTED.get(kind, 1)


def per_child(kind: Any) -> bool:
    """Whether each child maps to their own skin (Turtle shares its chambers)."""
    return _PER_CHILD.get(kind, False)


def robot_type_name(kind: Any) -> str | None:
    """The robot class name for a kind (``"ThymioRobot"`` / ``"TurtleTreeRobot"``)."""
    return _ROBOT_TYPE_NAME.get(kind)


def label(kind: Any) -> str:
    """Human label for a kind (the raw value if unknown)."""
    return _LABELS.get(kind, str(kind or ""))

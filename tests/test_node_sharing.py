"""Tests for a board shared by two robots (Turtle and Tree on one PCB).

Covers the three seams that make the shared board safe: one controller per MAC
(:class:`~src.hardware.node_registry.NodeRegistry`), the mounted robot restating
its board state (``claim_board``), and the config/live sharing helpers in
:mod:`src.core.node_sharing`.
"""

from unittest.mock import MagicMock, PropertyMock

from src.core import node_sharing
from src.hardware.node_registry import NodeRegistry
from src.robots.tree.tree_robot import TreeRobot
from src.robots.turtle.turtle_robot import TurtleRobot

SHARED_MAC = "AA:BB:CC:DD:EE:01"

SETTINGS = {
    "robots": {
        "turtles": [{
            "id": "turtle-1",
            "nodes": [{"mac": SHARED_MAC, "node_type": "node_multiplexed",
                       "max_slots": 4}],
            "skins": [{"skin_id": "shell", "skin_type": "turtle_square",
                       "chambers": [{"mac": SHARED_MAC, "slot": 0}]}],
        }],
        "trees": [{
            "id": "tree-1",
            "nodes": [{"mac": SHARED_MAC, "node_type": "node_multiplexed",
                       "max_slots": 12}],
            "skins": [{"skin_id": "branch-0", "skin_type": "tree_round",
                       "chambers": [{"mac": SHARED_MAC, "slot": 0}]}],
        }],
        "thymios": [{
            "thymio_id": "thymio-1",
            "nodes": [{"mac": "AA:BB:CC:DD:EE:99", "node_type": "node_direct"}],
        }],
    }
}


def _gateway():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True
    return gateway


def _robots():
    """A Turtle and a Tree built on the same MAC, as configured above."""
    gateway = _gateway()
    registry = NodeRegistry(gateway)
    turtle_cfg = SETTINGS["robots"]["turtles"][0]
    tree_cfg = SETTINGS["robots"]["trees"][0]
    turtle = TurtleRobot("turtle-1", gateway, turtle_cfg["nodes"],
                         turtle_cfg["skins"], registry=registry)
    tree = TreeRobot("tree-1", gateway, tree_cfg["nodes"],
                     tree_cfg["skins"], registry=registry)
    return turtle, tree, gateway


# ---------------------------------------------------------------------------
# Config-side helpers
# ---------------------------------------------------------------------------

def test_robot_ids_by_mac_lists_both_robots():
    by_mac = node_sharing.robot_ids_by_mac(SETTINGS)
    assert by_mac[SHARED_MAC] == ["turtle-1", "tree-1"]
    assert by_mac["AA:BB:CC:DD:EE:99"] == ["thymio-1"]


def test_shared_macs_only_returns_the_shared_board():
    assert node_sharing.shared_macs(SETTINGS) == {
        SHARED_MAC: ["turtle-1", "tree-1"]}


def test_macs_of_is_per_robot():
    """The add-node picker must offer a board another robot already uses."""
    assert node_sharing.macs_of(SETTINGS, "turtles", 0) == {SHARED_MAC}
    assert node_sharing.macs_of(SETTINGS, "turtles", 5) == set()


# ---------------------------------------------------------------------------
# Live-robot helpers
# ---------------------------------------------------------------------------

def test_conflicts_flags_two_robots_on_one_board():
    turtle, tree, _ = _robots()
    assert node_sharing.conflicts([turtle, tree]) == {
        SHARED_MAC: ["turtle-1", "tree-1"]}
    assert "only one of them can be mounted" in node_sharing.conflict_message(
        node_sharing.conflicts([turtle, tree]))


def test_no_conflict_for_a_single_robot():
    turtle, _tree, _ = _robots()
    assert node_sharing.conflicts([turtle]) == {}
    assert node_sharing.conflict_message({}) == ""


# ---------------------------------------------------------------------------
# One controller per board
# ---------------------------------------------------------------------------

def test_registry_hands_out_one_controller_per_mac():
    registry = NodeRegistry(_gateway())
    assert registry.controller(SHARED_MAC) is registry.controller(SHARED_MAC)


def test_sharing_robots_drive_the_same_controller():
    turtle, tree, _ = _robots()
    assert turtle.node_macs == tree.node_macs == [SHARED_MAC]
    assert turtle.controller_for(SHARED_MAC) is tree.controller_for(SHARED_MAC)


def test_without_a_registry_each_robot_keeps_its_own_controller():
    """Robots built standalone (tests, sim) are unaffected by the seam."""
    gateway = _gateway()
    turtle = TurtleRobot("turtle-1", gateway,
                         SETTINGS["robots"]["turtles"][0]["nodes"],
                         SETTINGS["robots"]["turtles"][0]["skins"])
    tree = TreeRobot("tree-1", gateway,
                     SETTINGS["robots"]["trees"][0]["nodes"],
                     SETTINGS["robots"]["trees"][0]["skins"])
    assert turtle.controller_for(SHARED_MAC) is not tree.controller_for(SHARED_MAC)


# ---------------------------------------------------------------------------
# Claiming the board
# ---------------------------------------------------------------------------

def _configure_calls(gateway):
    return [c for c in gateway.send.call_args_list
            if len(c.args) > 1 and c.args[1] == "configure"]


def test_claim_board_restates_this_robots_chamber_count():
    turtle, tree, gateway = _robots()
    # Both robots configured the shared node at build; the Tree spoke last.
    assert _configure_calls(gateway)[-1].kwargs["num_chambers"] == 12
    turtle.claim_board()
    assert _configure_calls(gateway)[-1].kwargs["num_chambers"] == 4
    tree.claim_board()
    assert _configure_calls(gateway)[-1].kwargs["num_chambers"] == 12


def test_claim_board_pushes_this_robots_led_angles():
    gateway = _gateway()
    registry = NodeRegistry(gateway)
    turtle = TurtleRobot(
        "turtle-1", gateway, SETTINGS["robots"]["turtles"][0]["nodes"],
        [{"skin_id": "shell", "led_angles": {0: 90.0},
          "chambers": [{"mac": SHARED_MAC, "slot": 0}]}], registry=registry)
    tree = TreeRobot(
        "tree-1", gateway, SETTINGS["robots"]["trees"][0]["nodes"],
        [{"skin_id": "branch-0", "led_angles": {0: 180.0},
          "chambers": [{"mac": SHARED_MAC, "slot": 0}]}], registry=registry)
    ctrl = turtle.controller_for(SHARED_MAC)
    assert ctrl is not None
    assert ctrl.led_angles == {0: 180.0}          # Tree built last
    turtle.claim_board()
    assert ctrl.led_angles == {0: 90.0}           # Turtle is mounted now
    tree.claim_board()
    assert ctrl.led_angles == {0: 180.0}

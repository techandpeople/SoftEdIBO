"""Tests for the merged Turtle & Tree robot module.

Turtle and Tree are one robot kind: the same nodes, only the skins differ.
These tests cover both shapes of deployment — a Turtle-style robot with several
skins spread across nodes, and a Tree-style robot whose per-branch skins carry
owner / sharing bookkeeping.
"""

from unittest.mock import MagicMock, PropertyMock

from src.robots.turtle_tree.turtle_tree_robot import TurtleTreeRobot


def _make_gateway():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True
    return gateway


def _make_turtle():
    """Turtle-style: several skins across two nodes."""
    gateway = _make_gateway()
    node_configs = [
        {"mac": "AA:BB:CC:DD:EE:01", "node_type": "node_direct", "max_slots": 3},
        {"mac": "AA:BB:CC:DD:EE:02", "node_type": "node_direct", "max_slots": 3},
    ]
    skin_configs = [
        {"skin_id": "skin_full", "name": "Full", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:01", "slot": 0, "max_pressure": 8.0},
            {"mac": "AA:BB:CC:DD:EE:01", "slot": 1, "max_pressure": 8.0},
            {"mac": "AA:BB:CC:DD:EE:01", "slot": 2, "max_pressure": 8.0},
        ]},
        {"skin_id": "skin_small_a", "name": "Small A", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:02", "slot": 0, "max_pressure": 8.0},
        ]},
        {"skin_id": "skin_small_b", "name": "Small B", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:02", "slot": 1, "max_pressure": 8.0},
            {"mac": "AA:BB:CC:DD:EE:02", "slot": 2, "max_pressure": 8.0},
        ]},
    ]
    robot = TurtleTreeRobot("turtle-1", gateway, node_configs, skin_configs)
    return robot, gateway


def _make_tree():
    """Tree-style: one node, one skin (branch) per slot."""
    gateway = _make_gateway()
    node_configs = [
        {"mac": "AA:BB:CC:DD:EE:20", "node_type": "node_direct", "max_slots": 3},
    ]
    skin_configs = [
        {"skin_id": "branch-0", "name": "Branch 0", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:20", "slot": 0, "max_pressure": 8.0}]},
        {"skin_id": "branch-1", "name": "Branch 1", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:20", "slot": 1, "max_pressure": 8.0}]},
        {"skin_id": "branch-2", "name": "Branch 2", "chambers": [
            {"mac": "AA:BB:CC:DD:EE:20", "slot": 2, "max_pressure": 8.0}]},
    ]
    robot = TurtleTreeRobot(
        robot_id="tree-1",
        gateway=gateway,
        node_configs=node_configs,
        skin_configs=skin_configs,
    )
    return robot, gateway


# --- Turtle-style skin layout ---------------------------------------------

def test_has_correct_skins():
    robot, _ = _make_turtle()
    assert len(robot.skins) == 3
    assert "skin_full" in robot.skins
    assert "skin_small_a" in robot.skins
    assert "skin_small_b" in robot.skins


def test_total_chambers():
    robot, _ = _make_turtle()
    # 3 + 1 + 2 = 6 chambers total
    assert robot.total_chambers == 6


def test_connect():
    robot, _ = _make_turtle()
    assert robot.connect() is True
    assert robot.status.value == "connected"


def test_status_data():
    robot, _ = _make_turtle()
    robot.connect()
    data = robot.get_status_data()
    assert data["robot_id"] == "turtle-1"
    assert len(data["skins"]) == 3


def test_small_skins_share_node():
    robot, _ = _make_turtle()
    skin_a = robot.skins["skin_small_a"]
    skin_b = robot.skins["skin_small_b"]
    # Both skins live on the same node (single-node-per-skin invariant)…
    assert skin_a.node_macs == skin_b.node_macs == ["AA:BB:CC:DD:EE:02"]
    # …but cover a different number of chambers.
    assert skin_a.chamber_count == 1
    assert skin_b.chamber_count == 2


# --- Tree-style ownership / sharing ---------------------------------------

def test_tree_has_correct_branches():
    robot, _ = _make_tree()
    assert len(robot.skins) == 3


def test_branch_assignment():
    robot, _ = _make_tree()
    robot.assign_to("branch-0", "p-001")
    assert robot.get_owner("branch-0") == "p-001"


def test_branch_sharing():
    robot, _ = _make_tree()
    robot.assign_to("branch-1", "p-001")
    robot.share_with("branch-1", "p-002")
    assert robot.get_owner("branch-1") == "p-001"
    assert "p-002" in robot.get_shared("branch-1")


def test_status_data_includes_owners():
    robot, _ = _make_tree()
    robot.assign_to("branch-2", "p-003")
    data = robot.get_status_data()
    assert data["owners"]["branch-2"] == "p-003"

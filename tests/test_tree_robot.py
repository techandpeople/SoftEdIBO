"""Tests for the Tree robot - one skin (branch) per child, with sharing."""

from unittest.mock import MagicMock, PropertyMock

from src.robots.tree.tree_robot import TreeRobot


def _make_gateway():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True
    return gateway


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
    robot = TreeRobot(
        robot_id="tree-1",
        gateway=gateway,
        node_configs=node_configs,
        skin_configs=skin_configs,
    )
    return robot, gateway


def test_robot_kind():
    robot, _ = _make_tree()
    assert robot.robot_kind == "tree"


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


def test_unshare():
    robot, _ = _make_tree()
    robot.assign_to("branch-1", "p-001")
    robot.share_with("branch-1", "p-002")
    robot.unshare("branch-1", "p-002")
    assert robot.get_shared("branch-1") == []


def test_status_data_includes_owners():
    robot, _ = _make_tree()
    robot.assign_to("branch-2", "p-003")
    data = robot.get_status_data()
    assert data["owners"]["branch-2"] == "p-003"

"""Tests for the Tree robot module."""

from unittest.mock import MagicMock, PropertyMock

from src.robots.tree.tree_robot import TreeRobot


def _make_tree():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True

    tree = TreeRobot(
        robot_id="tree-1",
        gateway=gateway,
        esp32_mac="AA:BB:CC:DD:EE:20",
        branch_ids=[0, 1, 2],
    )
    return tree, gateway


def test_tree_has_correct_branches():
    tree, _ = _make_tree()
    assert len(tree.branches) == 3


def test_tree_connect():
    tree, _ = _make_tree()
    assert tree.connect() is True
    assert tree.status.value == "connected"


def test_branch_assignment():
    tree, _ = _make_tree()
    branch = tree.branches[0]
    branch.assign_to("p-001")
    assert branch.owner == "p-001"


def test_branch_sharing():
    tree, _ = _make_tree()
    branch = tree.branches[1]
    branch.assign_to("p-001")
    branch.share_with("p-002")
    status = branch.get_status()
    assert status["owner"] == "p-001"
    assert "p-002" in status["shared_with"]

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
        skin_configs=[
            {"skin_id": "branch-0", "mac": "AA:BB:CC:DD:EE:20", "slots": [0]},
            {"skin_id": "branch-1", "mac": "AA:BB:CC:DD:EE:20", "slots": [1]},
            {"skin_id": "branch-2", "mac": "AA:BB:CC:DD:EE:20", "slots": [2]},
        ],
    )
    return tree, gateway


def test_tree_has_correct_branches():
    tree, _ = _make_tree()
    assert len(tree.skins) == 3


def test_tree_connect():
    tree, _ = _make_tree()
    assert tree.connect() is True
    assert tree.status.value == "connected"


def test_branch_assignment():
    tree, _ = _make_tree()
    tree.assign_to("branch-0", "p-001")
    assert tree.get_owner("branch-0") == "p-001"


def test_branch_sharing():
    tree, _ = _make_tree()
    tree.assign_to("branch-1", "p-001")
    tree.share_with("branch-1", "p-002")
    assert tree.get_owner("branch-1") == "p-001"
    assert "p-002" in tree.get_shared("branch-1")

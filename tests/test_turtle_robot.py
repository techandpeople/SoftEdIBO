"""Tests for the Turtle robot — several shared skins spread across nodes."""

from unittest.mock import MagicMock, PropertyMock

from src.robots.turtle.turtle_robot import TurtleRobot


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
    robot = TurtleRobot("turtle-1", gateway, node_configs, skin_configs)
    return robot, gateway


def test_robot_kind():
    robot, _ = _make_turtle()
    assert robot.robot_kind == "turtle"


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


def test_nodes_seen_since_uses_gateway_last_seen():
    robot, gateway = _make_turtle()
    seen = {"AA:BB:CC:DD:EE:01": 100.0}          # node 2 never answered
    gateway.node_last_seen.side_effect = seen.get
    assert robot.nodes_seen_since(50.0) == (1, 2)
    assert robot.nodes_seen_since(200.0) == (0, 2)

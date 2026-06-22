"""Tests for the Turtle robot module."""

from unittest.mock import MagicMock, PropertyMock

from src.robots.turtle.turtle_robot import TurtleRobot


def _make_turtle():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True

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
    turtle = TurtleRobot("turtle-1", gateway, node_configs, skin_configs)
    return turtle, gateway


def test_turtle_has_correct_skins():
    turtle, _ = _make_turtle()
    assert len(turtle.skins) == 3
    assert "skin_full" in turtle.skins
    assert "skin_small_a" in turtle.skins
    assert "skin_small_b" in turtle.skins


def test_turtle_total_chambers():
    turtle, _ = _make_turtle()
    # 3 + 1 + 2 = 6 chambers total
    assert turtle.total_chambers == 6


def test_turtle_connect():
    turtle, _ = _make_turtle()
    assert turtle.connect() is True
    assert turtle.status.value == "connected"


def test_turtle_status_data():
    turtle, _ = _make_turtle()
    turtle.connect()
    data = turtle.get_status_data()
    assert data["robot_id"] == "turtle-1"
    assert len(data["skins"]) == 3


def test_small_skins_share_node():
    turtle, _ = _make_turtle()
    skin_a = turtle.skins["skin_small_a"]
    skin_b = turtle.skins["skin_small_b"]
    # Both skins live on the same node (single-node-per-skin invariant)…
    assert skin_a.node_macs == skin_b.node_macs == ["AA:BB:CC:DD:EE:02"]
    # …but cover a different number of chambers.
    assert skin_a.chamber_count == 1
    assert skin_b.chamber_count == 2

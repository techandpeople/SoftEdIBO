"""Tests for the Turtle robot module."""

from unittest.mock import MagicMock, PropertyMock

from src.robots.turtle.turtle_robot import TurtleRobot


def _make_turtle():
    gateway = MagicMock()
    type(gateway).is_connected = PropertyMock(return_value=True)
    gateway.send.return_value = True

    skin_configs = [
        {"skin_id": "skin_full", "mac": "AA:BB:CC:DD:EE:01", "slots": [0, 1, 2]},
        {"skin_id": "skin_small_a", "mac": "AA:BB:CC:DD:EE:02", "slots": [0]},
        {"skin_id": "skin_small_b", "mac": "AA:BB:CC:DD:EE:02", "slots": [1, 2]},
    ]
    turtle = TurtleRobot("turtle-1", gateway, skin_configs)
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


def test_small_skins_share_esp32():
    turtle, _ = _make_turtle()
    skin_a = turtle.skins["skin_small_a"]
    skin_b = turtle.skins["skin_small_b"]
    # Both skins share the same ESP32
    assert skin_a.esp32_mac == skin_b.esp32_mac == "AA:BB:CC:DD:EE:02"
    # But use different slots
    assert list(skin_a.chambers.keys()) == [0]
    assert list(skin_b.chambers.keys()) == [1, 2]

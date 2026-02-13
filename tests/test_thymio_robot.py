"""Tests for the Thymio robot module."""

from src.robots.thymio.thymio_robot import ThymioRobot


def test_thymio_initial_state():
    thymio = ThymioRobot("thymio-1")
    assert thymio.status.value == "disconnected"
    assert thymio.robot_id == "thymio-1"


def test_thymio_connect():
    thymio = ThymioRobot("thymio-1")
    assert thymio.connect() is True
    assert thymio.status.value == "connected"


def test_thymio_disconnect():
    thymio = ThymioRobot("thymio-1")
    thymio.connect()
    thymio.disconnect()
    assert thymio.status.value == "disconnected"

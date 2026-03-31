"""Tests for the ESP32 controller module."""

from unittest.mock import MagicMock

from src.hardware.esp32_controller import ESP32Controller


def test_inflate_sends_command():
    gateway = MagicMock()
    gateway.send.return_value = True
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    result = controller.inflate(chamber=0, delta=20)
    assert result is True
    gateway.send.assert_called_once_with(
        "AA:BB:CC:DD:EE:01", "inflate", chamber=0, delta=20
    )


def test_deflate_sends_command():
    gateway = MagicMock()
    gateway.send.return_value = True
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    result = controller.deflate(chamber=2)
    assert result is True
    gateway.send.assert_called_once_with(
        "AA:BB:CC:DD:EE:01", "deflate", chamber=2, delta=10
    )


def test_handle_message_filters_by_mac():
    gateway = MagicMock()
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    # Message from another node should be ignored
    controller._handle_message({"source": "AA:BB:CC:DD:EE:02", "pressure": 100})
    assert controller.get_last_status() == {}

    # Message from this node should be stored
    controller._handle_message({"source": "AA:BB:CC:DD:EE:01", "pressure": 150})
    assert controller.get_last_status()["pressure"] == 150

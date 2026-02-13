"""Tests for the ESP-NOW gateway module."""

from unittest.mock import MagicMock, patch

from src.hardware.espnow_gateway import ESPNowGateway


def test_gateway_not_connected_by_default():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    assert not gateway.is_connected


def test_send_fails_when_not_connected():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    result = gateway.send("AA:BB:CC:DD:EE:01", "inflate", chamber=0, value=255)
    assert result is False


def test_on_message_registers_callback():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    callback = MagicMock()
    gateway.on_message(callback)
    assert callback in gateway._callbacks

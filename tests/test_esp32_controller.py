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


def test_inflate_sends_duty_when_given():
    gateway = MagicMock()
    gateway.send.return_value = True
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    controller.inflate(chamber=0, delta=20, duty=120)
    gateway.send.assert_called_once_with(
        "AA:BB:CC:DD:EE:01", "inflate", chamber=0, delta=20, duty=120
    )


def test_inflate_clamps_duty_and_omits_when_none():
    gateway = MagicMock()
    gateway.send.return_value = True
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    controller.inflate(chamber=0, delta=20, duty=999)
    _, kwargs = gateway.send.call_args
    assert kwargs["duty"] == 255            # clamped to the 8-bit ceiling

    gateway.send.reset_mock()
    controller.inflate(chamber=0, delta=20)
    _, kwargs = gateway.send.call_args
    assert "duty" not in kwargs             # omitted when unset


def test_deflate_and_set_pressure_send_duty():
    gateway = MagicMock()
    gateway.send.return_value = True
    controller = ESP32Controller("AA:BB:CC:DD:EE:01", gateway)

    controller.deflate(chamber=2, duty=90)
    gateway.send.assert_called_with(
        "AA:BB:CC:DD:EE:01", "deflate", chamber=2, delta=10, duty=90
    )
    controller.set_pressure(chamber=1, value=50, duty=70)
    gateway.send.assert_called_with(
        "AA:BB:CC:DD:EE:01", "set_pressure", chamber=1, value=50, duty=70
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

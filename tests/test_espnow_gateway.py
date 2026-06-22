"""Tests for the ESP-NOW gateway module."""

from src.hardware.espnow_gateway import ESPNowGateway


def test_gateway_not_connected_by_default():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    assert not gateway.is_connected


def test_send_fails_when_not_connected():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    result = gateway.send("AA:BB:CC:DD:EE:01", "inflate", chamber=0, value=255)
    assert result is False


class _Sink:
    def cb(self, data):  # bound method — on_message stores a weakref.WeakMethod
        pass


def test_on_message_registers_and_removes_callback():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    sink = _Sink()
    gateway.on_message(sink.cb)
    # Callbacks are held as weakrefs; resolve them to compare.
    assert any(wr() == sink.cb for wr in gateway._callbacks)

    gateway.remove_message_callback(sink.cb)
    assert all(wr() != sink.cb for wr in gateway._callbacks)

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


# --- handshake: a peer is only accepted if it identifies as a gateway -------

def test_gateway_lines_are_recognized():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    accepted = [
        b'{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}',
        b'{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00","ap":"SoftEdIBO"}',
        b'{"type":"ap_config","ssid":"SoftEdIBO","channel":1,"secured":true}',
        b'{"type":"ap_set","ok":true,"ssid":"SoftEdIBO"}',
        b'{"type":"error","reason":"ap_not_supported"}',
        # Only a gateway tags forwarded node messages with "source".
        b'{"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":50}',
    ]
    for line in accepted:
        assert gateway._is_gateway_line(line), line


def test_non_gateway_lines_are_rejected():
    gateway = ESPNowGateway("/dev/ttyUSB0")
    rejected = [
        b"",
        b"   ",
        b"rst:0x1 (POWERON_RESET),boot:0x13",   # ESP boot log noise
        b"not json at all",
        b'{"type":"status","chamber":0,"pressure":50}',  # node reply, no "source"
        b'{"status":"node_direct_ready"}',               # node boot announce
        b"[1,2,3]",                                       # valid JSON, not a dict
    ]
    for line in rejected:
        assert not gateway._is_gateway_line(line), line

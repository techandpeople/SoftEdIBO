"""Tests for reading a Thymio's RF address off its USB cable (no hardware)."""

import sys
import types

import pytest

from src.robots.thymio import thymio_cable
from src.robots.thymio.thymio_cable import (
    ThymioCableError,
    address_from_node_id,
    find_cabled_thymio_port,
    read_cabled_thymio_address,
)


# --- node id -> address -----------------------------------------------------

def test_address_is_node_id_bytes_swapped():
    assert address_from_node_id(0x256A) == "6a25"     # the live-confirmed pair
    assert address_from_node_id(0x7B31) == "317b"
    assert address_from_node_id(0x0102) == "0201"


# --- port detection --------------------------------------------------------

class _Port:
    def __init__(self, device, vid, pid, product=""):
        self.device, self.vid, self.pid, self.product = device, vid, pid, product


def _fake_comports(monkeypatch, ports):
    import serial.tools.list_ports as lp
    monkeypatch.setattr(lp, "comports", lambda: ports)


def test_find_port_picks_robot_ignores_dongle(monkeypatch):
    ports = [
        _Port("/dev/ttyACM0", 0x303A, 0x1001, "C6"),          # gateway - ignore
        _Port("/dev/ttyACM1", 0x0617, 0x000C, "Wireless"),    # dongle - ignore
        _Port("/dev/ttyACM2", 0x0617, 0x000A, "Thymio-II"),   # the robot
    ]
    _fake_comports(monkeypatch, ports)
    assert find_cabled_thymio_port() == "/dev/ttyACM2"


def test_find_port_none_when_only_dongle(monkeypatch):
    _fake_comports(monkeypatch, [_Port("/dev/ttyACM1", 0x0617, 0x000C, "Wireless")])
    assert find_cabled_thymio_port() is None


# --- end-to-end read over a fake thymiodirect ------------------------------

class _FakeConn:
    def __init__(self):
        self.shut = self.closed = False

    def shutdown(self):
        self.shut = True

    def close(self):
        self.closed = True


class _FakeThymio:
    """Stand-in for thymiodirect.Thymio: connect() reports one node id."""

    instances: list = []
    node_id = 0x256A

    def __init__(self, serial_port, on_connect=None):
        self.serial_port = serial_port
        self._on_connect = on_connect
        self.thymio_proxy = types.SimpleNamespace(connection=_FakeConn())
        type(self).instances.append(self)

    def connect(self):
        if self._on_connect:
            self._on_connect(self.node_id)


def _fake_thymiodirect(monkeypatch, cls=_FakeThymio):
    cls.instances = []
    monkeypatch.setitem(sys.modules, "thymiodirect",
                        types.SimpleNamespace(Thymio=cls))


def test_read_returns_address_and_shuts_down(monkeypatch):
    _fake_thymiodirect(monkeypatch)
    assert read_cabled_thymio_address(port="/dev/ttyACM2") == "6a25"
    conn = _FakeThymio.instances[0].thymio_proxy.connection
    assert conn.shut and conn.closed          # port + reader thread released


def test_read_no_port_raises(monkeypatch):
    monkeypatch.setattr(thymio_cable, "find_cabled_thymio_port", lambda: None)
    with pytest.raises(ThymioCableError, match="No Thymio found on USB"):
        read_cabled_thymio_address()


def test_read_times_out_when_silent(monkeypatch):
    class _Silent(_FakeThymio):
        def connect(self):
            pass                               # never reports a node
    _fake_thymiodirect(monkeypatch, _Silent)
    with pytest.raises(ThymioCableError, match="didn't answer"):
        read_cabled_thymio_address(port="/dev/ttyACM2", timeout=0.2)
    # still tore the connection down
    assert _Silent.instances[0].thymio_proxy.connection.shut

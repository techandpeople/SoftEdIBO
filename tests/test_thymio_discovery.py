"""Tests for Thymio address discovery (no hardware)."""

import json

from src.robots.thymio.thymio_discovery import parse_thymio_addr, discover_thymios


# --- frame parsing ---------------------------------------------------------

def test_parse_host_to_thymio_frame():
    # FCF|seq|PAN 8144|dst 256a(=0x6a25)|src 3732(=host)|…  → the robot is the dst
    assert parse_thymio_addr("6188388144256a373283006a25") == 0x6A25


def test_parse_thymio_to_host_frame():
    # dst 3732(=host)|src 256a(=0x6a25) → the robot is the src
    assert parse_thymio_addr("61888081443732256a8300") == 0x6A25


def test_parse_second_thymio_address():
    # dst 317b = 0x7b31 (a different robot)
    assert parse_thymio_addr("6188018144317b373283ff") == 0x7B31


def test_parse_ignores_ack_and_other_pans():
    assert parse_thymio_addr("020025c80a") is None          # 5-byte ACK — no addresses
    assert parse_thymio_addr("6188018144c2f4") is None       # too short after PAN
    assert parse_thymio_addr("618801f4c2256a373283ff") is None  # PAN 0xf4c2, not the Thymio net
    assert parse_thymio_addr("nothex") is None


# --- end-to-end discovery over a fake sniffing gateway ---------------------

class _FakeSniffGateway:
    """Feeds canned sniff frames to the raw callback when sniff_start is sent."""

    def __init__(self, frames, connected=True):
        self.is_connected = connected
        self._frames = frames
        self._raw_cbs: list = []
        self.sent: list = []

    def on_raw(self, cb):
        self._raw_cbs.append(cb)

    def remove_raw_callback(self, cb):
        if cb in self._raw_cbs:
            self._raw_cbs.remove(cb)

    def send(self, target, cmd, **kwargs):
        self.sent.append((target, cmd, kwargs))
        if cmd == "sniff_start":
            for data in self._frames:
                line = json.dumps({"type": "frame", "ch": 25, "data": data,
                                   "source": "thymio"})
                for cb in list(self._raw_cbs):
                    cb("rx", line)
        return True


def test_discover_returns_distinct_addresses():
    frames = [
        "6188388144256a373283006a25",   # host -> Thymio 6a25
        "61888081443732256a8300",       # Thymio 6a25 -> host (dedupe)
        "6188018144317b373283ff",       # host -> Thymio 7b31
        "020025c80a",                   # ACK — ignored
    ]
    gw = _FakeSniffGateway(frames)
    addrs = discover_thymios(gw, channel=25, secs=0.01)
    assert addrs == ["6a25", "7b31"]                     # first-seen order, deduped, hex
    # it drove the C6 sniffer and put it back
    assert ("thymio", "sniff_start", {"ch": 25}) in gw.sent
    assert ("thymio", "sniff_stop", {}) in gw.sent
    assert gw._raw_cbs == []                             # callback removed


def test_discover_no_gateway():
    assert discover_thymios(None) == []
    assert discover_thymios(_FakeSniffGateway([], connected=False)) == []


def test_discover_reports_each_new_address_live():
    frames = [
        "6188388144256a373283006a25",   # Thymio 6a25
        "61888081443732256a8300",       # 6a25 again — no second callback
        "6188018144317b373283ff",       # Thymio 7b31
    ]
    live: list[str] = []
    gw = _FakeSniffGateway(frames)
    discover_thymios(gw, channel=25, secs=0.01, on_found=live.append)
    assert live == ["6a25", "7b31"]                      # once per robot, in order


def test_discover_stop_event_ends_the_scan_early():
    import threading
    import time

    stop = threading.Event()
    stop.set()                                           # already stopped
    gw = _FakeSniffGateway([])
    t0 = time.monotonic()
    discover_thymios(gw, channel=25, secs=5.0, stop=stop)
    assert time.monotonic() - t0 < 2.0                   # didn't sit out the 5 s
    assert ("thymio", "sniff_stop", {}) in gw.sent       # C6 put back cleanly

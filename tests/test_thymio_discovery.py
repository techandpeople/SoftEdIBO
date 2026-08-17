"""Tests for Thymio address discovery (no hardware)."""

import json

from src.robots.thymio.thymio_discovery import parse_thymio_addr, discover_thymios


# --- frame parsing ---------------------------------------------------------

def test_parse_host_to_thymio_frame():
    # FCF|seq|PAN 8144|dst 256a(=0x6a25)|src 3732(=host)|...  -> the robot is the dst
    assert parse_thymio_addr("6188388144256a373283006a25") == 0x6A25


def test_parse_thymio_to_host_frame():
    # dst 3732(=host)|src 256a(=0x6a25) -> the robot is the src
    assert parse_thymio_addr("61888081443732256a8300") == 0x6A25


def test_parse_second_thymio_address():
    # dst 317b = 0x7b31 (a different robot)
    assert parse_thymio_addr("6188018144317b373283ff") == 0x7B31


def test_parse_ignores_ack_and_other_pans():
    assert parse_thymio_addr("020025c80a") is None          # 5-byte ACK - no addresses
    assert parse_thymio_addr("6188018144c2f4") is None       # too short after PAN
    assert parse_thymio_addr("618801f4c2256a373283ff") is None  # PAN 0xf4c2, not the Thymio net
    assert parse_thymio_addr("nothex") is None


# --- end-to-end discovery over a fake active-discovery gateway -------------

class _FakeDiscoverGateway:
    """Emits ``thymio_found`` lines to the raw callback when discovery starts.

    Mirrors the C6: ``thymio_discover`` on=True broadcasts LIST_NODES and reports
    each replying robot as ``{"type":"thymio_found","addr":...}``.
    """

    def __init__(self, addrs, connected=True):
        self.is_connected = connected
        self._addrs = addrs
        self._raw_cbs: list = []
        self.sent: list = []

    def on_raw(self, cb):
        self._raw_cbs.append(cb)

    def remove_raw_callback(self, cb):
        if cb in self._raw_cbs:
            self._raw_cbs.remove(cb)

    def _emit(self, obj):
        line = json.dumps(obj)
        for cb in list(self._raw_cbs):
            cb("rx", line)

    def _reboot_ack(self, cmd):
        # Mirror the C6: a reboot re-announces with rcp_ready on the clean boot,
        # which reboot_c6() waits for before scanning.
        if cmd == "reboot":
            self._emit({"type": "rcp_ready", "src": "c6", "source": "thymio"})
            return True
        return False

    def send(self, target, cmd, **kwargs):
        self.sent.append((target, cmd, kwargs))
        if self._reboot_ack(cmd):
            return True
        if cmd == "thymio_discover" and kwargs.get("on"):
            for addr in self._addrs:
                self._emit({"type": "thymio_found", "src": "c6", "addr": addr})
        return True


class _FakeLegacyFrameGateway(_FakeDiscoverGateway):
    """An old C6 that answers discovery by streaming raw ``frame`` lines instead
    of ``thymio_found`` - exercises the passive fallback parse."""

    def send(self, target, cmd, **kwargs):
        self.sent.append((target, cmd, kwargs))
        if self._reboot_ack(cmd):
            return True
        if cmd == "thymio_discover" and kwargs.get("on"):
            for data in self._addrs:                     # here _addrs holds frame hex
                self._emit({"type": "frame", "ch": 25, "data": data,
                            "source": "thymio"})
        return True


def test_discover_returns_distinct_addresses():
    gw = _FakeDiscoverGateway(["6a25", "6a25", "7b31"])   # dupe robot answers twice
    addrs = discover_thymios(gw, channel=25, secs=0.01)
    assert addrs == ["6a25", "7b31"]                     # first-seen order, deduped, hex
    # it started the C6 active discovery and put it back
    assert ("thymio", "thymio_discover", {"on": True, "ch": 25}) in gw.sent
    assert ("thymio", "thymio_discover", {"on": False}) in gw.sent
    assert gw._raw_cbs == []                             # callback removed


def test_discover_reboots_c6_before_scanning():
    # A clean radio is required (repeated scans deafen the C6's RX), so discovery
    # must reboot the C6 and THEN start the scan.
    gw = _FakeDiscoverGateway(["6a25"])
    addrs = discover_thymios(gw, channel=25, secs=0.01)
    assert addrs == ["6a25"]
    cmds = [c for _, c, _ in gw.sent]
    assert cmds.index("reboot") < cmds.index("thymio_discover")   # reboot first
    assert gw._raw_cbs == []                                      # taps all removed


def test_discover_skips_reboot_when_already_stopped():
    import threading

    stop = threading.Event()
    stop.set()
    gw = _FakeDiscoverGateway(["6a25"])
    discover_thymios(gw, channel=25, secs=5.0, stop=stop)
    assert "reboot" not in [c for _, c, _ in gw.sent]   # no reboot on an aborted scan


def test_discover_legacy_frame_fallback():
    # An old firmware streams raw frames; discovery still extracts the addresses.
    frames = [
        "6188388144256a373283006a25",   # host -> Thymio 6a25
        "6188018144317b373283ff",       # host -> Thymio 7b31
        "020025c80a",                   # ACK - ignored
    ]
    addrs = discover_thymios(_FakeLegacyFrameGateway(frames), channel=25, secs=0.01)
    assert addrs == ["6a25", "7b31"]


def test_discover_no_gateway():
    assert discover_thymios(None) == []
    assert discover_thymios(_FakeDiscoverGateway([], connected=False)) == []


def test_discover_reports_each_new_address_live():
    live: list[str] = []
    gw = _FakeDiscoverGateway(["6a25", "6a25", "7b31"])   # 6a25 answers twice
    discover_thymios(gw, channel=25, secs=0.01, on_found=live.append)
    assert live == ["6a25", "7b31"]                      # once per robot, in order


def test_discover_stop_event_ends_the_scan_early():
    import threading
    import time

    stop = threading.Event()
    stop.set()                                           # already stopped
    gw = _FakeDiscoverGateway([])
    t0 = time.monotonic()
    discover_thymios(gw, channel=25, secs=5.0, stop=stop)
    assert time.monotonic() - t0 < 2.0                   # didn't sit out the 5 s
    assert ("thymio", "thymio_discover", {"on": False}) in gw.sent   # C6 put back cleanly

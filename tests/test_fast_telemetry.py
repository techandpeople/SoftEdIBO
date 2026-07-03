"""Tests for the fast-telemetry (status_rate) request helper."""

from src.hardware.fast_telemetry import FastTelemetry


class _FakeGateway:
    """Records every send(mac, cmd, **kw) for assertions."""

    def __init__(self):
        self.sent = []

    def send(self, mac, cmd, **kw):
        self.sent.append((mac, cmd, kw))
        return True


def test_start_keepalive_stop_cycle():
    gw = _FakeGateway()
    ft = FastTelemetry(gw, "AA:01", rate_ms=40, ttl_ms=3000)
    assert not ft.active
    ft.start()
    assert ft.active
    ft.keepalive()
    ft.stop()
    assert not ft.active
    # start + keepalive send the fast rate; stop reverts (ms=0).
    assert gw.sent[0] == ("AA:01", "status_rate", {"ms": 40, "ttl": 3000})
    assert gw.sent[1] == ("AA:01", "status_rate", {"ms": 40, "ttl": 3000})
    assert gw.sent[2] == ("AA:01", "status_rate", {"ms": 0, "ttl": 0})


def test_keepalive_and_stop_are_noops_when_inactive():
    gw = _FakeGateway()
    ft = FastTelemetry(gw, "AA:01")
    assert ft.keepalive() is False
    assert ft.stop() is False
    assert gw.sent == []


def test_context_manager_starts_and_stops():
    gw = _FakeGateway()
    with FastTelemetry(gw, "BB:02", rate_ms=50, ttl_ms=2000) as ft:
        assert ft.active
    assert [s[2]["ms"] for s in gw.sent] == [50, 0]      # on then off


def test_no_gateway_is_safe():
    ft = FastTelemetry(None, "AA:01")
    assert ft.start() is False
    assert ft.stop() is False

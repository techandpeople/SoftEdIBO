"""Tests for the Thymio C6 WiFi-OTA updater (reuses WifiOTAUpdater over the
gateway's "thymio" UART route instead of an ESP-NOW MAC)."""

from src.hardware.wifi_ota_updater import C6WifiOTAUpdater


class _FakeGateway:
    def __init__(self):
        self.sent: list[tuple] = []

    @property
    def is_connected(self) -> bool:
        return True

    def send(self, target, command, **kwargs) -> bool:
        self.sent.append((target, command, kwargs))
        return True

    def send_gateway(self, command, **kwargs) -> bool:
        return True

    def on_message(self, cb) -> None:
        self._cb = cb

    def remove_message_callback(self, cb) -> None:
        pass


def test_c6_updater_targets_thymio_route():
    up = C6WifiOTAUpdater(_FakeGateway(), "x.bin", "SoftEdIBO", "softedibo")
    assert up._mac == "thymio"


def test_c6_rcp_ready_counts_as_done():
    up = C6WifiOTAUpdater(_FakeGateway(), "x.bin", "SoftEdIBO", "softedibo")
    # The C6 has no ota_done; its fresh boot banner is the finish signal.
    assert up._handle_node("rcp_ready", {"src": "c6"}) is True
    assert up._done is True


def test_c6_still_handles_inherited_node_replies():
    up = C6WifiOTAUpdater(_FakeGateway(), "x.bin", "SoftEdIBO", "softedibo")
    assert up._handle_node("ota_wifi_start", {}) is True
    assert up._started is True


def test_c6_handle_routes_thymio_source_to_node():
    up = C6WifiOTAUpdater(_FakeGateway(), "x.bin", "SoftEdIBO", "softedibo")
    # Gateway tags the C6's replies with source:"thymio" == self._mac.
    up._handle({"type": "rcp_ready", "src": "c6", "source": "thymio"})
    assert up._done is True


def test_c6_ota_wifi_fail_surfaces_as_error():
    up = C6WifiOTAUpdater(_FakeGateway(), "x.bin", "SoftEdIBO", "softedibo")
    assert up._handle_node("ota_wifi_fail", {"reason": "http", "code": 0, "err": 3}) is True
    assert up._error == "http"
    assert not up._done


def test_c6_await_start_sends_ota_wifi_to_thymio():
    gw = _FakeGateway()
    up = C6WifiOTAUpdater(gw, "x.bin", "SoftEdIBO", "softedibo")
    up.START_TIMEOUT = 0.05          # instance override keeps the test fast
    up._await_start("http://192.168.4.1/fw")
    ota = [(t, c, kw) for (t, c, kw) in gw.sent if c == "ota_wifi"]
    assert ota, "no ota_wifi command was sent"
    target, _cmd, kw = ota[0]
    assert target == "thymio"
    assert kw["ssid"] == "SoftEdIBO"
    assert kw["pass"] == "softedibo"
    assert kw["url"].endswith("/fw")

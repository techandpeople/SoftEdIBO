"""Unit tests for the WiFi (gateway-proxy) node firmware updater.

Covers the shared ``extract_app_image`` helper and the WifiOTAUpdater flow: a
fake gateway records the ``ota_store_*`` USB push and the ``ota_wifi`` trigger and
injects the gateway/node replies, so the whole handshake runs without hardware.
The test reassembles the staged image from the captured base64 chunks to confirm
the PC streams exactly the app bytes.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time

from src.hardware.node_ota_updater import _APP_OFFSET, _ESP_APP_MAGIC, extract_app_image
from src.hardware.wifi_ota_updater import WifiOTAUpdater

MAC = "AA:BB:CC:DD:EE:FF"
URL = "http://192.168.4.1/fw"


# ---------------------------------------------------------------------------
# extract_app_image
# ---------------------------------------------------------------------------

def test_extract_bare_app_passes_through():
    app = bytes([_ESP_APP_MAGIC]) + b"\x05\x02\x20" + b"\xab" * 100
    assert extract_app_image(app) == app


def test_extract_merged_image_slices_app_partition():
    app = bytes([_ESP_APP_MAGIC]) + b"the-real-app-bytes" * 10
    merged = b"\xff" * _APP_OFFSET + app
    assert extract_app_image(merged) == app


def test_extract_invalid_returns_empty():
    assert extract_app_image(b"\xff" * (_APP_OFFSET + 16)) == b""
    assert extract_app_image(b"") == b""


# ---------------------------------------------------------------------------
# WifiOTAUpdater
# ---------------------------------------------------------------------------

class FakeGateway:
    """Gateway stand-in: records sends, replays gateway/node messages."""

    def __init__(self, auto_ack=False):
        self.is_connected = True
        self.gateway_sends: list[tuple[str, dict]] = []   # (cmd, kwargs)
        self.node_sends: list[tuple[str, str, dict]] = []  # (mac, cmd, kwargs)
        self._cb = None
        self._lock = threading.Lock()
        self._auto_ack = auto_ack   # mirror the gateway's cumulative ota_store_ack
        self._recv = 0              # bytes "received" so far (for acks)

    def on_message(self, cb):
        self._cb = cb

    def remove_message_callback(self, cb):
        if self._cb is cb:
            self._cb = None

    def send_gateway(self, command, **kwargs):
        ack = None
        with self._lock:
            self.gateway_sends.append((command, kwargs))
            if self._auto_ack and command == "ota_store_data":
                self._recv += len(base64.b64decode(kwargs["data"]))
                ack = {"type": "ota_store_ack", "len": self._recv}
        # Deliver outside the lock (the updater's handler runs on this thread).
        if ack is not None:
            self.deliver(ack)
        return True

    def send(self, mac, command, **kwargs):
        with self._lock:
            self.node_sends.append((mac, command, kwargs))
        return True

    def deliver(self, message: dict):
        if self._cb is not None:
            self._cb(message)

    def staged_image(self) -> bytes:
        with self._lock:
            chunks = [kw["data"] for cmd, kw in self.gateway_sends
                      if cmd == "ota_store_data"]
        return b"".join(base64.b64decode(c) for c in chunks)

    def wait_gateway(self, command: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for cmd, kw in self.gateway_sends:
                    if cmd == command:
                        return kw
            time.sleep(0.02)
        raise AssertionError(f"gateway never got {command!r}: {self.gateway_sends}")

    def wait_node(self, command: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for _mac, cmd, kw in self.node_sends:
                    if cmd == command:
                        return kw
            time.sleep(0.02)
        raise AssertionError(f"node never got {command!r}: {self.node_sends}")


def _write_app(tmp_path, body: bytes):
    path = tmp_path / "firmware.bin"
    path.write_bytes(body)
    return path


def _run_in_thread(updater: WifiOTAUpdater):
    result: list = []
    thread = threading.Thread(target=lambda: result.append(updater.run()), daemon=True)
    thread.start()
    return result, thread


def _make(gateway, tmp_path, body, ssid="SoftEdIBO", password="softedibo"):
    return WifiOTAUpdater(gateway, MAC, _write_app(tmp_path, body), ssid, password)


def test_wifi_ota_stages_and_confirms(tmp_path):
    app = bytes([_ESP_APP_MAGIC]) + b"node-firmware-payload" * 64
    gw = FakeGateway()
    result, thread = _run_in_thread(_make(gw, tmp_path, app))

    begin = gw.wait_gateway("ota_store_begin")
    assert begin["size"] == len(app)
    assert begin["md5"] == hashlib.md5(app).hexdigest()
    gw.deliver({"type": "ota_store_ready"})

    # Once the PC has sent ota_store_end the whole image is on the gateway.
    gw.wait_gateway("ota_store_end")
    assert gw.staged_image() == app
    gw.deliver({"type": "ota_stored", "ok": True, "url": URL})

    trigger = gw.wait_node("ota_wifi")
    assert trigger["url"] == URL
    assert trigger["ssid"] == "SoftEdIBO"
    assert trigger["pass"] == "softedibo"

    gw.deliver({"source": MAC, "type": "ota_wifi_start"})
    gw.deliver({"source": MAC, "type": "ota_done"})
    thread.join(timeout=5)
    assert result and result[0][0] is True


def test_wifi_ota_large_image_flow_controlled(tmp_path):
    # Bigger than the in-flight window so _await_window actually blocks/resumes;
    # the fake gateway acks cumulatively like the real one, so it drains fine.
    app = bytes([_ESP_APP_MAGIC]) + b"node-firmware-payload" * 4096  # ~84 KB
    gw = FakeGateway(auto_ack=True)
    result, thread = _run_in_thread(_make(gw, tmp_path, app))

    gw.wait_gateway("ota_store_begin")
    gw.deliver({"type": "ota_store_ready"})
    gw.wait_gateway("ota_store_end")
    assert gw.staged_image() == app
    gw.deliver({"type": "ota_stored", "ok": True, "url": URL})

    gw.wait_node("ota_wifi")
    gw.deliver({"source": MAC, "type": "ota_wifi_start"})
    gw.deliver({"source": MAC, "type": "ota_done"})
    thread.join(timeout=5)
    assert result and result[0][0] is True


def test_wifi_ota_stalls_when_gateway_stops_acking(tmp_path):
    # A large image with no acks must fail fast (not hang) once the window fills.
    app = bytes([_ESP_APP_MAGIC]) + b"z" * (16 * 1024)
    gw = FakeGateway()  # never acks
    updater = _make(gw, tmp_path, app)
    updater.STORE_ACK_TIMEOUT = 0.3
    result, thread = _run_in_thread(updater)

    gw.wait_gateway("ota_store_begin")
    gw.deliver({"type": "ota_store_ready"})
    thread.join(timeout=5)
    assert result and result[0][0] is False
    assert "Staging failed" in result[0][1]
    # Never reached the node trigger because staging never completed.
    assert not gw.node_sends


def test_wifi_ota_no_psram_reports_clear_error(tmp_path):
    app = bytes([_ESP_APP_MAGIC]) + b"x" * 200
    gw = FakeGateway()
    result, thread = _run_in_thread(_make(gw, tmp_path, app))
    gw.wait_gateway("ota_store_begin")
    gw.deliver({"type": "ota_store_error", "reason": "no_psram"})
    thread.join(timeout=5)
    assert result and result[0][0] is False
    assert "PSRAM" in result[0][1]


def test_wifi_ota_node_error_fails(tmp_path):
    app = bytes([_ESP_APP_MAGIC]) + b"y" * 200
    gw = FakeGateway()
    result, thread = _run_in_thread(_make(gw, tmp_path, app))
    gw.wait_gateway("ota_store_begin")
    gw.deliver({"type": "ota_store_ready"})
    gw.wait_gateway("ota_store_end")
    gw.deliver({"type": "ota_stored", "ok": True, "url": URL})
    gw.wait_node("ota_wifi")
    gw.deliver({"source": MAC, "type": "ota_error", "reason": "bad_wifi_args"})
    thread.join(timeout=5)
    assert result and result[0][0] is False
    assert "bad_wifi_args" in result[0][1]


def test_wifi_ota_rejects_invalid_image(tmp_path):
    ok, msg = _make(FakeGateway(), tmp_path, b"\xff" * 64).run()
    assert ok is False
    assert "app image" in msg


def test_wifi_ota_requires_ssid(tmp_path):
    app = bytes([_ESP_APP_MAGIC]) + b"x" * 50
    ok, msg = _make(FakeGateway(), tmp_path, app, ssid="", password="").run()
    assert ok is False
    assert "access-point name" in msg

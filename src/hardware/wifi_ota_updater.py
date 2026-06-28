"""Fast over-the-air firmware update for a node, over WiFi via the gateway SoftAP.

Where :class:`~src.hardware.node_ota_updater.NodeOTAUpdater` streams the image as
small base64 ESP-NOW chunks (robust, but a ~800 KB image crawls across in
minutes), this is far faster *and* keeps the PC on the USB cable only:

  1. the PC streams the image to the gateway over the existing USB link
     (``ota_store_*`` gateway-local commands); the gateway buffers it in PSRAM;
  2. the PC tells the node (over ESP-NOW) to join the gateway's SoftAP and
     HTTP-download the image straight from the gateway;
  3. the node verifies it (``x-MD5`` header) and reboots.

So the bulk bytes go PC -> gateway over the cable, then gateway -> node over WiFi.
The PC never joins any WiFi network. This requires the **S3 SoftAP build**
(``-DGATEWAY_AP`` + PSRAM); the gateway firmware is in
``firmware/gateway/src/main.cpp`` and the node side in ``firmware/common/se_ota.h``.

Protocol::

    PC -> gw    {"cmd":"ota_store_begin","size":N,"md5":"<hex>"}
    gw -> PC    {"type":"ota_store_ready"} | {"type":"ota_store_error","reason":..}
    PC -> gw    {"cmd":"ota_store_data","data":"<base64>"}   (xN, no per-chunk ack)
    PC -> gw    {"cmd":"ota_store_end"}
    gw -> PC    {"type":"ota_stored","ok":true,"url":"http://192.168.4.1/fw"}
    PC -> node  {"cmd":"ota_wifi","ssid":..,"pass":..,"url":..}
    node -> PC  {"type":"ota_wifi_start"}        (node joins AP + downloads)
    node -> PC  {"type":"ota_done"}              (broadcast on the fresh boot)

Confirmation comes on the node's next boot (a successful HTTPUpdate reboots
before it can reply), via the RTC flag the firmware arms (see se_ota.h).

This class is framework-agnostic (no Qt); the GUI runs :meth:`run` in a worker
thread and surfaces ``on_progress`` / ``on_log``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.node_ota_updater import extract_app_image

logger = logging.getLogger(__name__)

# Raw bytes per ota_store_data line. The gateway reads USB lines into a 512-byte
# buffer, so the whole JSON line must stay under that: 256 raw -> 344 base64 chars
# + ~33 envelope ≈ 380 bytes, comfortably below 512.
STORE_CHUNK = 256

# Flow control for the staging stream. USB is ordered but the gateway's RX buffer
# can overflow if we blast the whole image back-to-back, silently dropping bytes
# (-> size_mismatch). So we keep at most STORE_WINDOW bytes "in flight" (sent but
# not yet acked) and block for the gateway's cumulative ota_store_ack to catch up.
# Kept well under the gateway's RX buffer (16 KB); the gateway acks every 4 KB.
STORE_WINDOW = 8192

# Friendlier text for the gateway's ota_store_error reasons.
_STAGE_ERRORS = {
    "no_psram": "gateway has no PSRAM for staging — WiFi OTA needs the S3 "
                "access-point build",
    "ap_not_supported": "this gateway is not the access-point build — flash the "
                        "S3 AP firmware, or use the ESP-NOW transport",
}


class WifiOTAUpdater:
    """Pushes one firmware image to a node fast, PC staying on the USB cable."""

    STORE_READY_TIMEOUT = 5.0   # wait for ota_store_ready after ota_store_begin
    STORE_ACK_TIMEOUT = 5.0     # wait for an ota_store_ack while window is full
    STORE_END_TIMEOUT = 10.0    # wait for ota_stored after ota_store_end
    START_TIMEOUT = 8.0         # wait for ota_wifi_start (node accepted)
    START_RESEND = 2.0          # resend ota_wifi until accepted
    # connect (≤15 s on the node) + download + verify + reboot + re-announce.
    DONE_TIMEOUT = 90.0

    def __init__(
        self,
        gateway: ESPNowGateway,
        mac: str,
        firmware_path: str | Path,
        ssid: str,
        password: str,
        *,
        on_progress: Callable[[int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ):
        self._gateway = gateway
        self._mac = mac
        self._path = Path(firmware_path)
        self._ssid = ssid
        self._password = password
        self._on_progress = on_progress
        self._on_log = on_log

        self._event = threading.Event()    # set when a relevant reply arrives
        self._store_ready = False          # gateway accepted the image (PSRAM ok)
        self._acked = 0                    # bytes the gateway has confirmed storing
        self._stored_url: str | None = None  # gateway URL once the image is staged
        self._started = False              # node acked ota_wifi_start
        self._done = False                 # node broadcast ota_done after reboot
        self._error: str | None = None     # gateway/node error reason
        self._error_detail = ""            # extra context for the error (e.g. got/want)
        self._cancelled = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request abort; :meth:`run` returns shortly after."""
        self._cancelled = True
        self._event.set()

    def run(self) -> tuple[bool, str]:
        """Perform the update. Returns ``(ok, message)``. Blocking."""
        if not self._gateway.is_connected:
            return False, "Gateway not connected"
        if not self._ssid:
            return False, "No access-point name given"
        try:
            data = self._path.read_bytes()
        except OSError as e:
            return False, f"Cannot read firmware: {e}"
        image = extract_app_image(data)
        if not image:
            return False, "Firmware does not contain a valid app image"

        md5 = hashlib.md5(image).hexdigest()
        self._gateway.on_message(self._handle)
        try:
            return self._drive(image, md5)
        finally:
            self._gateway.remove_message_callback(self._handle)

    # ------------------------------------------------------------------
    # Transfer phases
    # ------------------------------------------------------------------

    def _drive(self, image: bytes, md5: str) -> tuple[bool, str]:
        self._log(f"{self._path.name}: {len(image)} bytes — staging on the gateway")

        # 1. Stage the image on the gateway over USB.
        url = self._stage(image, md5)
        if url is None:
            return False, self._stage_failure_message()

        # 2. Tell the node to join the AP and pull from the gateway.
        self._log("image staged — telling the node to download it")
        if not self._await_start(url):
            return False, self._start_failure_message()

        # 3. Wait for the node to finish + reboot + announce.
        self._log("node joining the access point and downloading…")
        return self._await_done()

    def _stage_failure_message(self) -> str:
        if self._cancelled:
            return "Cancelled"
        reason = self._error or "gateway did not stage the image"
        return "Staging failed: " + _STAGE_ERRORS.get(reason, reason) + self._error_detail

    def _start_failure_message(self) -> str:
        if self._cancelled:
            return "Cancelled"
        if self._error:
            return f"Node refused WiFi update: {self._error}"
        return ("Node did not accept the WiFi update (no ota_wifi_start). "
                "Is it online over ESP-NOW?")

    def _await_done(self) -> tuple[bool, str]:
        deadline = time.monotonic() + self.DONE_TIMEOUT
        while time.monotonic() < deadline:
            if self._cancelled:
                return False, "Cancelled"
            if self._done:
                if self._on_progress:
                    self._on_progress(100)
                return True, "Update complete — node rebooted into new firmware"
            if self._error:
                return False, f"Node error: {self._error}"
            self._event.wait(0.2)
            self._event.clear()
        return False, ("Timed out waiting for the node to confirm. It may not "
                       "have reached the gateway access point.")

    def _stage(self, image: bytes, md5: str) -> str | None:
        """Stream the image to the gateway's PSRAM; return its serve URL or None."""
        self._event.clear()
        self._gateway.send_gateway("ota_store_begin", size=len(image), md5=md5)
        if not self._wait(lambda: self._store_ready or self._error,
                          self.STORE_READY_TIMEOUT):
            return None
        if self._error:
            return None

        total = len(image)
        last_pct = -1
        sent = 0
        for off in range(0, total, STORE_CHUNK):
            if self._cancelled or self._error:
                return None
            # Flow control: don't let unacked bytes outrun the gateway's RX buffer.
            if not self._await_window(sent):
                return None
            block = image[off:off + STORE_CHUNK]
            chunk = base64.b64encode(block).decode("ascii")
            if not self._gateway.send_gateway("ota_store_data", data=chunk):
                self._error = "usb_write_failed"
                return None
            sent += len(block)
            pct = min(99, int(sent * 100 / total))
            if pct != last_pct and self._on_progress:
                last_pct = pct
                self._on_progress(pct)

        self._stored_url = None
        self._event.clear()
        self._gateway.send_gateway("ota_store_end")
        if not self._wait(lambda: self._stored_url or self._error,
                          self.STORE_END_TIMEOUT):
            return None
        return self._stored_url

    def _await_window(self, sent: int) -> bool:
        """Block until in-flight (sent - acked) bytes drop below the window.

        Returns False on cancel/error/timeout (caller aborts staging).
        """
        deadline = time.monotonic() + self.STORE_ACK_TIMEOUT
        while sent - self._acked > STORE_WINDOW:
            if self._cancelled or self._error:
                return False
            if time.monotonic() >= deadline:
                self._error = "stage_stalled"
                self._error_detail = " (gateway stopped acking — likely USB loss)"
                return False
            if self._event.wait(0.2):
                self._event.clear()
                deadline = time.monotonic() + self.STORE_ACK_TIMEOUT
        return True

    def _await_start(self, url: str) -> bool:
        deadline = time.monotonic() + self.START_TIMEOUT
        next_send = 0.0
        while time.monotonic() < deadline:
            if self._cancelled or self._error:
                return False
            if self._started:
                return True
            now = time.monotonic()
            if now >= next_send:
                # "pass" is a Python keyword, so it can't be a kwarg name.
                self._gateway.send(self._mac, "ota_wifi", ssid=self._ssid,
                                   url=url, **{"pass": self._password})
                next_send = now + self.START_RESEND
            self._event.wait(0.2)
            self._event.clear()
        return self._started

    def _wait(self, predicate: Callable[[], Any], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._cancelled:
                return False
            if predicate():
                return True
            self._event.wait(0.2)
            self._event.clear()
        return bool(predicate())

    # ------------------------------------------------------------------
    # Gateway message handler (runs on the gateway read thread)
    # ------------------------------------------------------------------

    def _handle(self, data: dict[str, Any]) -> None:
        mtype = data.get("type")
        # Gateway-local replies carry no "source"; node replies are MAC-filtered.
        if data.get("source") == self._mac:
            handled = self._handle_node(mtype, data)
        else:
            handled = self._handle_gateway(mtype, data)
        if handled:
            self._event.set()

    def _handle_gateway(self, mtype: Any, data: dict[str, Any]) -> bool:
        if mtype == "ota_store_ready":
            self._store_ready = True
        elif mtype == "ota_store_ack":
            try:
                self._acked = int(data.get("len", self._acked))
            except (TypeError, ValueError):
                pass
        elif mtype == "ota_stored":
            self._stored_url = data.get("url")
        elif mtype == "ota_store_error":
            self._error = str(data.get("reason", "store_error"))
            got, want = data.get("got"), data.get("want")
            if got is not None and want is not None:
                self._error_detail = f" ({got} of {want} bytes received)"
        else:
            return False
        return True

    def _handle_node(self, mtype: Any, data: dict[str, Any]) -> bool:
        if mtype == "ota_wifi_start":
            self._started = True
        elif mtype == "ota_done":
            self._done = True
        elif mtype == "ota_error":
            self._error = str(data.get("reason", "unknown"))
        else:
            return False
        return True

    def _log(self, msg: str) -> None:
        logger.info("WiFi OTA %s: %s", self._mac, msg)
        if self._on_log:
            self._on_log(msg)

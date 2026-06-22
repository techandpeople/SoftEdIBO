"""Over-the-air firmware update for a single node, over ESP-NOW via the gateway.

The PC drives the whole transfer; the node only writes flash and ACKs (see the
firmware side in ``firmware/common/se_ota.h``). The firmware image is read from
disk, split into small chunks, base64-encoded and streamed as ordinary JSON
ESP-NOW messages through the existing :class:`ESPNowGateway` pipe — no WiFi/AP
involved, so it works anywhere the gateway can reach the node.

Protocol::

    PC -> node  {"cmd":"ota_begin","size":N,"md5":"<hex>","chunk":144}
    node -> PC  {"type":"ota_ready"} | {"type":"ota_error","reason":...}
    PC -> node  {"cmd":"ota_data","seq":S,"data":"<base64>"}
    node -> PC  {"type":"ota_ack","seq":S}
    PC -> node  {"cmd":"ota_end"}
    node -> PC  {"type":"ota_done"} (node reboots) | {"type":"ota_error",...}

A small sliding window pipelines chunks to hide the round-trip latency; lost or
reordered chunks are retransmitted on a per-sequence timeout. The node tolerates
duplicates and drops future chunks, so the window stays correct under loss.

This class is framework-agnostic (no Qt). The GUI runs :meth:`run` in a worker
thread and surfaces ``on_progress`` / ``on_log`` to the user.
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

logger = logging.getLogger(__name__)

# Chunk sizing: although ESP-NOW's nominal payload cap is 250 bytes, in practice
# the gateway->node relay silently drops frames once the JSON payload grows past
# ~190 bytes — a 144-byte chunk (~232 byte payload) never reaches the node, while
# a 96-byte chunk (~164 byte payload) gets through reliably. So keep chunks small.
# The data message ``{"cmd":"ota_data","seq":NNNNN,"data":"<base64>"}`` adds ~40
# bytes of envelope; 96 raw bytes -> 128 base64 chars (multiple of 3, no padding).
CHUNK_SIZE = 96

# ESP32 application image magic byte (first byte of a bare app-partition image).
_ESP_APP_MAGIC = 0xE9
# Standard offset of the application partition within a full/merged flash image
# (bootloader @ 0x1000, partition table @ 0x8000, app @ 0x10000).
_APP_OFFSET = 0x10000


class NodeOTAUpdater:
    """Streams a firmware ``.bin`` to one node and verifies it, over ESP-NOW."""

    WINDOW = 8           # max chunks in flight
    ACK_TIMEOUT = 0.4    # seconds before retransmitting an unacked chunk
    MAX_RETRIES = 8      # per-chunk retransmit attempts before aborting
    READY_TIMEOUT = 5.0  # seconds to wait for ota_ready / ota_done

    def __init__(
        self,
        gateway: ESPNowGateway,
        mac: str,
        firmware_path: str | Path,
        *,
        on_progress: Callable[[int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ):
        self._gateway = gateway
        self._mac = mac
        self._path = Path(firmware_path)
        self._on_progress = on_progress
        self._on_log = on_log

        self._lock = threading.Lock()
        self._acked: set[int] = set()
        self._event = threading.Event()   # set when a relevant reply arrives
        self._terminal: str | None = None  # "done" or an error reason
        self._cancelled = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request abort; :meth:`run` returns ``(False, ...)`` shortly after."""
        self._cancelled = True
        self._event.set()

    def run(self) -> tuple[bool, str]:
        """Perform the update. Returns ``(ok, message)``. Blocking."""
        if not self._gateway.is_connected:
            return False, "Gateway not connected"
        try:
            data = self._path.read_bytes()
        except OSError as e:
            return False, f"Cannot read firmware: {e}"
        if not data:
            return False, "Firmware file is empty"

        data = self._app_image(data)
        if not data:
            return False, "Firmware does not contain a valid app image"

        md5 = hashlib.md5(data).hexdigest()
        chunks = [
            base64.b64encode(data[i : i + CHUNK_SIZE]).decode("ascii")
            for i in range(0, len(data), CHUNK_SIZE)
        ]
        self._log(f"{self._path.name}: {len(data)} bytes, {len(chunks)} chunks")

        self._gateway.on_message(self._handle)
        try:
            return self._transfer(len(data), md5, chunks)
        finally:
            self._gateway.remove_message_callback(self._handle)

    def _app_image(self, data: bytes) -> bytes:
        """Return the bare app-partition image to flash over OTA.

        The bundled node ``.bin`` files are *merged* flash images (bootloader +
        partition table + app, meant for esptool at offset 0x0). ``Update.h`` on
        the node expects only the application image (first byte = 0xE9), so a
        merged image makes the node reject the first sector. Detect that case and
        slice out the app partition at its standard 0x10000 offset; a file that
        already starts with the app magic is sent as-is.
        """
        if data[0] == _ESP_APP_MAGIC:
            return data
        if len(data) > _APP_OFFSET and data[_APP_OFFSET] == _ESP_APP_MAGIC:
            self._log(f"merged flash image — extracting app at 0x{_APP_OFFSET:x}")
            return data[_APP_OFFSET:]
        return b""

    # ------------------------------------------------------------------
    # Transfer phases
    # ------------------------------------------------------------------

    def _transfer(self, size: int, md5: str, chunks: list[str]) -> tuple[bool, str]:
        # 1. begin
        self._terminal = None
        self._event.clear()
        self._gateway.send(self._mac, "ota_begin", size=size, md5=md5, chunk=CHUNK_SIZE)
        ok, msg = self._wait_terminal({"ready"}, self.READY_TIMEOUT, "ota_begin")
        if not ok:
            return False, msg

        # 2. stream data with a sliding window + per-chunk retransmit
        total = len(chunks)
        sent_at: dict[int, float] = {}
        retries: dict[int, int] = {}
        base = 0          # lowest unacked sequence
        next_seq = 0      # next sequence not yet sent
        last_pct = -1

        while base < total:
            if self._cancelled:
                return False, "Cancelled"
            if self._terminal and self._terminal != "ready":
                return False, f"Node error: {self._terminal}"

            # Fill the window.
            while next_seq < min(base + self.WINDOW, total):
                self._send_chunk(next_seq, chunks[next_seq])
                sent_at[next_seq] = time.monotonic()
                next_seq += 1

            self._event.wait(self.ACK_TIMEOUT)
            self._event.clear()

            # Advance base over contiguous acks.
            with self._lock:
                while base in self._acked:
                    sent_at.pop(base, None)
                    retries.pop(base, None)
                    base += 1

            pct = int(base * 100 / total)
            if pct != last_pct:
                last_pct = pct
                if self._on_progress:
                    self._on_progress(pct)

            # Retransmit timed-out, still-unacked chunks in the window.
            now = time.monotonic()
            for seq in range(base, next_seq):
                if seq in self._acked:
                    continue
                if now - sent_at.get(seq, now) < self.ACK_TIMEOUT:
                    continue
                retries[seq] = retries.get(seq, 0) + 1
                if retries[seq] > self.MAX_RETRIES:
                    return False, f"No ACK for chunk {seq} after {self.MAX_RETRIES} retries"
                self._send_chunk(seq, chunks[seq])
                sent_at[seq] = now

        # 3. end + verify + reboot
        self._terminal = None
        self._event.clear()
        self._gateway.send(self._mac, "ota_end")
        ok, msg = self._wait_terminal({"done"}, self.READY_TIMEOUT, "ota_end")
        if not ok:
            return False, msg
        if self._on_progress:
            self._on_progress(100)
        return True, "Update complete — node rebooting"

    def _send_chunk(self, seq: int, data: str) -> None:
        self._gateway.send(self._mac, "ota_data", seq=seq, data=data)

    def _wait_terminal(
        self, accept: set[str], timeout: float, phase: str
    ) -> tuple[bool, str]:
        """Wait for a terminal reply; ``accept`` holds the success token(s)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._cancelled:
                return False, "Cancelled"
            if self._terminal is not None:
                if self._terminal in accept:
                    return True, "ok"
                return False, f"Node error during {phase}: {self._terminal}"
            self._event.wait(0.1)
            self._event.clear()
        return False, f"Timed out waiting for node during {phase}"

    # ------------------------------------------------------------------
    # Gateway message handler (runs on the gateway read thread)
    # ------------------------------------------------------------------

    def _handle(self, data: dict[str, Any]) -> None:
        if data.get("source") != self._mac:
            return
        mtype = data.get("type")
        if mtype == "ota_ack":
            seq = data.get("seq")
            if seq is not None:
                with self._lock:
                    self._acked.add(int(seq))
            self._event.set()
        elif mtype == "ota_ready":
            self._terminal = "ready"
            self._event.set()
        elif mtype == "ota_done":
            self._terminal = "done"
            self._event.set()
        elif mtype == "ota_error":
            self._terminal = str(data.get("reason", "unknown"))
            self._event.set()

    def _log(self, msg: str) -> None:
        logger.info("OTA %s: %s", self._mac, msg)
        if self._on_log:
            self._on_log(msg)

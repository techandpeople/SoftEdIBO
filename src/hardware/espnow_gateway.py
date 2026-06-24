"""Serial communication with the ESP-NOW gateway ESP32.

The gateway ESP32 is connected to the PC via USB/serial and relays
commands to/from remote ESP32 nodes using the ESP-NOW protocol.

Protocol format (JSON over serial):
  PC -> Gateway:  {"target": "AA:BB:CC:DD:EE:01", "cmd": "inflate", "chamber": 0, "value": 255}
  Gateway -> PC:  {"source": "AA:BB:CC:DD:EE:01", "type": "status", "chamber": 0, "pressure": 128}
"""

import json
import logging
import threading
import weakref
from typing import Any, Callable

import serial

logger = logging.getLogger(__name__)


class ESPNowGateway:
    """Manages serial communication with the ESP-NOW gateway."""

    def __init__(self, port: str, baud_rate: int = 115200):
        self._port = port
        self._baud_rate = baud_rate
        self._serial: serial.Serial | None = None
        self._running = False
        self._read_thread: threading.Thread | None = None
        # WeakMethod refs so old controllers are GC'd after robot reconfiguration.
        self._callbacks: list[weakref.ref] = []
        # Strong refs to raw serial taps (e.g. the serial monitor). Held strongly
        # because the consumer deregisters explicitly when it closes.
        self._raw_callbacks: list[Callable[[str, str], None]] = []
        self._logged_disconnected = False
        self._known_macs: set[str] = set()

    @property
    def known_macs(self) -> frozenset[str]:
        """MAC addresses of nodes that have sent at least one message."""
        return frozenset(self._known_macs)

    @property
    def is_connected(self) -> bool:
        """Check if gateway is connected."""
        return self._serial is not None and self._serial.is_open

    def connect(self) -> bool:
        """Open serial connection to the gateway."""
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                timeout=1,
            )
            self._running = True
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True
            )
            self._read_thread.start()
            logger.info("Connected to ESP-NOW gateway on %s", self._port)
            return True
        except serial.SerialException as e:
            logger.warning("Failed to connect to gateway on %s: %s", self._port, e)
            return False

    def disconnect(self) -> None:
        """Close serial connection."""
        self._running = False
        if self._read_thread is not None:
            self._read_thread.join(timeout=2)
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        self._known_macs.clear()
        logger.info("Disconnected from ESP-NOW gateway")

    def send(self, target_mac: str, command: str, **kwargs: Any) -> bool:
        """Send a command to a remote ESP32 node via the gateway."""
        if not self.is_connected:
            if not self._logged_disconnected:
                logger.debug("Gateway not connected — commands will be dropped")
                self._logged_disconnected = True
            return False
        self._logged_disconnected = False

        message = {"target": target_mac, "cmd": command, **kwargs}
        try:
            line = json.dumps(message)
            self._serial.write((line + "\n").encode("utf-8"))
            logger.debug("Sent to %s: %s", target_mac, command)
            self._emit_raw("tx", line)
            return True
        except serial.SerialException:
            logger.exception("Failed to send command to %s", target_mac)
            return False

    def send_gateway(self, command: str, **kwargs: Any) -> bool:
        """Send a command to the gateway itself (not relayed to a node).

        Gateway-local commands carry no ``target`` field, so the firmware
        handles them on the spot instead of forwarding over ESP-NOW (e.g.
        ``get_ap`` / ``set_ap`` for the SoftAP build).
        """
        if not self.is_connected:
            return False
        message = {"cmd": command, **kwargs}
        try:
            line = json.dumps(message)
            self._serial.write((line + "\n").encode("utf-8"))
            self._emit_raw("tx", line)
            return True
        except serial.SerialException:
            logger.exception("Failed to send gateway command %s", command)
            return False

    def send_raw(self, text: str) -> bool:
        """Write a raw line to the gateway serial port.

        Used by the serial monitor to send arbitrary commands the structured
        :meth:`send` API can't express. A trailing newline is added if missing.
        """
        if not self.is_connected:
            return False
        line = text if text.endswith("\n") else text + "\n"
        try:
            self._serial.write(line.encode("utf-8"))
            self._emit_raw("tx", line.rstrip("\n"))
            return True
        except serial.SerialException:
            logger.exception("Failed to send raw serial line")
            return False

    def scan(self) -> None:
        """Broadcast a ping to all nodes. Nodes that respond will appear in known_macs."""
        self.send("FF:FF:FF:FF:FF:FF", "ping")

    def on_message(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for incoming messages from ESP32 nodes."""
        self._callbacks.append(weakref.WeakMethod(callback))

    def remove_message_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Deregister a callback previously passed to :meth:`on_message`.

        Used by short-lived consumers (e.g. the OTA updater) that need to stop
        receiving messages deterministically rather than waiting for GC.
        """
        self._callbacks = [
            wr for wr in self._callbacks if wr() is not None and wr() != callback
        ]

    def on_raw(self, callback: Callable[[str, str], None]) -> None:
        """Register a tap for raw serial traffic.

        ``callback(direction, text)`` is invoked for every complete line, where
        ``direction`` is ``"rx"`` (from the gateway) or ``"tx"`` (sent by us).
        RX callbacks fire on the serial read thread, so consumers that touch the
        GUI must marshal to the GUI thread (e.g. via a Qt signal).
        """
        self._raw_callbacks.append(callback)

    def remove_raw_callback(self, callback: Callable[[str, str], None]) -> None:
        """Deregister a tap previously passed to :meth:`on_raw`."""
        if callback in self._raw_callbacks:
            self._raw_callbacks.remove(callback)

    def _emit_raw(self, direction: str, text: str) -> None:
        for cb in list(self._raw_callbacks):
            try:
                cb(direction, text)
            except Exception:
                logger.exception("Raw serial tap callback failed")

    def _dispatch_line(self, raw: bytes) -> None:
        """Parse one complete line and fan it out to registered callbacks."""
        if not raw.strip():
            return
        self._emit_raw("rx", raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        try:
            data = json.loads(raw.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Invalid JSON from gateway: %s", raw)
            return
        if "source" in data:
            self._known_macs.add(data["source"])
        dead: list[weakref.ref] = []
        for wr in self._callbacks:
            cb = wr()
            if cb is None:
                dead.append(wr)
            else:
                cb(data)
        for d in dead:
            self._callbacks.remove(d)

    def _read_loop(self) -> None:
        """Background thread that reads incoming serial data.

        Accumulates raw bytes and only dispatches complete newline-terminated
        lines. This avoids parsing partial messages: ``readline()`` returns
        whatever is buffered when the serial timeout fires, which can split a
        message mid-line and produce spurious "Invalid JSON" warnings.

        The gateway streams continuously, so opening the port usually lands
        mid-message. The bytes before the first newline are therefore a partial
        fragment — discard them to resync rather than dispatch a broken line.
        """
        buf = bytearray()
        synced = False
        while self._running and self._serial is not None:
            try:
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if not chunk:
                    continue
                buf.extend(chunk)
                while b"\n" in buf:
                    raw, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    if not synced:
                        synced = True  # first fragment is a partial line; drop it
                        continue
                    self._dispatch_line(raw)
            except serial.SerialException:
                logger.exception("Serial read error — gateway disconnected")
                if self._serial is not None:
                    self._serial.close()
                    self._serial = None
                self._running = False
                break

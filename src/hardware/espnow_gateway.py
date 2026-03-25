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
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
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
            line = json.dumps(message) + "\n"
            self._serial.write(line.encode("utf-8"))
            logger.debug("Sent to %s: %s", target_mac, command)
            return True
        except serial.SerialException:
            logger.exception("Failed to send command to %s", target_mac)
            return False

    def scan(self) -> None:
        """Broadcast a ping to all nodes. Nodes that respond will appear in known_macs."""
        self.send("FF:FF:FF:FF:FF:FF", "ping")

    def on_message(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for incoming messages from ESP32 nodes."""
        self._callbacks.append(callback)

    def _read_loop(self) -> None:
        """Background thread that reads incoming serial data."""
        while self._running and self._serial is not None:
            try:
                line = self._serial.readline()
                if not line:
                    continue
                data = json.loads(line.decode("utf-8").strip())
                if "source" in data:
                    self._known_macs.add(data["source"])
                for callback in self._callbacks:
                    callback(data)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from gateway: %s", line)
            except serial.SerialException:
                logger.exception("Serial read error")
                break

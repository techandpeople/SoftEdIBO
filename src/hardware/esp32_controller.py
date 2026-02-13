"""High-level controller for a single ESP32 node via the ESP-NOW gateway."""

import logging
from typing import Any, Callable

from src.hardware.espnow_gateway import ESPNowGateway

logger = logging.getLogger(__name__)


class ESP32Controller:
    """Controls a single remote ESP32 node through the gateway."""

    def __init__(self, mac_address: str, gateway: ESPNowGateway):
        self.mac_address = mac_address
        self._gateway = gateway
        self._last_status: dict[str, Any] = {}
        self._touch_callbacks: list[Callable[[int, int], None]] = []

        self._gateway.on_message(self._handle_message)

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to this ESP32 node."""
        return self._gateway.send(self.mac_address, command, **kwargs)

    def inflate(self, chamber: int, value: int = 255) -> bool:
        """Inflate an air chamber to the given value (0-255)."""
        return self.send_command("inflate", chamber=chamber, value=value)

    def deflate(self, chamber: int) -> bool:
        """Deflate an air chamber."""
        return self.send_command("deflate", chamber=chamber)

    def set_pressure(self, chamber: int, pressure: int) -> bool:
        """Set target pressure for an air chamber (0-255)."""
        return self.send_command("set_pressure", chamber=chamber, value=pressure)

    def calibrate_sensor(self, sensor_id: int) -> bool:
        """Request sensor calibration on the ESP32."""
        return self.send_command("calibrate_sensor", sensor=sensor_id)

    def on_touch(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for touch sensor events.

        Args:
            callback: Called with (sensor_id, raw_value) on each reading.
        """
        self._touch_callbacks.append(callback)

    def get_last_status(self) -> dict[str, Any]:
        """Get the last known status of this ESP32 node."""
        return self._last_status.copy()

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process incoming messages, filtering for this node's MAC."""
        if data.get("source") == self.mac_address:
            self._last_status.update(data)
            logger.debug("Status from %s: %s", self.mac_address, data)

            if data.get("type") == "touch":
                sensor_id = data.get("sensor", 0)
                raw_value = data.get("value", 0)
                for callback in self._touch_callbacks:
                    callback(sensor_id, raw_value)

    def __repr__(self) -> str:
        return f"ESP32Controller(mac={self.mac_address!r})"

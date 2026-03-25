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
        self._touch_callbacks:    list[Callable[[int, int], None]] = []
        self._pressure_callbacks: list[Callable[[int, int], None]] = []

        self._gateway.on_message(self._handle_message)

    @property
    def is_connected(self) -> bool:
        """True if the underlying gateway is connected."""
        return self._gateway.is_connected

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to this ESP32 node."""
        return self._gateway.send(self.mac_address, command, **kwargs)

    def inflate(self, chamber: int, delta: int = 10) -> bool:
        """Inflate a chamber by delta % of its max pressure (0-100)."""
        return self.send_command("inflate", chamber=chamber, delta=delta)

    def deflate(self, chamber: int, delta: int = 10) -> bool:
        """Deflate a chamber by delta % of its max pressure (0-100)."""
        return self.send_command("deflate", chamber=chamber, delta=delta)

    def hold(self, chamber: int) -> bool:
        """Hold pressure — stop pump, close inflate and deflate valves for this chamber."""
        return self.send_command("hold", chamber=chamber)

    def set_pressure(self, chamber: int, value: int) -> bool:
        """Set absolute target pressure for a chamber (0-100 %)."""
        return self.send_command("set_pressure", chamber=chamber, value=value)

    def set_max_pressure(self, chamber: int, value: int) -> bool:
        """Set per-chamber max pressure on the ESP32 node (0-100 %).

        The node will refuse to inflate past this limit, even if the app crashes.
        """
        return self.send_command("set_max_pressure", chamber=chamber, value=value)

    def calibrate_sensor(self, sensor_id: int) -> bool:
        """Request sensor calibration on the ESP32."""
        return self.send_command("calibrate_sensor", sensor=sensor_id)

    def on_touch(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for touch sensor events.

        Args:
            callback: Called with (sensor_id, raw_value) on each reading.
        """
        self._touch_callbacks.append(callback)

    def on_pressure(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for pressure status messages.

        Args:
            callback: Called with (chamber_id, pressure) on each status reading.
        """
        self._pressure_callbacks.append(callback)

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

            elif data.get("type") == "status" and "chamber" in data and "pressure" in data:
                chamber_id = int(data["chamber"])
                pressure   = int(data["pressure"])
                for callback in self._pressure_callbacks:
                    callback(chamber_id, pressure)

    def __repr__(self) -> str:
        return f"ESP32Controller(mac={self.mac_address!r})"

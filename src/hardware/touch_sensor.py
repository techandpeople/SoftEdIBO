"""Touch sensor model for detecting physical interaction.

Supports different sensor types (e.g. copper capacitive, magnetic/Hall effect)
that can be attached to air chambers or robot surfaces.
"""

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SensorType(Enum):
    """Supported touch sensor types."""
    CAPACITIVE_COPPER = "capacitive_copper"
    HALL_EFFECT_MAGNET = "hall_effect_magnet"


class TouchSensor:
    """Represents a single touch sensor on a robot."""

    def __init__(
        self,
        sensor_id: int,
        sensor_type: SensorType,
        esp32_mac: str,
        pin: int,
    ):
        """Initialize a touch sensor.

        Args:
            sensor_id: Unique identifier for this sensor.
            sensor_type: Type of sensor hardware.
            esp32_mac: MAC address of the ESP32 reading this sensor.
            pin: GPIO pin on the ESP32 for this sensor.
        """
        self.sensor_id = sensor_id
        self.sensor_type = sensor_type
        self.esp32_mac = esp32_mac
        self.pin = pin
        self._raw_value: int = 0
        self._is_touched: bool = False
        self._threshold: int = 512  # Default threshold for touch detection

    @property
    def raw_value(self) -> int:
        """Get the last raw sensor reading."""
        return self._raw_value

    @property
    def is_touched(self) -> bool:
        """Check if the sensor is currently being touched."""
        return self._is_touched

    @property
    def threshold(self) -> int:
        """Get the touch detection threshold."""
        return self._threshold

    @threshold.setter
    def threshold(self, value: int) -> None:
        self._threshold = value

    def update(self, raw_value: int) -> bool:
        """Update sensor with a new reading. Returns True if touch state changed."""
        self._raw_value = raw_value
        was_touched = self._is_touched
        self._is_touched = raw_value >= self._threshold
        return self._is_touched != was_touched

    def to_dict(self) -> dict[str, Any]:
        """Serialize sensor state."""
        return {
            "sensor_id": self.sensor_id,
            "type": self.sensor_type.value,
            "raw_value": self._raw_value,
            "is_touched": self._is_touched,
            "threshold": self._threshold,
            "pin": self.pin,
        }

    def __repr__(self) -> str:
        return (
            f"TouchSensor(id={self.sensor_id}, type={self.sensor_type.value}, "
            f"touched={self._is_touched}, raw={self._raw_value})"
        )

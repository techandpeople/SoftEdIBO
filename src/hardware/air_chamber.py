"""Air chamber model representing a single inflatable chamber."""

from enum import Enum


class ChamberState(Enum):
    """Possible states of an air chamber."""
    IDLE = "idle"
    INFLATING = "inflating"
    DEFLATING = "deflating"
    INFLATED = "inflated"


class AirChamber:
    """Represents a single air chamber with inflate/deflate capabilities."""

    def __init__(self, chamber_id: int, esp32_mac: str):
        self.chamber_id = chamber_id
        self.esp32_mac = esp32_mac
        self._state = ChamberState.IDLE
        self._pressure: int = 0  # 0-255

    @property
    def state(self) -> ChamberState:
        """Get current chamber state."""
        return self._state

    @state.setter
    def state(self, value: ChamberState) -> None:
        self._state = value

    @property
    def pressure(self) -> int:
        """Get current pressure level (0-255)."""
        return self._pressure

    @pressure.setter
    def pressure(self, value: int) -> None:
        self._pressure = max(0, min(255, value))

    def __repr__(self) -> str:
        return (
            f"AirChamber(id={self.chamber_id}, "
            f"state={self._state.value}, pressure={self._pressure})"
        )

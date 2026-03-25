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

    def __init__(
        self,
        chamber_id: int,
        esp32_mac: str,
        max_pressure: int = 100,
    ):
        self.chamber_id = chamber_id
        self.esp32_mac = esp32_mac
        self.max_pressure = max(0, min(100, max_pressure))
        self._state = ChamberState.IDLE
        self._pressure: int = 0         # 0-100 (% of max), current measured value
        self._target_pressure: int = 0  # 0-100 (% of max), commanded target

    @property
    def state(self) -> ChamberState:
        """Get current chamber state."""
        return self._state

    @state.setter
    def state(self, value: ChamberState) -> None:
        self._state = value

    @property
    def pressure(self) -> int:
        """Get current pressure level (0-100 %)."""
        return self._pressure

    @pressure.setter
    def pressure(self, value: int) -> None:
        self._pressure = max(0, min(self.max_pressure, value))

    @property
    def target_pressure(self) -> int:
        """Get commanded target pressure (0-100 %)."""
        return self._target_pressure

    @target_pressure.setter
    def target_pressure(self, value: int) -> None:
        self._target_pressure = max(0, min(self.max_pressure, value))

    def __repr__(self) -> str:
        return (
            f"AirChamber(id={self.chamber_id}, "
            f"state={self._state.value}, pressure={self._pressure}%)"
        )

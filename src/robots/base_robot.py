"""Abstract base class for all SoftEdIBO robots."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.hardware.air_reservoir import AirReservoir


class RobotStatus(Enum):
    """Possible states of a robot."""
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"
    ERROR        = "error"


class BaseRobot(ABC):
    """Abstract base class that all robots must implement."""

    def __init__(self, robot_id: str, name: str):
        self.robot_id = robot_id
        self.name = name
        self._status = RobotStatus.DISCONNECTED

    @property
    def status(self) -> RobotStatus:
        return self._status

    @property
    def pressure_reservoir(self) -> "AirReservoir | None":
        """Shared pressurised-air reservoir, if this robot has one."""
        return None

    @property
    def vacuum_reservoir(self) -> "AirReservoir | None":
        """Shared vacuum reservoir, if this robot has one."""
        return None

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection with the robot. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the robot."""
        ...

    @abstractmethod
    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to the robot. Returns True on success."""
        ...

    @abstractmethod
    def get_status_data(self) -> dict[str, Any]:
        """Get detailed status data from the robot."""
        ...

    def pause(self) -> None:
        """Freeze all chambers at current pressure. Override for hardware robots."""

    def resume(self) -> None:
        """Allow new commands after a pause. Override if needed."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.robot_id!r}, status={self._status.value})"

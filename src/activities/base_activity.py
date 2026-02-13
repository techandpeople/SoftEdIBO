"""Abstract base class for Robot Hospital activities."""

from abc import ABC, abstractmethod
from typing import Any

from src.core.session import Session
from src.robots.base_robot import BaseRobot


class BaseActivity(ABC):
    """Abstract base class for all study activities."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def setup(self, session: Session, robots: list[BaseRobot]) -> None:
        """Prepare the activity with the given session and robots."""
        ...

    @abstractmethod
    def start(self) -> None:
        """Start the activity."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the activity."""
        ...

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Get the current activity state."""
        ...

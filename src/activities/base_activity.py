"""Abstract base class for Robot Hospital activities."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session


class BaseActivity(ABC):
    """Abstract base class for all study activities.

    Each concrete activity must declare a ``robot_type`` class variable
    indicating which robot type it works with.  Only one robot type is
    allowed per activity; multiple robot instances of that type are fine.

    Example::

        class GroupTouchActivity(BaseActivity):
            robot_type = TurtleRobot
    """

    robot_type: ClassVar[type[BaseRobot]]

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        """Validate robot types and delegate to :meth:`_setup`.

        Raises:
            TypeError: If any robot is not an instance of :attr:`robot_type`.
        """
        wrong = [r for r in robots if not isinstance(r, self.robot_type)]
        if wrong:
            raise TypeError(
                f"{type(self).__name__} requires {self.robot_type.__name__} robots, "
                f"got: {[type(r).__name__ for r in wrong]}"
            )
        self._setup(session, robots)

    @abstractmethod
    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        """Activity-specific setup logic. Called after robot type validation."""
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
        """Return the current activity state as a dictionary."""
        ...

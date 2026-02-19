"""Robot connection management panel."""

from PySide6.QtWidgets import QWidget

from src.gui.ui_robot_panel import Ui_RobotPanel
from src.robots.base_robot import BaseRobot, RobotStatus
from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.tree.tree_robot import TreeRobot
from src.robots.turtle.turtle_robot import TurtleRobot


class RobotPanel(QWidget, Ui_RobotPanel):
    """Panel for monitoring and managing robot connections.

    Call :meth:`refresh` after the robot registry changes to update
    all list views.
    """

    def __init__(self):
        super().__init__()
        self.setupUi(self)

    def refresh(self, robots: list[BaseRobot]) -> None:
        """Repopulate all robot lists from the given robot collection."""
        turtles  = [r for r in robots if isinstance(r, TurtleRobot)]
        thymios  = [r for r in robots if isinstance(r, ThymioRobot)]
        trees    = [r for r in robots if isinstance(r, TreeRobot)]

        self._fill_list(self.turtle_list,  turtles)
        self._fill_list(self.thymio_list,  thymios)
        self._fill_list(self.tree_list,    trees)

    @staticmethod
    def _fill_list(list_widget, robots: list[BaseRobot]) -> None:
        list_widget.clear()
        for robot in robots:
            status = robot.status.value
            icon = "●" if robot.status == RobotStatus.CONNECTED else "○"
            list_widget.addItem(f"{icon}  {robot.name}  [{status}]")

"""RobotMonitorWidget — visualises all Skins of a robot as a horizontal row.

One SkinWidget is created per Skin in robot.skins.
Works for any robot that exposes a `skins: dict[str, Skin]` attribute
(TurtleRobot, TreeRobot, ThymioRobot, SimulatedRobot).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QSizePolicy

from src.gui.monitor.skin_widget import SkinWidget
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot


class RobotMonitorWidget(QGroupBox):
    """Widget for a single robot — one SkinWidget per Skin."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, robot: BaseRobot) -> None:
        super().__init__(robot.robot_id)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._skin_widgets: list[SkinWidget] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        skins: dict[str, Skin] = getattr(robot, "skins", {})

        for skin in skins.values():
            sw = SkinWidget(skin)
            sw.touch_event.connect(self.touch_event)
            self._skin_widgets.append(sw)
            layout.addWidget(sw)

        if not skins:
            layout.addWidget(QLabel(f"{robot.robot_id} — no skins configured"))

    def set_paused(self, paused: bool) -> None:
        for sw in self._skin_widgets:
            sw.set_paused(paused)

    def refresh(self) -> None:
        for sw in self._skin_widgets:
            sw.refresh()

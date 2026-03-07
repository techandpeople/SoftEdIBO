"""RobotMonitorPanel — scrollable container for all robot monitor widgets.

Holds one RobotMonitorWidget per robot and auto-refreshes every 300 ms
by reading directly from the model objects (no polling via get_status()).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from src.gui.monitor.flow_layout import FlowLayout
from src.gui.monitor.robot_monitor_widget import RobotMonitorWidget
from src.robots.base_robot import BaseRobot


class _WheelFilter(QObject):
    """Forwards wheel events from a child widget to a QScrollArea."""

    def __init__(self, scroll_area: QScrollArea) -> None:
        super().__init__(scroll_area)
        self._scroll = scroll_area

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel:
            self._scroll.wheelEvent(event)
            return True
        return False


class RobotMonitorPanel(QWidget):
    """Scrollable panel — one RobotMonitorWidget per robot, auto-refreshed."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        self._inner = QWidget()
        self._layout = FlowLayout(self._inner, h_spacing=6, v_spacing=6)
        scroll.setWidget(self._inner)
        self._inner.installEventFilter(_WheelFilter(scroll))

        self._robot_widgets: list[RobotMonitorWidget] = []

        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh)

    def set_robots(self, robots: list[BaseRobot]) -> None:
        """Rebuild the panel for the given robot list."""
        self._timer.stop()
        for rw in self._robot_widgets:
            self._layout.removeWidget(rw)
            rw.deleteLater()
        self._robot_widgets.clear()

        for robot in robots:
            rw = RobotMonitorWidget(robot)
            rw.touch_event.connect(self.touch_event)
            self._robot_widgets.append(rw)
            self._layout.addWidget(rw)

        if robots:
            self._timer.start()

    def set_paused(self, paused: bool) -> None:
        """Enable or disable all interactive buttons in the monitor."""
        for rw in self._robot_widgets:
            rw.set_paused(paused)

    def _refresh(self) -> None:
        for rw in self._robot_widgets:
            rw.refresh()

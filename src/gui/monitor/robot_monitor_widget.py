"""RobotMonitorWidget - visualises all Skins of a robot.

Layout: one SkinWidget per Skin in robot.skins. Works for any robot that
exposes a ``skins: dict[str, Skin]`` attribute.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QSizePolicy

from src.gui.monitor.cpr_led_indicator import CprLedIndicator
from src.gui.monitor.skin_widget import SkinWidget
from src.hardware.skin import Skin
from src.robots.base_robot import BaseRobot


class RobotMonitorWidget(QGroupBox):
    """Widget for a single robot - one SkinWidget per Skin."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, robot: BaseRobot) -> None:
        super().__init__(robot.robot_id)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._robot = robot
        self._skin_widgets: list[SkinWidget] = []
        self._cpr_indicator = CprLedIndicator(self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        # Kept at the leading edge of the robot card, as requested. It is
        # hidden unless the active activity is CPR.
        layout.addWidget(self._cpr_indicator)

        skins: dict[str, Skin] = getattr(robot, "skins", {})
        for skin in skins.values():
            sw = SkinWidget(skin)
            sw.touch_event.connect(self.touch_event)
            self._skin_widgets.append(sw)
            layout.addWidget(sw)

        if not skins:
            layout.addWidget(QLabel(f"{robot.robot_id} - nothing configured"))

    def organ_view_for(self, skin_id: str):
        """The organ display (grid view) of a skin on this robot, or None."""
        for sw in self._skin_widgets:
            if sw.skin_id == skin_id:
                return sw.organ_view
        return None

    def set_paused(self, paused: bool) -> None:
        for sw in self._skin_widgets:
            sw.set_paused(paused)

    def set_activity(self, activity) -> None:
        """Update the left-side CPR LED from this robot's activity state."""
        is_cpr = bool(activity and "cpr" in str(getattr(activity, "name", "")).lower())
        state = None
        if is_cpr:
            try:
                states = activity.get_state().get("states", {})
                # A behaviour has one unit per skin. A single robot-level unit
                # (bare Thymio) uses only the robot id, hence both forms.
                prefix = f"{self._robot.robot_id}/"
                robot_states = [value for key, value in states.items()
                                if key == self._robot.robot_id or key.startswith(prefix)]
                # Green only when every visible skin unit has completed.
                if robot_states and all(str(value).lower() in {"complete", "success", "done"}
                                        for value in robot_states):
                    state = "complete"
                elif robot_states:
                    state = robot_states[0]
            except Exception:
                # The monitor must stay usable if a third-party activity has a
                # partial get_state implementation.
                state = None
        self._cpr_indicator.set_activity_state(state, visible=is_cpr)

    def refresh(self) -> None:
        for sw in self._skin_widgets:
            sw.refresh()

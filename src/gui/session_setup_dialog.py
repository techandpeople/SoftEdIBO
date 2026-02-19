"""Dialog for configuring a new session before it starts."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QListWidgetItem, QWidget

from src.activities.base_activity import BaseActivity
from src.activities.group_touch import GroupTouchActivity
from src.data.database import Database
from src.data.models import ParticipantRecord
from src.gui.ui_session_setup_dialog import Ui_SessionSetupDialog
from src.robots.base_robot import BaseRobot

# All activities available for selection, in display order.
_ACTIVITIES: list[BaseActivity] = [
    GroupTouchActivity(),
]


class SessionSetupDialog(QDialog, Ui_SessionSetupDialog):
    """Dialog that collects session ID, activity, robot, and participant selection.

    Args:
        robots: All currently connected robots across all types.
        db: Database instance used to load the participant roster.
        parent: Optional parent widget.
    """

    def __init__(
        self,
        robots: list[BaseRobot],
        db: Database,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setupUi(self)

        self._robots = robots

        for activity in _ACTIVITIES:
            self.activity_combo.addItem(activity.name, userData=activity)

        self.activity_combo.currentIndexChanged.connect(self._on_activity_changed)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        self.session_id_input.setText(db.next_session_id())

        self._on_activity_changed(0)
        self._populate_participants(db.get_all_participants())

    # ------------------------------------------------------------------
    # Public result accessors (call after exec() == QDialog.Accepted)
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """The session ID entered by the user."""
        return self.session_id_input.text().strip()

    @property
    def selected_activity(self) -> BaseActivity | None:
        """The activity chosen in the combo box."""
        return self.activity_combo.currentData()

    @property
    def selected_robots(self) -> list[BaseRobot]:
        """Robots checked by the user in the list."""
        result = []
        for i in range(self.robots_list.count()):
            item = self.robots_list.item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                robot = item.data(Qt.ItemDataRole.UserRole)
                if robot is not None:
                    result.append(robot)
        return result

    @property
    def selected_participants(self) -> list[ParticipantRecord]:
        """Participants checked by the user in the list."""
        result = []
        for i in range(self.participants_list.count()):
            item = self.participants_list.item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                record = item.data(Qt.ItemDataRole.UserRole)
                if record is not None:
                    result.append(record)
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _on_activity_changed(self, index: int) -> None:
        """Refresh the robot list whenever the selected activity changes."""
        activity: BaseActivity | None = self.activity_combo.itemData(index)
        if activity is None:
            self.robot_type_label.setText("—")
            self._populate_robots([])
            return

        self.robot_type_label.setText(activity.robot_type.__name__)
        compatible = [r for r in self._robots if isinstance(r, activity.robot_type)]
        self._populate_robots(compatible)

    def _populate_robots(self, robots: list[BaseRobot]) -> None:
        """Fill the list widget with checkable robot entries."""
        self.robots_list.clear()

        if not robots:
            self.no_robots_label.setVisible(True)
            self.robots_list.setVisible(False)
            return

        self.no_robots_label.setVisible(False)
        self.robots_list.setVisible(True)

        for robot in robots:
            item = QListWidgetItem(f"{robot.robot_id}  [{robot.status.value}]")
            item.setData(Qt.ItemDataRole.UserRole, robot)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.robots_list.addItem(item)

    def _populate_participants(self, records: list[ParticipantRecord]) -> None:
        """Fill the participants list with checkable entries."""
        self.participants_list.clear()

        if not records:
            self.no_participants_label.setVisible(True)
            self.participants_list.setVisible(False)
            return

        self.no_participants_label.setVisible(False)
        self.participants_list.setVisible(True)

        for record in records:
            label = f"{record.participant_id}  {record.alias}"
            if record.age is not None:
                label += f"  (age {record.age})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.participants_list.addItem(item)

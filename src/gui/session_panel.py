"""Session control panel for managing study sessions."""

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QWidget

from src.data.database import Database
from src.data.models import InteractionEvent, SessionRecord
from src.gui.session_setup_dialog import SessionSetupDialog
from src.gui.ui_session_panel import Ui_SessionPanel
from src.robots.base_robot import BaseRobot


class SessionPanel(QWidget, Ui_SessionPanel):
    """Panel that shows the active session state and session controls.

    Args:
        db: Open database instance for persisting session records.

    Signals:
        session_finished: Emitted after a session is stopped so that
            other panels can refresh their data from the database.
    """

    session_finished = Signal()

    def __init__(self, db: Database):
        super().__init__()
        self.setupUi(self)

        self._db = db
        self._available_robots: list[BaseRobot] = []
        self._current_record: SessionRecord | None = None

        self.new_session_btn.clicked.connect(self._open_setup_dialog)
        self.pause_btn.clicked.connect(self._on_pause)
        self.stop_btn.clicked.connect(self._on_stop)

    def set_available_robots(self, robots: list[BaseRobot]) -> None:
        """Update the pool of robots offered in the setup dialog."""
        self._available_robots = robots

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _open_setup_dialog(self) -> None:
        """Open the session setup dialog and start a new session."""
        dialog = SessionSetupDialog(self._available_robots, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        session_id = dialog.session_id
        activity = dialog.selected_activity
        robots = dialog.selected_robots

        if not session_id or activity is None:
            return

        start_time = datetime.now()
        self._current_record = SessionRecord(
            session_id=session_id,
            activity_name=activity.name,
            start_time=start_time,
        )
        self._db.save_session(self._current_record)
        self._db.log_event(InteractionEvent(
            session_id=session_id,
            participant_id="system",
            type="session",
            action="start",
            timestamp=start_time,
        ))

        robot_names = ", ".join(r.name for r in robots) if robots else "none"
        self.session_id_label.setText(session_id)
        self.activity_label.setText(activity.name)
        self.robots_label.setText(robot_names)
        self.status_label.setText("Status: Running")
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

    def _on_pause(self) -> None:
        """Toggle between paused and running state, logging a session event."""
        if self.pause_btn.text() == "Pause":
            action = "pause"
            self.pause_btn.setText("Resume")
            self.status_label.setText("Status: Paused")
        else:
            action = "resume"
            self.pause_btn.setText("Pause")
            self.status_label.setText("Status: Running")

        if self._current_record is not None:
            self._db.log_event(InteractionEvent(
                session_id=self._current_record.session_id,
                participant_id="system",
                type="session",
                action=action,
                timestamp=datetime.now(),
            ))

    def _on_stop(self) -> None:
        """Finish the session, persist end time, and reset the panel."""
        if self._current_record is not None:
            end_time = datetime.now()
            self._current_record.end_time = end_time
            self._db.save_session(self._current_record)
            self._db.log_event(InteractionEvent(
                session_id=self._current_record.session_id,
                participant_id="system",
                type="session",
                action="stop",
                timestamp=end_time,
            ))
            self._current_record = None

        self.session_id_label.setText("—")
        self.activity_label.setText("—")
        self.robots_label.setText("—")
        self.status_label.setText("Status: No active session")
        self.pause_btn.setText("Pause")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.session_finished.emit()

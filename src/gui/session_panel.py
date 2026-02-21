"""Session control panel for managing study sessions."""

from datetime import datetime

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.config.settings import Settings
from src.data import last_assignments as last_asgn
from src.data.database import Database
from src.data.models import InteractionEvent, SessionRecord
from src.gui.assignment_dialog import AssignmentDialog
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
    session_started = Signal(str)   # emits session_id
    session_stopped = Signal()

    def __init__(self, db: Database):
        super().__init__()
        self.setupUi(self)

        self._db = db
        self._available_robots: list[BaseRobot] = []
        self._current_record: SessionRecord | None = None

        self.new_session_btn.clicked.connect(self._open_setup_dialog)
        self.pause_btn.clicked.connect(self._on_pause)
        self.stop_btn.clicked.connect(self._on_stop)

        QTimer.singleShot(0, self._check_for_resume)

    def set_available_robots(self, robots: list[BaseRobot]) -> None:
        """Update the pool of robots offered in the setup dialog."""
        self._available_robots = robots

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_for_resume(self) -> None:
        """Detect interrupted sessions on startup and offer to resume."""
        active = self._db.get_active_sessions()
        if not active:
            return

        # Silently close all but the most recent
        now = datetime.now()
        for record in active[:-1]:
            record.end_time = now
            self._db.save_session(record)

        record = active[-1]
        reply = QMessageBox.question(
            self,
            "Resume Session",
            f"Session <b>{record.session_id}</b> ({record.activity_name}) was interrupted.\n\n"
            "Do you want to resume it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._resume_session(record)
        else:
            record.end_time = now
            self._db.save_session(record)

    def _resume_session(self, record: SessionRecord) -> None:
        """Restore panel state for a previously interrupted session."""
        self._current_record = record
        participants = self._db.get_session_participants(record.session_id)
        participant_names = (
            ", ".join(p.participant_id for p in participants) if participants else "none"
        )

        self.session_id_label.setText(record.session_id)
        self.activity_label.setText(record.activity_name)
        self.robots_label.setText("—")
        self.participants_label.setText(participant_names)
        self.status_label.setText("Status: Running (resumed)")
        self.new_session_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

        self._db.log_event(InteractionEvent(
            session_id=record.session_id,
            participant_id="system",
            type="session",
            action="resume",
            timestamp=datetime.now(),
        ))
        self.session_started.emit(record.session_id)

    def _open_setup_dialog(self) -> None:
        """Open the session setup dialog and start a new session."""
        dialog = SessionSetupDialog(self._available_robots, self._db, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        session_id = dialog.session_id
        activity = dialog.selected_activity
        robots = dialog.selected_robots
        participants = dialog.selected_participants

        if not session_id or activity is None:
            return

        # Open assignment dialog if there are robots and participants to assign
        assignments = []
        if robots and participants:
            last_path = Settings.ROOT / "data" / "last_assignments.json"
            last_data = last_asgn.load(last_path)
            prefill = []
            if last_data and last_asgn.should_prefill(
                last_data,
                [r.robot_id for r in robots],
                [p.participant_id for p in participants],
            ):
                prefill = last_asgn.to_session_assignments(last_data, session_id)

            assign_dlg = AssignmentDialog(
                session_id, robots, participants,
                last_assignments=prefill,
                parent=self,
            )
            if assign_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            assignments = assign_dlg.assignments
            last_asgn.save(
                last_path,
                [r.robot_id for r in robots],
                [p.participant_id for p in participants],
                assignments,
            )

        start_time = datetime.now()
        self._current_record = SessionRecord(
            session_id=session_id,
            activity_name=activity.name,
            start_time=start_time,
        )
        self._db.save_session(self._current_record)

        for participant in participants:
            self._db.link_participant_to_session(session_id, participant.participant_id)

        for assignment in assignments:
            self._db.save_assignment(assignment)

        self._db.log_event(InteractionEvent(
            session_id=session_id,
            participant_id="system",
            type="session",
            action="start",
            timestamp=start_time,
        ))

        robot_names = ", ".join(r.robot_id for r in robots) if robots else "none"
        participant_names = (
            ", ".join(p.participant_id for p in participants)
            if participants else "none"
        )
        self.session_id_label.setText(session_id)
        self.activity_label.setText(activity.name)
        self.robots_label.setText(robot_names)
        self.participants_label.setText(participant_names)
        self.status_label.setText("Status: Running")
        self.new_session_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

        self.session_started.emit(session_id)

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
        self.participants_label.setText("—")
        self.status_label.setText("Status: No active session")
        self.new_session_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.session_finished.emit()
        self.session_stopped.emit()

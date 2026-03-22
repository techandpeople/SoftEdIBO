"""Session control panel for managing study sessions."""

from datetime import datetime

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.activities import get_activity
from src.activities.base_activity import BaseActivity
from src.config.settings import Settings
from src.data import last_assignments as last_asgn
from src.data.database import Database
from src.data.models import InteractionEvent, ParticipantRecord, SessionAssignment, SessionRecord
from src.gui.assignment_dialog import AssignmentDialog
from src.gui.session_setup_dialog import SessionSetupDialog
from src.gui.monitor import RobotMonitorPanel
from src.gui.touch_assignment_panel import TouchAssignmentPanel
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
        self._current_activity: BaseActivity | None = None
        self._skin_participant: dict[str, str] = {}   # skin_id -> participant_id
        self._skin_robot: dict[str, str] = {}         # skin_id -> robot_id
        self._session_participants: list[ParticipantRecord] = []
        self._pending_touches: list[tuple[str, int]] = []  # (skin_id, chamber_id) waiting for assignment
        self._assignment_panel: TouchAssignmentPanel | None = None

        self.new_session_btn.clicked.connect(self._open_setup_dialog)
        self.pause_btn.clicked.connect(self._on_pause)
        self.stop_btn.clicked.connect(self._on_stop)

        self._monitor = RobotMonitorPanel()
        self._monitor.touch_event.connect(self._on_touch_event)
        self.verticalLayout.removeItem(self.verticalSpacer)
        self.verticalLayout.addWidget(self._monitor)

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

        last_path = Settings.ROOT / "data" / "last_assignments.json"
        last_data = last_asgn.load(last_path)
        if last_data and last_data.get("session_id") == record.session_id:
            ids = set(last_data.get("robot_ids", []))
            session_robots = [r for r in self._available_robots if r.robot_id in ids]
        else:
            session_robots = []
        self._current_activity = get_activity(record.activity_name)
        if self._current_activity is not None:
            session_robots = self._current_activity.prepare_robots(session_robots)
        self._monitor.set_robots(session_robots)
        self._build_skin_participant_map(record.session_id)
        self._session_participants = list(participants)
        self._open_assignment_panel(session_robots)

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
        last_path = Settings.ROOT / "data" / "last_assignments.json"
        assignments = []
        if robots and participants:
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

        # Always persist session robots so resume can filter correctly
        last_asgn.save(
            last_path,
            [r.robot_id for r in robots],
            [p.participant_id for p in participants],
            assignments,
            session_id=session_id,
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

        self._current_activity = activity
        robots = activity.prepare_robots(robots)
        self._session_participants = list(participants)
        self.session_started.emit(session_id)
        self._monitor.set_robots(robots)
        self._build_skin_participant_map(session_id)
        self._open_assignment_panel(robots)

    def _build_skin_participant_map(self, session_id: str) -> None:
        """Build a skin_id → participant_id lookup from session assignments."""
        self._skin_participant = {}
        for assignment in self._db.get_session_assignments(session_id):
            for skin_id in assignment.unit_ids:
                self._skin_participant[skin_id] = assignment.participant_id

    def _on_touch_event(self, skin_id: str, chamber_id: int, action: str) -> None:
        """Log a touch interaction attributed to the participant assigned to the skin."""
        if self._current_record is None:
            return

        if action == "press" and skin_id not in self._skin_participant:
            if self._assignment_panel is not None:
                # Defer logging — accumulate all chamber touches for this skin.
                # enqueue() adds to queue on first touch; warns if skin already pending.
                self._pending_touches.append((skin_id, chamber_id))
                self._assignment_panel.enqueue(skin_id)
                return
            # No panel (no participants) — log immediately as unknown
        participant_id = self._skin_participant.get(skin_id, "unknown")
        self._db.log_event(InteractionEvent(
            session_id=self._current_record.session_id,
            participant_id=participant_id,
            type="touch",
            action=action,
            target=f"{skin_id}:{chamber_id}",
            timestamp=datetime.now(),
        ))

    def _open_assignment_panel(self, robots: list[BaseRobot]) -> None:
        """Open the on-touch assignment panel if there are unassigned skins."""
        if self._assignment_panel is not None:
            self._assignment_panel.close()
            self._assignment_panel = None

        if not self._session_participants:
            return

        all_skins: list[tuple[str, str]] = []
        for robot in robots:
            for skin in getattr(robot, "skins", {}).values():
                all_skins.append((skin.skin_id, skin.name))
                self._skin_robot[skin.skin_id] = robot.robot_id

        if not all_skins:
            return

        unassigned = [sid for sid, _ in all_skins if sid not in self._skin_participant]
        if not unassigned:
            return

        panel = TouchAssignmentPanel(all_skins, self._session_participants, parent=self)
        # Mark already-assigned skins
        for skin_id, participant_id in self._skin_participant.items():
            panel.mark_pre_assigned(skin_id, participant_id)

        panel.assigned.connect(self._on_skin_assigned)
        panel.skipped.connect(self._on_skin_touch_skipped)
        self._assignment_panel = panel

    def _on_skin_assigned(self, skin_id: str, participant_id: str) -> None:
        """Called when the operator assigns a skin to a participant via the panel."""
        if self._current_record is None:
            return
        self._skin_participant[skin_id] = participant_id
        robot_id = self._skin_robot.get(skin_id, "")
        if robot_id:
            self._db.save_assignment(SessionAssignment(
                session_id=self._current_record.session_id,
                robot_id=robot_id,
                participant_id=participant_id,
                unit_ids=[skin_id],
            ))
        # Log all pending touches for this skin with the now-known participant
        remaining = []
        for sk, ch in self._pending_touches:
            if sk == skin_id:
                self._db.log_event(InteractionEvent(
                    session_id=self._current_record.session_id,
                    participant_id=participant_id,
                    type="touch",
                    action="press",
                    target=f"{sk}:{ch}",
                    timestamp=datetime.now(),
                ))
            else:
                remaining.append((sk, ch))
        self._pending_touches = remaining

    def _on_skin_touch_skipped(self, skin_id: str) -> None:
        """Called when the operator skips the first queued touch for a skin."""
        if self._current_record is None:
            return
        # Log the first pending touch for this skin as unknown
        for i, (sk, ch) in enumerate(self._pending_touches):
            if sk == skin_id:
                self._db.log_event(InteractionEvent(
                    session_id=self._current_record.session_id,
                    participant_id="unknown",
                    type="touch",
                    action="press",
                    target=f"{sk}:{ch}",
                    timestamp=datetime.now(),
                ))
                self._pending_touches.pop(i)
                break

    def _on_pause(self) -> None:
        """Toggle between paused and running state, logging a session event."""
        if self.pause_btn.text() == "Pause":
            action = "pause"
            self.pause_btn.setText("Resume")
            self.status_label.setText("Status: Paused")
            if self._current_activity is not None:
                self._current_activity.pause()
            self._monitor.set_paused(True)
        else:
            action = "resume"
            self.pause_btn.setText("Pause")
            self.status_label.setText("Status: Running")
            if self._current_activity is not None:
                self._current_activity.resume()
            self._monitor.set_paused(False)

        if self._current_record is not None:
            self._db.log_event(InteractionEvent(
                session_id=self._current_record.session_id,
                participant_id="system",
                type="session",
                action=action,
                timestamp=datetime.now(),
            ))

    def _flush_last_assignments(self, session_id: str) -> None:
        """Overwrite last_assignments.json with the complete final skin→participant map.

        Called on session stop so that on-touch assignments made during the session
        are included in the prefill for the next session, not just pre-session ones.
        """
        if not self._skin_participant or not self._session_participants:
            return
        # Group skin_ids by (robot_id, participant_id)
        grouped: dict[tuple[str, str], list[str]] = {}
        for skin_id, participant_id in self._skin_participant.items():
            robot_id = self._skin_robot.get(skin_id, "")
            if robot_id:
                grouped.setdefault((robot_id, participant_id), []).append(skin_id)
        final_assignments = [
            SessionAssignment(
                session_id=session_id,
                robot_id=robot_id,
                participant_id=participant_id,
                unit_ids=skin_ids,
            )
            for (robot_id, participant_id), skin_ids in grouped.items()
        ]
        robot_ids = list({
            self._skin_robot[s] for s in self._skin_participant if s in self._skin_robot
        })
        participant_ids = [p.participant_id for p in self._session_participants]
        last_path = Settings.ROOT / "data" / "last_assignments.json"
        last_asgn.save(last_path, robot_ids, participant_ids, final_assignments, session_id)

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
            self._flush_last_assignments(self._current_record.session_id)
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

        if self._current_activity is not None:
            self._current_activity.stop()
            self._current_activity = None

        if self._assignment_panel is not None:
            self._assignment_panel.close()
            self._assignment_panel = None

        self.session_finished.emit()
        self.session_stopped.emit()
        self._monitor.set_robots([])
        self._skin_participant = {}
        self._skin_robot = {}
        self._session_participants = []
        self._pending_touches = []

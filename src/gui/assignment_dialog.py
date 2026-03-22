"""Dialog for assigning robot skins/branches to participants before a session."""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.data.models import ParticipantRecord, SessionAssignment
from src.robots.base_robot import BaseRobot


def _units_for_robot(robot: BaseRobot) -> list[str]:
    """Return the list of assignable unit IDs for a robot.

    - TurtleRobot / TreeRobot / ThymioRobot => skin IDs
    - Other => single unit with the robot's own ID
    """
    skins = getattr(robot, "skins", None)
    if skins:
        return list(skins.keys())
    return [robot.robot_id]


class AssignmentDialog(QDialog):
    """Let the operator assign robot units (skins / branches) to participants.

    For every selected robot a group box is shown.  Inside, each participant
    gets a row of checkboxes — one per assignable unit on that robot.  The
    **Auto** button distributes units evenly across participants in round-robin
    order, clearing any manual selection first.

    Args:
        session_id: ID of the session being started.
        robots: Selected robots for this session.
        participants: Selected participants for this session.
        parent: Optional parent widget.
    """

    def __init__(
        self,
        session_id: str,
        robots: list[BaseRobot],
        participants: list[ParticipantRecord],
        last_assignments: list[SessionAssignment] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Assign Robot Units to Participants")
        self.setMinimumSize(560, 400)

        self._session_id = session_id
        self._robots = robots
        self._participants = participants

        # Build a lookup from last assignments: {(robot_id, participant_id): set[unit_id]}
        self._last: dict[tuple[str, str], set[str]] = {}
        for a in (last_assignments or []):
            self._last[(a.robot_id, a.participant_id)] = set(a.unit_ids)

        # _checks[robot_id][participant_id][unit_id] = QCheckBox
        self._checks: dict[str, dict[str, dict[str, QCheckBox]]] = {}

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(
            QLabel("Assign robot skins / branches to each participant.")
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self._container_layout = QVBoxLayout(container)
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        for robot in robots:
            units = _units_for_robot(robot)
            self._build_robot_group(robot, units)

        self._container_layout.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        skip_btn = btn_box.addButton(
            "Skip (assign on touch)", QDialogButtonBox.ButtonRole.ResetRole
        )
        skip_btn.clicked.connect(self.accept)
        main_layout.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Building per-robot group
    # ------------------------------------------------------------------

    def _build_robot_group(self, robot: BaseRobot, units: list[str]) -> None:
        group = QGroupBox(f"{type(robot).__name__}  —  {robot.robot_id}")
        group_layout = QVBoxLayout(group)

        self._checks[robot.robot_id] = {}

        if not units:
            group_layout.addWidget(QLabel("No assignable units on this robot."))
            self._container_layout.addWidget(group)
            return

        if not self._participants:
            group_layout.addWidget(QLabel("No participants selected."))
            self._container_layout.addWidget(group)
            return

        # Header row: unit labels
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Participant"), stretch=2)
        for uid in units:
            lbl = QLabel(uid)
            lbl.setMinimumWidth(80)
            header_row.addWidget(lbl, stretch=1)
        group_layout.addLayout(header_row)

        # One row per participant
        for record in self._participants:
            self._checks[robot.robot_id][record.participant_id] = {}
            row = QHBoxLayout()
            name = f"{record.participant_id}  {record.alias}"
            row.addWidget(QLabel(name), stretch=2)
            prev_units = self._last.get((robot.robot_id, record.participant_id), set())
            for uid in units:
                cb = QCheckBox()
                cb.setChecked(uid in prev_units)
                self._checks[robot.robot_id][record.participant_id][uid] = cb
                row.addWidget(cb, stretch=1)
            group_layout.addLayout(row)

        # Auto / Clear buttons
        btn_row = QHBoxLayout()
        auto_btn = QPushButton("Auto")
        auto_btn.clicked.connect(lambda _=False, r=robot, u=units: self._auto_assign(r, u))
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda _=False, r=robot: self._clear_assignments(r))
        btn_row.addWidget(auto_btn)
        btn_row.addWidget(clear_btn)
        group_layout.addLayout(btn_row)

        self._container_layout.addWidget(group)

    # ------------------------------------------------------------------
    # Auto-assignment
    # ------------------------------------------------------------------

    def _auto_assign(self, robot: BaseRobot, units: list[str]) -> None:
        """Assign one skin to each participant (cycling through skins), ensuring every participant has at least one."""
        robot_checks = self._checks.get(robot.robot_id, {})
        if not robot_checks or not units:
            return

        # Clear all first
        for p_checks in robot_checks.values():
            for cb in p_checks.values():
                cb.setChecked(False)

        for idx, (pid, p_checks) in enumerate(robot_checks.items()):
            uid = units[idx % len(units)]
            p_checks[uid].setChecked(True)

    def _clear_assignments(self, robot: BaseRobot) -> None:
        """Uncheck all assignments for a robot."""
        for p_checks in self._checks.get(robot.robot_id, {}).values():
            for cb in p_checks.values():
                cb.setChecked(False)

    # ------------------------------------------------------------------
    # Result accessor (call after exec() == Accepted)
    # ------------------------------------------------------------------

    @property
    def assignments(self) -> list[SessionAssignment]:
        """Return the list of assignments as configured by the user.

        Only assignments where at least one unit is checked are included.
        """
        result: list[SessionAssignment] = []
        for robot_id, participant_map in self._checks.items():
            for participant_id, unit_map in participant_map.items():
                checked_units = [uid for uid, cb in unit_map.items() if cb.isChecked()]
                if checked_units:
                    result.append(
                        SessionAssignment(
                            session_id=self._session_id,
                            robot_id=robot_id,
                            participant_id=participant_id,
                            unit_ids=checked_units,
                        )
                    )
        return result

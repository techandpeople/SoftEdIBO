"""Dialog for configuring a new session before it starts.

Activities are authored per SKIN condition (Natural / Wrinkles / Organs) and
run on any robot - robot-specific steps live in the behaviour's ``if robot
is...`` blocks - so this dialog lists every configured robot with a LIVE
online/offline status instead of filtering by robot class:

* On open it triggers a gateway node scan and repaints each robot's status as
  the answers arrive: **Online** (all of the robot's nodes answered),
  **Partial** (some did), **Ready** (a node-less wireless Thymio whose
  transport is up) or **Offline**. Ticking is entirely up to the user.
* When a ticked robot's configured skin variants don't match the selected
  activity's target skin, a warning shows under the list - the session can
  still start (useful when the physical skins were swapped without updating
  the config).
* Ticking two robots that share a board (the Turtle and the Tree are built on
  ONE node_multiplexed PCB, moved between bodies) BLOCKS the start: only one of
  them can be mounted, so running both would actuate one body's chamber map
  through the other's. This is the one hard stop here; everything else warns.
"""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFormLayout,
    QListWidgetItem,
    QWidget,
)

from src.activities import available_activities, skin_condition
from src.activities.base_activity import BaseActivity
from src.core import node_sharing
from src.data.database import Database
from src.data.models import ParticipantRecord
from src.gui.base_dialog import BaseDialog
from src.gui.ui_session_setup_dialog import Ui_SessionSetupDialog
from src.robots.base_robot import BaseRobot

# Status refresh cadence after the scan: nodes answer within ~a second, so a
# few quick repaints settle the dots without polling forever.
_STATUS_REFRESH_MS = 500
_STATUS_REFRESH_TICKS = 8

_STATUS_COLORS = {
    "online":  QColor("#2a9d2a"),
    "ready":   QColor("#2a9d2a"),
    "partial": QColor("#b36b00"),
    "offline": QColor("#cc2222"),
}


class SessionSetupDialog(BaseDialog, Ui_SessionSetupDialog):
    """Dialog that collects session ID, activity, robot, and participant selection.

    Args:
        robots: All currently configured robots across all kinds.
        db: Database instance used to load the participant roster.
        gateway: Shared SoftEdIBO gateway (scanned for live node status);
            may be ``None`` (e.g. tests) - every ESP robot then reads Offline.
        parent: Optional parent widget.
    """

    def __init__(
        self,
        robots: list[BaseRobot],
        db: Database,
        gateway=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setupUi(self)

        self._robots = robots
        self._db = db
        self._gateway = gateway
        # Liveness reference: nodes heard from after this instant count as
        # alive. Refreshed by _start_status_scan just before the scan goes out.
        self._scan_ref = time.monotonic()

        for activity in available_activities(self._db):
            self.activity_combo.addItem(activity.name, userData=activity)

        # Simulation-mode checkbox - added programmatically right under the
        # activity dropdown in the form layout (parent form is the .ui file's
        # ``formLayout``). Toggling it just stores intent; the value is read
        # via ``simulation_mode`` after accept().
        self._sim_check = QCheckBox(
            "Run in simulation mode (no real hardware)"
        )
        self._sim_check.setToolTip(
            "When ticked, the selected activity runs against SimulatedRobot "
            "instances instead of the real ESP32 nodes. Useful for testing "
            "behaviors without the physical robots."
        )

        # Record-sensor-streams checkbox - same programmatic pattern. Records
        # every gateway message of the session to a JSONL file (no video); read
        # via ``record_streams`` after accept(). On by default; no effect in
        # simulation (no real gateway traffic).
        self._record_check = QCheckBox(
            "Record sensor streams (JSONL, for analysis)"
        )
        self._record_check.setChecked(True)
        self._record_check.setToolTip(
            "When ticked, all sensor messages of the session are saved to "
            "data/recordings/<session_id>.jsonl for later analysis and "
            "touch-gesture model training. No camera / video is involved."
        )
        parent = self.activity_combo.parentWidget()
        form = parent.layout() if parent is not None else None
        if isinstance(form, QFormLayout):
            # PySide6 stubs type getWidgetPosition's (row, role) tuple as object.
            base = form.getWidgetPosition(self.activity_combo)[0]  # pyright: ignore[reportIndexIssue]
            form.insertRow(base + 1, "", self._sim_check)
            form.insertRow(base + 2, "", self._record_check)
        elif form is not None:
            # Fallback: stash below the activity combo if the parent layout
            # isn't a form (e.g. after .ui refactors).
            form.addWidget(self._sim_check)
            form.addWidget(self._record_check)

        self.activity_combo.currentIndexChanged.connect(self._on_activity_changed)
        self.robots_list.itemChanged.connect(lambda _item: self._update_selection_warnings())
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        self.session_id_input.setText(db.next_session_id())

        self._populate_robots()
        self._on_activity_changed(0)
        self._populate_participants(db.get_all_participants())
        self._start_status_scan()

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
    def simulation_mode(self) -> bool:
        """True if the user ticked 'Run in simulation mode'."""
        return self._sim_check.isChecked()

    @property
    def record_streams(self) -> bool:
        """True if the user wants raw sensor streams recorded to JSONL."""
        return self._record_check.isChecked()

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
    # Activity -> target skin + mismatch warning
    # ------------------------------------------------------------------

    def _on_activity_changed(self, index: int) -> None:
        """Show the new activity's target skin and re-check skin mismatches."""
        activity: BaseActivity | None = self.activity_combo.itemData(index)
        skin = getattr(activity, "skin", None)
        self.target_skin_label.setText(
            skin_condition.label(skin) if skin else "Any")
        self._update_selection_warnings()

    def _update_selection_warnings(self) -> None:
        """Repaint the warning label and gate the OK button.

        Skin-variant mismatches only warn; robots sharing one board are a hard
        stop (see the module docstring)."""
        activity = self.selected_activity
        skin = getattr(activity, "skin", None)
        lines: list[str] = []
        clashes = node_sharing.conflicts(self.selected_robots)
        if clashes:
            lines.append(node_sharing.conflict_message(clashes))
        ok_btn = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(not clashes)
        if skin:
            for robot in self.selected_robots:
                mismatches = skin_condition.skin_mismatches(
                    skin, getattr(robot, "skins", {}).values())
                lines += [f"{robot.robot_id} - {m}" for m in mismatches]
            if lines:
                lines.insert(0, "WARNING: Skins don't match the activity's target "
                                f"({skin_condition.label(skin)}):")
        # Legacy robot-kind-targeted behaviours only run on one robot class;
        # ticking another would fail at start, so call it out here instead.
        robot_type = getattr(activity, "robot_type", BaseRobot)
        if robot_type is not BaseRobot:
            wrong = [r.robot_id for r in self.selected_robots
                     if not isinstance(r, robot_type)]
            if wrong:
                lines.append(
                    f"WARNING: This activity only runs on {robot_type.__name__}: "
                    + ", ".join(wrong))
        self.skin_warning_label.setText("\n".join(lines))
        self.skin_warning_label.setVisible(bool(lines))

    # ------------------------------------------------------------------
    # Robot list + live status
    # ------------------------------------------------------------------

    def _populate_robots(self) -> None:
        """Fill the list with every configured robot (status painted later)."""
        # {robot_id: other robots it shares a board with} - shown on the row so
        # the mutually exclusive pair is visible before ticking both.
        self._shares_board_with: dict[str, list[str]] = {}
        for rids in node_sharing.conflicts(self._robots).values():
            for rid in rids:
                others = [o for o in rids if o != rid]
                known = self._shares_board_with.setdefault(rid, [])
                known += [o for o in others if o not in known]
        self.robots_list.clear()
        if not self._robots:
            self.no_robots_label.setVisible(True)
            self.robots_list.setVisible(False)
            return
        self.no_robots_label.setVisible(False)
        self.robots_list.setVisible(True)
        for robot in self._robots:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, robot)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.robots_list.addItem(item)
        self._refresh_statuses()

    def _start_status_scan(self) -> None:
        """Scan the gateway's nodes and repaint statuses as answers arrive."""
        self._scan_ref = time.monotonic()
        if self._gateway is not None and self._gateway.is_connected:
            self._gateway.scan()
        self._refresh_ticks = 0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(_STATUS_REFRESH_MS)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._status_timer.start()

    def _on_status_tick(self) -> None:
        self._refresh_ticks += 1
        self._refresh_statuses()
        if self._refresh_ticks >= _STATUS_REFRESH_TICKS:
            self._status_timer.stop()

    def _refresh_statuses(self) -> None:
        """Repaint every robot row's status dot/label (ticks left alone)."""
        for i in range(self.robots_list.count()):
            item = self.robots_list.item(i)
            robot = item.data(Qt.ItemDataRole.UserRole)
            if robot is None:
                continue
            status, detail = self._robot_status(robot)
            dot = "*" if status in ("online", "ready", "partial") else "o"
            kind = getattr(robot, "robot_kind", "") or "?"
            shared = self._shares_board_with.get(robot.robot_id, [])
            share_note = f"  (shares its board with {', '.join(shared)})" if shared else ""
            item.setText(f"{dot} {robot.robot_id}  [{kind.capitalize()}]"
                         f"  -  {detail}{share_note}")
            item.setForeground(_STATUS_COLORS.get(status, QColor("#cc2222")))

    def _robot_status(self, robot: BaseRobot) -> tuple[str, str]:
        """``(status, human detail)`` for a robot row.

        Node-owning robots are judged by which of THEIR nodes answered the
        scan (``nodes_seen_since``); a node-less wireless Thymio is "ready"
        whenever its transport (the gateway link) is up.
        """
        gateway_up = self._gateway is not None and self._gateway.is_connected
        seen = getattr(robot, "nodes_seen_since", None)
        alive, total = seen(self._scan_ref) if seen is not None else (0, 0)
        if total == 0:
            if getattr(robot, "gateway", None) is not None and gateway_up:
                return "ready", "Ready (no nodes; transport up)"
            return "offline", ("Offline (gateway disconnected)"
                               if not gateway_up else "Offline")
        if not gateway_up:
            return "offline", "Offline (gateway disconnected)"
        if alive == total:
            return "online", f"Online ({alive}/{total} nodes)"
        if alive > 0:
            return "partial", f"Partial ({alive}/{total} nodes)"
        return "offline", f"Offline (0/{total} nodes)"

    # ------------------------------------------------------------------
    # Participants
    # ------------------------------------------------------------------

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

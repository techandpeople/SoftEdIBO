"""Non-modal floating panel for on-touch skin-to-participant assignment."""

from __future__ import annotations

import shutil
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from src.data.models import ParticipantRecord
from src.gui.ui_touch_assignment_panel import Ui_TouchAssignmentPanel


class _SkinRow(QFrame):
    """One row representing a single skin and its assignment status."""

    def __init__(self, skin_id: str, skin_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.skin_id = skin_id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        self._name_label = QLabel(f"<b>{skin_name}</b>")
        self._status_label = QLabel("Waiting for touch…")
        self._status_label.setStyleSheet("color: #888;")
        layout.addWidget(self._name_label, stretch=1)
        layout.addWidget(self._status_label, stretch=2)

    def set_active(self, active: bool) -> None:
        if active:
            self.setStyleSheet("background-color: #fff3cd; border-radius: 4px;")
        else:
            self.setStyleSheet("")

    def set_assigned(self, participant_id: str) -> None:
        self._status_label.setText(f"→ {participant_id}")
        self._status_label.setStyleSheet("color: #2a7a2a; font-weight: bold;")
        self.setStyleSheet("background-color: #d4edda; border-radius: 4px;")


class TouchAssignmentPanel(QWidget, Ui_TouchAssignmentPanel):
    """Floating panel that queues unassigned touches and lets the operator
    identify who touched each skin.

    Opens when the session starts with unassigned skins and closes automatically
    once every skin has a real participant assigned.

    Signals:
        assigned(skin_id, participant_id): skin was assigned to a participant.
        skipped(skin_id): first queued touch for this skin was skipped (log as unknown).
    """

    assigned = Signal(str, str)   # skin_id, participant_id
    skipped = Signal(str)         # skin_id

    def __init__(
        self,
        skins: list[tuple[str, str]],           # (skin_id, skin_name)
        participants: list[ParticipantRecord],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setupUi(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._skins: dict[str, str] = dict(skins)        # skin_id -> skin_name
        self._participants = participants
        self._assigned: set[str] = set()                  # skin_ids with a real assignment
        self._queue: list[str] = []                       # ordered skin_ids waiting for assignment

        # Build skin rows into the scroll area
        self._rows: dict[str, _SkinRow] = {}
        for skin_id, skin_name in skins:
            row = _SkinRow(skin_id, skin_name)
            self._rows[skin_id] = row
            self.skin_rows_layout.addWidget(row)
        self.skin_rows_layout.addStretch()

        # Build participant buttons + skip into btn_row
        for p in participants:
            label = p.participant_id
            if p.alias:
                label += f" ({p.alias})"
            btn = QPushButton(label)
            btn.setMinimumHeight(36)
            btn.clicked.connect(
                lambda _=False, pid=p.participant_id: self._on_assign(pid)
            )
            self.btn_layout.addWidget(btn)
        skip_btn = QPushButton("Skip for now")
        skip_btn.setMinimumHeight(36)
        skip_btn.setStyleSheet("color: #888;")
        skip_btn.clicked.connect(self._on_skip)
        self.btn_layout.addWidget(skip_btn)

        self.btn_row.hide()
        self.pending_label.setStyleSheet("color: #888; font-style: italic;")

        self.show()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, skin_id: str) -> None:
        """Register a touch on an unassigned skin.

        Always plays the alert to notify the operator of a new touch.
        If the skin is already in the queue, the prompt label also flashes red.
        """
        already_pending = skin_id in self._queue
        self._warn_retouched(flash=already_pending)
        if not already_pending:
            self._queue.append(skin_id)
            self._update_display()

    def is_queued(self, skin_id: str) -> bool:
        """Return True if this skin already has a pending queue entry."""
        return skin_id in self._queue

    def mark_pre_assigned(self, skin_id: str, participant_id: str) -> None:
        """Mark a skin as already assigned (from pre-session dialog)."""
        self._assigned.add(skin_id)
        if skin_id in self._rows:
            self._rows[skin_id].set_assigned(participant_id)
        self._check_done()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update_display(self) -> None:
        if not self._queue:
            self.prompt_label.setText("Touch a skin to assign it.")
            self.btn_row.hide()
            self.pending_label.hide()
            for row in self._rows.values():
                row.set_active(False)
            self._check_done()
            return

        current_skin = self._queue[0]
        skin_name = self._skins.get(current_skin, current_skin)
        self.prompt_label.setText(f"Who touched <b>{skin_name}</b>?")
        self.btn_row.show()

        pending_count = len(self._queue) - 1
        if pending_count > 0:
            self.pending_label.setText(f"({pending_count} more pending)")
            self.pending_label.show()
        else:
            self.pending_label.hide()

        for skin_id, row in self._rows.items():
            if skin_id not in self._assigned:
                row.set_active(skin_id == current_skin)

    def _on_assign(self, participant_id: str) -> None:
        if not self._queue:
            return
        skin_id = self._queue[0]
        self._queue = [s for s in self._queue if s != skin_id]
        self._assigned.add(skin_id)
        if skin_id in self._rows:
            self._rows[skin_id].set_assigned(participant_id)
        self.assigned.emit(skin_id, participant_id)
        self._update_display()

    def _on_skip(self) -> None:
        if not self._queue:
            return
        skin_id = self._queue.pop(0)
        if skin_id in self._rows:
            self._rows[skin_id].set_active(False)
        self.skipped.emit(skin_id)
        self._update_display()

    def _warn_retouched(self, flash: bool = True) -> None:
        """Alert the operator of a touch on an unassigned skin.

        Args:
            flash: If True, also flash the prompt label red (used when the skin
                   is already pending, to signal a duplicate touch).
        """
        if flash:
            self.prompt_label.setStyleSheet("background-color: #ff6b6b; color: white; border-radius: 4px;")
            QTimer.singleShot(600, lambda: self.prompt_label.setStyleSheet(""))
        self.raise_()
        self.activateWindow()
        # Audio: try system bell then common Linux sound players as fallback
        QApplication.beep()
        if sys.platform != "win32":
            for cmd in (
                ["pw-play", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
                ["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
            ):
                if shutil.which(cmd[0]):
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break

    def _check_done(self) -> None:
        if not self._queue and self._assigned.issuperset(self._skins):
            self.close()

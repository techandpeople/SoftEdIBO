"""Observer quick-tag panel — live behavioral coding during a session.

The researcher chose to run the study **without video recording** (decision in
docs/STUDY_PLAN.md), so behaviors the sensors can't see — a child watching,
pointing, helping a peer, talking — are coded live instead. This floating panel
gives the observer one button per behavior code, per participant: a click logs
a timestamped ``observer`` event into the same ``interaction_events`` timeline
as the sensor events, so everything lines up for analysis.

It also carries a **Marker** button that logs a ``marker`` event (with an
optional note) — a single clapperboard click to align the event log with the
observer's paper notes or any external clock.

The static frame (intro label + marker button) lives in ``ui/observer_panel.ui``;
the per-participant behavior-code boxes and the gesture row are built dynamically
from the session's participant list and a configurable list of behavior codes
into ``content_layout``. It only *emits* events via the ``event`` signal;
persistence is the SessionPanel's job, so this panel stays free of any database
dependency.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QPushButton,
    QWidget,
)

from src.data.models import ParticipantRecord
from src.gui.ui_observer_panel import Ui_ObserverPanel

# Default behavior codes. Kept short and observable; the researcher can refine
# them later (a future settings entry could make this list configurable without
# touching code).
DEFAULT_BEHAVIOR_CODES: tuple[str, ...] = (
    "watches",
    "points",
    "helps",
    "talks",
    "takes_turn",
    "withdraws",
)


class ObserverPanel(QWidget, Ui_ObserverPanel):
    """Floating panel of per-participant behavior-code buttons + a marker.

    Signals:
        event(type, action, target, metadata): a coded observation. ``type`` is
            ``"observer"`` (behavior codes) or ``"marker"``; ``action`` is the
            behavior code or ``"mark"``; ``target`` is the participant_id (empty
            for a session-wide marker); ``metadata`` carries an optional note.
    """

    event = Signal(str, str, str, str)   # type, action, target, metadata

    def __init__(
        self,
        participants: list[ParticipantRecord],
        behavior_codes: tuple[str, ...] = DEFAULT_BEHAVIOR_CODES,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setupUi(self)
        self._codes = tuple(behavior_codes)

        # One group box per participant, each with a grid of code buttons.
        for participant in participants:
            box = QGroupBox(self._participant_label(participant))
            grid = QGridLayout(box)
            for i, code in enumerate(self._codes):
                btn = QPushButton(code.replace("_", " "))
                btn.setMinimumWidth(96)
                # Bind both the participant id and the code per button.
                btn.clicked.connect(
                    lambda _=False, pid=participant.participant_id, c=code:
                    self._emit_observation(pid, c))
                grid.addWidget(btn, i // 3, i % 3)
            self.content_layout.addWidget(box)

        # Touch-gesture labelling row — tags the touch happening *now* with a
        # gesture class, for the offline labeller (scripts/label_touches.py) to
        # align with the recorded stream and build a training set. Logged as a
        # ``gesture_label`` event; the offline tool resolves the exact segment.
        from src.ml.gesture_taxonomy import DEFINITIONS, GESTURE_CLASSES
        gesture_box = QGroupBox("Touch gesture (label the current touch)")
        gesture_row = QHBoxLayout(gesture_box)
        for code in GESTURE_CLASSES:
            btn = QPushButton(code)
            btn.setToolTip(DEFINITIONS.get(code, ""))
            btn.clicked.connect(lambda _=False, c=code: self._emit_gesture(c))
            gesture_row.addWidget(btn)
        gesture_row.addStretch(1)
        self.content_layout.addWidget(gesture_box)

        # Session-wide marker button (defined in the .ui).
        self.marker_btn.clicked.connect(self._emit_marker)

    @staticmethod
    def _participant_label(participant: ParticipantRecord) -> str:
        alias = getattr(participant, "alias", "") or participant.participant_id
        return f"{alias}  [{participant.participant_id}]"

    def _emit_observation(self, participant_id: str, code: str) -> None:
        self.event.emit("observer", code, participant_id, "")

    def _emit_gesture(self, code: str) -> None:
        # target left empty — the offline labeller aligns this timestamp to the
        # active touch segment (and thus its skin) in the recorded stream.
        self.event.emit("gesture_label", code, "", "")

    def _emit_marker(self) -> None:
        note, ok = QInputDialog.getText(
            self, "Marker", "Optional note for this marker:")
        if not ok:
            return
        self.event.emit("marker", "mark", "", note.strip())

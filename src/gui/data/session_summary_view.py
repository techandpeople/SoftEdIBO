"""SessionSummaryView - facts about a session and the researcher's review."""

from __future__ import annotations

import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from src.core.session_snapshot import KIND_FIRMWARE, KIND_TOUCH
from src.data.interaction_model import InteractionModel
from src.data.models import SESSION_STATUSES, SessionAnnotation
from src.data.session_data import SessionData
from src.gui.ui_session_summary_view import Ui_SessionSummaryView

_NONE = "-"
_SURFACE_TEXT = {
    "shared": "one skin shared by the children",
    "individual_shared": "own skin each, on the same robot",
    "individual_separate": "own robot each",
}


class SessionSummaryView(QWidget, Ui_SessionSummaryView):
    """Shows one session and lets the researcher annotate it.

    Emits ``annotation_saved`` with a :class:`SessionAnnotation`; storing it
    is the owner's job.
    """

    annotation_saved = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._session_id: str | None = None
        for status in SESSION_STATUSES:
            self.status_combo.addItem(status.capitalize(), status)
        self.save_btn.clicked.connect(self._on_save)
        self.show_session(None, None)

    def show_session(self, data: SessionData | None,
                     model: InteractionModel | None) -> None:
        """Show ``data`` (blank and disabled when no session is selected)."""
        self._session_id = data.session.session_id if data else None
        self.review_group.setEnabled(data is not None)
        if data is None or model is None:
            for label in (self.activity_value, self.when_value, self.robots_value,
                          self.participants_value, self.outcome_value):
                label.setText(_NONE)
            self.conditions_text.setPlainText("")
            self._show_annotation(None)
            return
        row = model.session().rows[0]
        self.activity_value.setText(str(row["activity"]) or _NONE)
        self.when_value.setText(
            f"{data.session.start_time:%Y-%m-%d %H:%M:%S}, "
            f"{self._duration_text(row['duration_s'])}")
        surface = _SURFACE_TEXT.get(str(row["surface"]), "")
        self.robots_value.setText(
            ", ".join(part for part in (str(row["robot_kinds"]), surface) if part)
            or _NONE)
        self.participants_value.setText(self._participants_text(data))
        self.outcome_value.setText(self._outcome_text(row))
        self.conditions_text.setPlainText(self._conditions_text(data))
        self._show_annotation(data.annotation)

    # ------------------------------------------------------------------

    @staticmethod
    def _duration_text(seconds) -> str:
        total = int(seconds or 0)
        return f"{total // 60} min {total % 60:02d} s"

    @staticmethod
    def _participants_text(data: SessionData) -> str:
        names = {p.participant_id: p.alias for p in data.participants}
        lines = [
            f"{a.participant_id} ({names.get(a.participant_id, '')}): "
            f"{a.robot_id} [{', '.join(a.unit_ids) or 'whole robot'}]"
            for a in data.assignments]
        assigned = {a.participant_id for a in data.assignments}
        lines += [f"{pid} ({alias}): not assigned"
                  for pid, alias in names.items() if pid not in assigned]
        return "\n".join(lines) or _NONE

    def _outcome_text(self, row: dict) -> str:
        parts = [f"touches: {row['touches']}",
                 f"phase changes: {row['milestones']}"]
        if row["units"]:
            finished = f"{row['units_finished']} of {row['units']} finished"
            if row["time_to_finish_ms"] is not None:
                finished += (" after "
                             + self._duration_text(row["time_to_finish_ms"] / 1000))
            parts.append(finished)
        return ", ".join(parts)

    @staticmethod
    def _conditions_text(data: SessionData) -> str:
        sections = []
        firmware = data.snapshots.get(KIND_FIRMWARE) or {}
        if firmware:
            sections.append("Firmware\n" + "\n".join(
                f"  {mac}: {marker or 'not reported'}"
                for mac, marker in sorted(firmware.items())))
        touch = data.snapshots.get(KIND_TOUCH) or {}
        if touch:
            sections.append("Touch settings\n" + json.dumps(
                touch, indent=2, sort_keys=True))
        return "\n\n".join(sections)

    def _show_annotation(self, annotation: SessionAnnotation | None) -> None:
        status = annotation.status if annotation else SESSION_STATUSES[0]
        self.status_combo.setCurrentIndex(max(0, self.status_combo.findData(status)))
        self.group_edit.setText(annotation.group_label if annotation else "")
        self.order_spin.setValue(
            annotation.visit_order or 0 if annotation else 0)
        self.notes_edit.setPlainText(annotation.notes if annotation else "")

    def _on_save(self) -> None:
        if self._session_id is None:
            return
        self.annotation_saved.emit(SessionAnnotation(
            session_id=self._session_id,
            status=self.status_combo.currentData(),
            group_label=self.group_edit.text().strip(),
            visit_order=self.order_spin.value() or None,
            notes=self.notes_edit.toPlainText().strip()))

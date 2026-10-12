"""SessionEventsView - a session's raw events, readable and correctable."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QTableWidgetItem, QWidget

from src.data.models import EventCorrection, InteractionEvent
from src.data.session_data import SessionData, event_details
from src.gui.ui_session_events_view import Ui_SessionEventsView

_ALL_TYPES = ""
_COL_CORRECTION = 7


def _details_text(event: InteractionEvent) -> str:
    """Metadata as ``key = value`` pairs (free text is shown as is)."""
    details = event_details(event)
    if not details:
        return event.metadata or ""
    return "; ".join(f"{key} = {value}" for key, value in details.items())


def _correction_text(correction: EventCorrection | None) -> str:
    if correction is None:
        return ""
    parts = []
    if correction.excluded:
        parts.append("excluded")
    if correction.participant_id:
        parts.append(f"attributed to {correction.participant_id}")
    if correction.note:
        parts.append(correction.note)
    return ", ".join(parts)


class SessionEventsView(QWidget, Ui_SessionEventsView):
    """Lists one session's events and lets the researcher correct them.

    Emits ``corrections_requested`` (a list of :class:`EventCorrection`) and
    ``corrections_cleared`` (a list of event ids); storing them is the
    owner's job.
    """

    corrections_requested = Signal(object)
    corrections_cleared = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._data: SessionData | None = None
        header = self.events_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.type_filter.currentIndexChanged.connect(self._fill)
        self.events_table.itemSelectionChanged.connect(self._update_buttons)
        self.reassign_btn.clicked.connect(self._on_reassign)
        self.exclude_btn.clicked.connect(self._on_exclude)
        self.clear_btn.clicked.connect(self._on_clear)
        self.show_session(None)

    def show_session(self, data: SessionData | None) -> None:
        """Show ``data``'s events (an empty table when none is selected)."""
        self._data = data
        wanted = self.type_filter.currentData()
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem("All events", _ALL_TYPES)
        for kind in sorted({e.type for e in data.events} if data else ()):
            self.type_filter.addItem(kind, kind)
        self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(wanted)))
        self.type_filter.blockSignals(False)
        self.participant_combo.clear()
        for participant in (data.participants if data else ()):
            self.participant_combo.addItem(
                f"{participant.participant_id} ({participant.alias})",
                participant.participant_id)
        self._fill()

    def selected_event_ids(self) -> list[int]:
        """Ids of the events whose rows are selected."""
        rows = sorted({i.row() for i in self.events_table.selectedItems()})
        ids = []
        for row in rows:
            item = self.events_table.item(row, 0)
            event_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            if event_id is not None:
                ids.append(int(event_id))
        return ids

    # ------------------------------------------------------------------

    def _fill(self) -> None:
        table = self.events_table
        table.setRowCount(0)
        data = self._data
        if data is None:
            self._update_buttons()
            return
        wanted = self.type_filter.currentData() or _ALL_TYPES
        start = data.session.start_time
        for event in sorted(data.events, key=lambda e: e.timestamp):
            if wanted and event.type != wanted:
                continue
            correction = (data.corrections.get(event.event_id)
                          if event.event_id is not None else None)
            row = table.rowCount()
            table.insertRow(row)
            cells = (
                event.timestamp.isoformat(sep=" ", timespec="milliseconds"),
                f"{(event.timestamp - start).total_seconds():.3f}",
                event.participant_id, event.type, event.action, event.target,
                _details_text(event), _correction_text(correction))
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if correction is not None and correction.excluded:
                    item.setForeground(self.palette().placeholderText())
                table.setItem(row, column, item)
            first = table.item(row, 0)
            if first is not None:
                first.setData(Qt.ItemDataRole.UserRole, event.event_id)
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = bool(self.selected_event_ids())
        self.reassign_btn.setEnabled(
            selected and self.participant_combo.count() > 0)
        self.exclude_btn.setEnabled(selected)
        self.clear_btn.setEnabled(selected)

    def _existing(self, event_id: int) -> EventCorrection:
        """The event's current correction, or a blank one to build on."""
        assert self._data is not None
        current = self._data.corrections.get(event_id)
        if current is not None:
            return replace(current)        # a copy: the loaded data stays as is
        return EventCorrection(event_id=event_id,
                               session_id=self._data.session.session_id)

    def _on_reassign(self) -> None:
        participant = self.participant_combo.currentData()
        if self._data is None or not participant:
            return
        corrections = [self._existing(i) for i in self.selected_event_ids()]
        for correction in corrections:
            correction.participant_id = participant
        self.corrections_requested.emit(corrections)

    def _on_exclude(self) -> None:
        if self._data is None:
            return
        corrections = [self._existing(i) for i in self.selected_event_ids()]
        for correction in corrections:
            correction.excluded = True
        self.corrections_requested.emit(corrections)

    def _on_clear(self) -> None:
        self.corrections_cleared.emit(self.selected_event_ids())

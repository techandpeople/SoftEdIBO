"""Data panel: review, annotate and export the recorded sessions."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QMessageBox, QTreeWidgetItem, QWidget

from src.config.settings import Settings
from src.core.session_trash import SessionTrash
from src.data import interaction_model as im
from src.data.database import Database
from src.data.models import SESSION_STATUSES
from src.data.session_data import SessionData, load_session_data
from src.data.session_index import NO_GROUP, SessionListRow, build_session_rows
from src.data.timeline import build_timeline
from src.gui.trash_dialog import TrashDialog
from src.gui.ui_data_panel import Ui_DataPanel

_SOURCE_LABELS = (
    (im.SOURCE_PARTICIPANT, "Participant"),
    (im.SOURCE_SKIN, "Skin"),
    (im.SOURCE_SENSOR, "Sensor"),
)
_ALL_STATUSES = ""
_LIST_WIDTH = 520


class DataPanel(QWidget, Ui_DataPanel):
    """Panel for reviewing, annotating, exporting and deleting sessions.

    The list on the left groups the sessions by group of children; the views
    on the right show the selected one. Deletion is recoverable: "Delete
    Session" moves a session to the trash, and "Trash..." restores or
    permanently deletes it.

    Args:
        db: Open database instance to load sessions and events from.
    """

    def __init__(self, db: Database):
        super().__init__()
        self.setupUi(self)

        self._db = db
        self._trash = SessionTrash(db, Settings().recordings_dir)
        self._rows: list[SessionListRow] = []
        self._current: SessionData | None = None
        # The Patterns view pools every selected session, which is costly:
        # it is only recomputed while that view is the one shown.
        self._patterns_stale = True

        # The session list needs room for its seven columns; the views take
        # the rest and grow with the window.
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([_LIST_WIDTH, 2 * _LIST_WIDTH])
        self.sessions_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)

        self.status_filter.addItem("All statuses", _ALL_STATUSES)
        for status in SESSION_STATUSES:
            self.status_filter.addItem(status.capitalize(), status)
        for key, label in _SOURCE_LABELS:
            self.source_combo.addItem(label, key)

        self.filter_edit.textChanged.connect(self._fill_tree)
        self.status_filter.currentIndexChanged.connect(self._fill_tree)
        self.source_combo.currentIndexChanged.connect(self._show_current)
        self.views_tabs.currentChanged.connect(self._refresh_patterns)
        self.sessions_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.summary_view.annotation_saved.connect(self._on_annotation_saved)
        self.events_view.corrections_requested.connect(self._on_corrections)
        self.events_view.corrections_cleared.connect(self._on_corrections_cleared)
        self.export_btn.clicked.connect(self._on_export)
        self.delete_btn.clicked.connect(self._on_delete)
        self.trash_btn.clicked.connect(self._on_open_trash)

        self.refresh()

    # ------------------------------------------------------------------
    # Session list
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload every session from the database, keeping the selection."""
        self._rows = build_session_rows(self._db)
        self._fill_tree()

    def selected_session_ids(self) -> list[str]:
        """Ids of the selected sessions (a selected group selects its
        sessions), in list order."""
        ids: list[str] = []
        for item in self.sessions_tree.selectedItems():
            children = [item.child(i) for i in range(item.childCount())]
            for leaf in children or [item]:
                session_id = leaf.data(0, Qt.ItemDataRole.UserRole)
                if session_id and session_id not in ids:
                    ids.append(session_id)
        return ids

    def select_session(self, session_id: str) -> bool:
        """Select one session in the list. False if it is not listed."""
        for leaf in self._session_items():
            if leaf.data(0, Qt.ItemDataRole.UserRole) == session_id:
                self.sessions_tree.clearSelection()
                self.sessions_tree.setCurrentItem(leaf)
                return True
        return False

    def group_labels(self) -> list[str]:
        """The group headings currently listed, top to bottom."""
        return [group.text(0) for group in self._group_items()]

    def listed_session_ids(self, group: str | None = None) -> list[str]:
        """Sessions currently listed (after filtering), top to bottom;
        ``group`` narrows it to one group heading."""
        return [leaf.data(0, Qt.ItemDataRole.UserRole)
                for leaf in self._session_items(group)]

    def session_cell(self, session_id: str, column: int) -> str:
        """Text of one column of a listed session ("" if not listed)."""
        for leaf in self._session_items():
            if leaf.data(0, Qt.ItemDataRole.UserRole) == session_id:
                return leaf.text(column)
        return ""

    def _group_items(self) -> list[QTreeWidgetItem]:
        tree = self.sessions_tree
        items = (tree.topLevelItem(i) for i in range(tree.topLevelItemCount()))
        return [item for item in items if item is not None]

    def _session_items(self, group: str | None = None) -> list[QTreeWidgetItem]:
        return [heading.child(i) for heading in self._group_items()
                if group is None or heading.text(0) == group
                for i in range(heading.childCount())]

    def _fill_tree(self) -> None:
        keep = self.selected_session_ids()
        text = self.filter_edit.text()
        status = self.status_filter.currentData() or _ALL_STATUSES
        tree = self.sessions_tree
        tree.blockSignals(True)
        tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for row in self._rows:
            if not row.matches(text, status):
                continue
            label = row.group or NO_GROUP
            group = groups.get(label)
            if group is None:
                group = QTreeWidgetItem(tree, [label])
                group.setFirstColumnSpanned(True)
                groups[label] = group
            leaf = QTreeWidgetItem(group, [
                row.session_id, row.activity,
                row.start.isoformat(sep=" ", timespec="seconds"),
                f"{int(row.duration_s) // 60}:{int(row.duration_s) % 60:02d}",
                str(row.children), row.status, row.finished])
            leaf.setData(0, Qt.ItemDataRole.UserRole, row.session_id)
            leaf.setSelected(row.session_id in keep)
        tree.expandAll()
        tree.blockSignals(False)
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        ids = self.selected_session_ids()
        self.delete_btn.setEnabled(bool(ids))
        current_id = self._current.session.session_id if self._current else None
        wanted = ids[0] if ids else None
        if wanted != current_id or wanted is None:
            self._load(wanted)
        else:
            self._mark_patterns_stale()

    # ------------------------------------------------------------------
    # Selected session
    # ------------------------------------------------------------------

    def _load(self, session_id: str | None) -> None:
        self._current = (load_session_data(self._db, session_id)
                         if session_id else None)
        self._show_current()

    def _show_current(self) -> None:
        data = self._current
        model = (im.InteractionModel(data, self.source_combo.currentData())
                 if data is not None else None)
        self.summary_view.show_session(data, model)
        self.timeline_view.show_timeline(
            build_timeline(data, model) if data and model else None)
        self.children_view.show_session(model)
        self.events_view.show_session(data)
        self._mark_patterns_stale()

    def _mark_patterns_stale(self) -> None:
        self._patterns_stale = True
        self._refresh_patterns()

    def _refresh_patterns(self) -> None:
        """Recompute the pooled Patterns view if it is showing and stale."""
        if (not self._patterns_stale
                or self.views_tabs.currentWidget() is not self.patterns_view):
            return
        self._patterns_stale = False
        sessions = [data for data in (
            load_session_data(self._db, session_id)
            for session_id in self.selected_session_ids()) if data is not None]
        self.patterns_view.show_sessions(sessions, self.source_combo.currentData())

    def _reload_current(self) -> None:
        if self._current is not None:
            self._load(self._current.session.session_id)

    def _on_annotation_saved(self, annotation) -> None:
        self._db.save_session_annotation(annotation)
        self._reload_current()
        self.refresh()      # status / group changed: the list regroups

    def _on_corrections(self, corrections) -> None:
        for correction in corrections:
            self._db.save_event_correction(correction)
        self._reload_current()

    def _on_corrections_cleared(self, event_ids) -> None:
        for event_id in event_ids:
            self._db.delete_event_correction(event_id)
        self._reload_current()

    # ------------------------------------------------------------------
    # Export / delete / trash
    # ------------------------------------------------------------------

    def _on_export(self) -> None:
        """Open the export dialog for the selected (or all) sessions."""
        from src.gui.research_export_dialog import ResearchExportDialog
        self._db.flush_events()
        ResearchExportDialog(
            self._db, self._rows, self.selected_session_ids(),
            source_key=self.source_combo.currentData(), parent=self).exec()

    def _on_delete(self) -> None:
        """Move the selected session(s) to the trash (recoverable)."""
        session_ids = self.selected_session_ids()
        if not session_ids:
            QMessageBox.warning(self, "Delete", "Select a session first.")
            return

        if len(session_ids) == 1:
            activity = next((r.activity for r in self._rows
                             if r.session_id == session_ids[0]), "")
            text = f"Move session {session_ids[0]} ({activity}) to the trash?"
        else:
            text = f"Move {len(session_ids)} sessions to the trash?"

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Delete Session")
        confirm.setText(text)
        confirm.setInformativeText(
            "It leaves the list and can be restored (or deleted for good) from "
            "the Trash button."
        )
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        confirm.setDefaultButton(QMessageBox.StandardButton.Yes)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        for session_id in session_ids:
            self._trash.trash(session_id)
        self.refresh()

    def _on_open_trash(self) -> None:
        """Open the trash dialog, then refresh in case a session was restored."""
        TrashDialog(self._trash, self).exec()
        self.refresh()

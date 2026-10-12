"""ResearchExportDialog - choose what to export and write the workbook."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from src.data import interaction_model as im
from src.data.database import Database
from src.data.research_export import ExportOptions, ResearchExporter
from src.data.session_index import SessionListRow
from src.gui.base_dialog import BaseDialog
from src.gui.ui_research_export_dialog import Ui_ResearchExportDialog

_SOURCE_LABELS = (
    (im.SOURCE_PARTICIPANT, "Participant"),
    (im.SOURCE_SKIN, "Skin"),
    (im.SOURCE_SENSOR, "Sensor"),
)


class ResearchExportDialog(BaseDialog, Ui_ResearchExportDialog):
    """Exports sessions to an Excel workbook for analysis.

    Args:
        db: Open database to read from.
        rows: Every listed session (used to count what will be exported).
        selected_ids: Sessions selected in the Data tab.
        source_key: Initial touch grouping (the one shown in the Data tab).
    """

    def __init__(self, db: Database, rows: list[SessionListRow],
                 selected_ids: list[str], source_key: str = im.SOURCE_PARTICIPANT,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._exporter = ResearchExporter(db)
        self._rows = rows
        self._selected_ids = list(selected_ids)

        for key, label in _SOURCE_LABELS:
            self.source_combo.addItem(label, key)
        self.source_combo.setCurrentIndex(
            max(0, self.source_combo.findData(source_key)))
        self.selected_radio.setEnabled(bool(self._selected_ids))
        (self.selected_radio if self._selected_ids else self.all_radio).setChecked(True)

        for control in (self.selected_radio, self.all_radio):
            control.toggled.connect(self._update_summary)
        for check in (self.include_test_check, self.include_excluded_check):
            check.toggled.connect(self._update_summary)
        self.cancel_btn.clicked.connect(self.reject)
        self.export_btn.clicked.connect(self._on_export)
        self._update_summary()

    def options(self) -> ExportOptions:
        """The export options as currently chosen."""
        return ExportOptions(
            source_key=self.source_combo.currentData(),
            include_test=self.include_test_check.isChecked(),
            include_excluded=self.include_excluded_check.isChecked())

    def session_ids(self) -> list[str]:
        """The sessions the current choices export, oldest first."""
        options = self.options()
        wanted = (set(self._selected_ids) if self.selected_radio.isChecked()
                  else None)
        rows = [row for row in self._rows
                if (wanted is None or row.session_id in wanted)
                and options.accepts(row.status)]
        return [row.session_id for row in sorted(rows, key=lambda r: r.start)]

    def export_to(self, path: str | Path) -> int:
        """Write the workbook to ``path``. Returns the sessions exported."""
        return self._exporter.export(self.session_ids(), path, self.options())

    def _update_summary(self) -> None:
        count = len(self.session_ids())
        self.summary_label.setText(
            f"{count} session(s) will be exported." if count
            else "No session matches these choices.")
        self.export_btn.setEnabled(count > 0)

    def _on_export(self) -> None:
        ids = self.session_ids()
        docs = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        name = (f"SoftEdIBO_{ids[0]}.xlsx" if len(ids) == 1
                else "SoftEdIBO_sessions.xlsx")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export for Analysis", str(Path(docs) / name),
            "Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            written = self.export_to(path)
        except ImportError:
            QMessageBox.critical(
                self, "Export for Analysis",
                "The Excel export needs the 'openpyxl' package, which is not "
                "installed.")
            return
        except OSError as error:
            QMessageBox.critical(
                self, "Export for Analysis",
                f"Could not write {path}:\n{error}")
            return
        QMessageBox.information(
            self, "Export for Analysis",
            f"Exported {written} session(s) to {path}")
        self.accept()

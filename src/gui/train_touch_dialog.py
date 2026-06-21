"""Touch Gestures dialog — label recordings and train models, all in the app.

End-to-end, no command line:

1. **Add recording** — pick a session JSONL. Its skin type is read from the
   recording header and selected automatically (no manual tagging); segments are
   pre-filled with the live labels tapped during the session.
2. **Edit** the gesture label of each touch segment in the table (a dropdown of
   gesture classes). Group several touches into one gesture (e.g. a triple tap).
   Import / Export CSV to hand-edit or share datasets.
3. **Train** — fits one model per ``skin_type`` across every loaded recording,
   so loading recordings of several types trains all their models in one pass.

The heavy lifting lives in ``src/ml/labeling.py`` (segment + align + CSV) and
``src/ml/training.py`` (fit). scikit-learn is the optional ``ml`` extra; if it's
missing, Train says so instead of failing.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from src.config.settings import Settings
from src.gui.async_task import run_async
from src.gui.ui_train_touch_dialog import Ui_TrainTouchDialog
from src.ml import labeling
from src.ml.gesture_taxonomy import GESTURE_CLASSES
from src.ml.touch_classifier import model_path
from src.hardware.skin_geometry import known_skin_types


class TrainTouchDialog(QDialog, Ui_TrainTouchDialog):
    """Label recorded touch segments and train per-skin-type gesture models."""

    # Emitted from the training worker thread; delivered (queued) on the GUI
    # thread so log lines can safely touch the QPlainTextEdit.
    _train_log = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        # recording path → list[LabelRow] (insertion order matches the list widget)
        self._recordings: dict[str, list[labeling.LabelRow]] = {}

        self.splitter.setSizes([240, 600])

        # Skin type combo: blank entry + the known skin types.
        self.type_combo.addItem("", "")
        for st in known_skin_types():
            self.type_combo.addItem(st, st)

        # Table behaviour not expressible in the .ui.
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)

        # Wiring.
        self.rec_list.currentRowChanged.connect(self._show_selected)
        self.add_btn.clicked.connect(self._add_recording)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.group_btn.clicked.connect(self._group_selected)
        self.ungroup_btn.clicked.connect(self._ungroup_selected)
        self.import_csv_btn.clicked.connect(self._import_csv)
        self.export_csv_btn.clicked.connect(self._export_csv)
        self.import_model_btn.clicked.connect(self._import_model)
        self.export_model_btn.clicked.connect(self._export_model)
        self.train_btn.clicked.connect(self._train)
        self._train_log.connect(self.log.appendPlainText)

    # ------------------------------------------------------------------
    # Recordings
    # ------------------------------------------------------------------

    def _add_recording(self) -> None:
        rec_dir = Settings().recordings_dir
        start = str(rec_dir if rec_dir.exists() else Settings.ROOT)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select recording", start, "Recordings (*.jsonl)")
        if not path or path in self._recordings:
            return
        p = Path(path)
        # Parsing the .jsonl plus the optional DB lookup can stall on big
        # recordings, so load off the GUI thread and populate when it's ready.
        self.log.appendPlainText(f"Loading {p.name}…")
        run_async(
            lambda: self._load_recording(p),
            on_done=lambda result, pp=path: self._on_recording_loaded(pp, *result),
            on_error=lambda exc, pp=path: self.log.appendPlainText(
                f"Failed to load {Path(pp).name}: {exc}"),
            parent=self,
        )

    @staticmethod
    def _load_recording(p: Path):
        """Worker-thread part of loading a recording (no GUI access)."""
        source_types = labeling.skin_types_of(p)
        source_variants = labeling.skin_variants_of(p)
        try:
            events = labeling.gesture_events_from_db(labeling.session_id_of(p))
        except Exception:   # noqa: BLE001 — DB optional; just no auto-labels
            events = []
        rows = labeling.label_rows_for(p, gesture_events=events,
                                       source_types=source_types,
                                       source_variants=source_variants)
        return rows, source_types

    def _on_recording_loaded(self, path: str, rows, source_types) -> None:
        if path in self._recordings:
            return  # already added (e.g. a double trigger)
        p = Path(path)
        # Populate the dict and the list together so _current_path()'s
        # row→insertion-order mapping stays consistent.
        self._recordings[path] = rows
        types = sorted(set(source_types.values()))
        self.rec_list.addItem(
            p.name + (f"  ({', '.join(types)})" if types else ""))
        self.rec_list.setCurrentRow(self.rec_list.count() - 1)
        n_auto = sum(1 for r in rows if r.label)
        self.log.appendPlainText(
            f"Loaded {p.name}: {len(rows)} segments, {n_auto} auto-labelled "
            "from live tags"
            + (f"; skin type: {', '.join(types)}." if types
               else " (no skin type in header — pick one above)."))

    def _current_path(self) -> str | None:
        row = self.rec_list.currentRow()
        if row < 0 or row >= len(self._recordings):
            return None
        return list(self._recordings)[row]

    def _show_selected(self, _row: int) -> None:
        self.table.setRowCount(0)
        path = self._current_path()
        if path is None:
            return
        for r in self._recordings[path]:
            self._add_table_row(r)
        self._sync_type_combo(path)

    def _sync_type_combo(self, path: str) -> None:
        """Reflect the recording's detected skin type in the combo (no re-tag)."""
        rows = self._recordings[path]
        types = sorted({r.skin_type for r in rows if r.skin_type})
        self.type_combo.blockSignals(True)
        i = self.type_combo.findData(types[0]) if types else 0
        self.type_combo.setCurrentIndex(i if i >= 0 else 0)
        self.type_combo.blockSignals(False)

    def _on_type_changed(self) -> None:
        """User picked a type — tag every row of the current recording with it.

        Auto-detected recordings already carry their type, so this is the manual
        fallback (e.g. an old recording with no skin type in its header)."""
        path = self._current_path()
        if path is None:
            return
        st = self.type_combo.currentData() or ""
        for r in self._recordings[path]:
            r.skin_type = st

    def _add_table_row(self, lr: labeling.LabelRow) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate((lr.source, f"{lr.start_ms:.0f}",
                                    f"{lr.duration_ms:.0f}",
                                    str(lr.group_id) if lr.group_id else "")):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, item)
        combo = QComboBox()
        combo.addItem("", "")
        for g in GESTURE_CLASSES:
            combo.addItem(g, g)
        if lr.label:
            i = combo.findData(lr.label)
            combo.setCurrentIndex(i if i >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _i, rw=row: self._on_label_edited(rw))
        self.table.setCellWidget(row, 4, combo)

    def _on_label_edited(self, table_row: int) -> None:
        path = self._current_path()
        if path is None or table_row >= len(self._recordings[path]):
            return
        rows = self._recordings[path]
        label = (self.table.cellWidget(table_row, 4).currentData() or "")
        rows[table_row].label = label
        # A label change on any member of a group applies to the whole group.
        gid = rows[table_row].group_id
        if gid:
            for i, r in enumerate(rows):
                if r.group_id == gid and i != table_row:
                    r.label = label
                    self._set_combo_label(i, label)

    def _set_combo_label(self, table_row: int, label: str) -> None:
        """Set a row's gesture combo without re-triggering label edits."""
        combo = self.table.cellWidget(table_row, 4)
        if combo is None:
            return
        combo.blockSignals(True)
        j = combo.findData(label)
        combo.setCurrentIndex(j if j >= 0 else 0)
        combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Grouping (merge several touches into one gesture, e.g. triple tap)
    # ------------------------------------------------------------------

    def _selected_rows(self) -> list[int]:
        return sorted({idx.row() for idx in self.table.selectedIndexes()})

    def _group_selected(self) -> None:
        path = self._current_path()
        if path is None:
            return
        sel = self._selected_rows()
        if len(sel) < 2:
            QMessageBox.information(
                self, "Group", "Select at least two touches to group.")
            return
        rows = self._recordings[path]
        gid = max((r.group_id for r in rows), default=0) + 1
        # Seed the shared label from the first selected row that already has one.
        label = next((rows[i].label for i in sel if rows[i].label), "")
        for i in sel:
            rows[i].group_id = gid
            rows[i].label = label
        self._show_selected(self.rec_list.currentRow())
        self.log.appendPlainText(
            f"Grouped {len(sel)} touches as gesture #{gid}"
            + (f" ('{label}')." if label else " — pick a label for the group."))

    def _ungroup_selected(self) -> None:
        path = self._current_path()
        if path is None:
            return
        rows = self._recordings[path]
        for i in self._selected_rows():
            rows[i].group_id = 0
        self._show_selected(self.rec_list.currentRow())

    # ------------------------------------------------------------------
    # CSV import / export
    # ------------------------------------------------------------------

    def _import_csv(self) -> None:
        path = self._current_path()
        if path is None:
            QMessageBox.information(self, "Import", "Add a recording first.")
            return
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Import labels CSV", str(Settings().recordings_dir),
            "Labels (*.csv)")
        if not csv_path:
            return
        imported = {(r.source, round(r.start_ms)): r
                    for r in labeling.read_csv(Path(csv_path))}
        for r in self._recordings[path]:
            match = imported.get((r.source, round(r.start_ms)))
            if match:
                r.label = match.label
                r.group_id = match.group_id
        self._show_selected(self.rec_list.currentRow())
        self.log.appendPlainText(f"Imported labels from {Path(csv_path).name}.")

    def _export_csv(self) -> None:
        path = self._current_path()
        if path is None:
            QMessageBox.information(self, "Export", "Add a recording first.")
            return
        default = str(Path(path).with_suffix(".labels.csv"))
        csv_path, _ = QFileDialog.getSaveFileName(
            self, "Export labels CSV", default, "Labels (*.csv)")
        if not csv_path:
            return
        n = labeling.write_csv(self._recordings[path], Path(csv_path))
        self.log.appendPlainText(f"Exported {n} labelled segments → {csv_path}")

    # ------------------------------------------------------------------
    # Trained-model import / export
    # ------------------------------------------------------------------

    def _current_skin_type(self) -> str:
        return self.type_combo.currentData() or ""

    def _export_model(self) -> None:
        """Save the trained model for the selected skin type to a chosen file."""
        skin_type = self._current_skin_type()
        src = model_path(skin_type)
        if not skin_type or not src.exists():
            QMessageBox.information(
                self, "Export model",
                f"No trained model for skin type '{skin_type or '(none)'}'. "
                "Train one first.")
            return
        dst, _ = QFileDialog.getSaveFileName(
            self, "Export model", f"touch_{skin_type}.joblib",
            "Model (*.joblib)")
        if not dst:
            return
        import shutil
        shutil.copyfile(src, dst)
        self.log.appendPlainText(f"Exported model ({skin_type}) → {dst}")

    def _import_model(self) -> None:
        """Copy a chosen .joblib into the selected skin type's model slot."""
        skin_type = self._current_skin_type()
        if not skin_type:
            QMessageBox.information(
                self, "Import model", "Select a skin type first.")
            return
        src, _ = QFileDialog.getOpenFileName(
            self, "Import model", str(Settings().recordings_dir),
            "Model (*.joblib)")
        if not src:
            return
        dst = model_path(skin_type)
        if dst.exists() and QMessageBox.question(
                self, "Import model",
                f"Replace the existing model for '{skin_type}'?"
        ) != QMessageBox.StandardButton.Yes:
            return
        import shutil
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        self.log.appendPlainText(
            f"Imported model for {skin_type} ← {Path(src).name}")

    # ------------------------------------------------------------------
    # Train
    # ------------------------------------------------------------------

    def _train(self) -> None:
        # Train over every loaded recording, so loading recordings of several
        # skin types trains all their models in one pass.
        pairs = [(path, labeling.label_map(rows))
                 for path, rows in self._recordings.items()
                 if any(r.label for r in rows)]
        if not pairs:
            QMessageBox.information(
                self, "Train", "Label at least some segments first.")
            return
        from src.ml.training import train_models
        self.log.appendPlainText("\nTraining…")
        self.train_btn.setEnabled(False)
        # Training (scikit-learn cross-validation) takes seconds; run it on a
        # worker thread so the dialog stays responsive. ``on_log`` fires on that
        # worker thread, so route it through the ``_train_log`` signal.
        run_async(
            lambda: train_models(
                pairs, Settings.ROOT / "models",
                on_log=self._train_log.emit),
            on_done=self._on_train_done,
            on_error=self._on_train_error,
            parent=self,
        )

    def _on_train_done(self, report) -> None:
        trained = sum(1 for r in report.results if r.trained)
        self.log.appendPlainText(
            f"\nDone. {trained}/{len(report.results)} model(s) trained.")
        self.train_btn.setEnabled(True)

    def _on_train_error(self, exc: Exception) -> None:
        from src.ml.training import MLNotInstalled
        if isinstance(exc, MLNotInstalled):
            self.log.appendPlainText(str(exc))
            QMessageBox.warning(
                self, "scikit-learn not installed",
                "Training needs the optional ML dependencies.\n\n"
                "Install them with:\n    pip install -e '.[ml]'")
        else:
            self.log.appendPlainText(f"Training failed: {exc}")
        self.train_btn.setEnabled(True)

"""Touch Gestures dialog — label recordings and train models, all in the app.

End-to-end, no command line:

1. **Add recording** — picks a session JSONL from the recordings folder,
   segments it, and auto-fills each touch segment's gesture from the live
   labels tapped in the observer panel (aligned by time).
2. **Edit** the label of each segment in the table (a dropdown of gesture
   classes). Import / Export CSV to hand-edit or share datasets.
3. **Train** — one model per ``skin_type`` from every loaded recording, with a
   rule-baseline comparison and report.

The heavy lifting lives in ``src/ml/labeling.py`` (segment + align + CSV) and
``src/ml/training.py`` (fit). scikit-learn is the optional ``ml`` extra; if it's
missing, Train says so instead of failing.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.ml import labeling
from src.ml.gesture_taxonomy import GESTURE_CLASSES
from src.hardware.skin_geometry import known_skin_types


class TrainTouchDialog(QDialog):
    """Label recorded touch segments and train per-skin-type gesture models."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Touch Gestures — label & train")
        self.resize(820, 600)
        # recording path → list[LabelRow]
        self._recordings: dict[str, list[labeling.LabelRow]] = {}

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Add recordings, label each touch (auto-filled from live tags), "
            "then Train. One model is trained per skin type."))

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, stretch=1)

        # --- Left: recordings list ---
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Recordings:"))
        self._rec_list = QListWidget()
        self._rec_list.currentRowChanged.connect(self._show_selected)
        lv.addWidget(self._rec_list, stretch=1)
        add_btn = QPushButton("Add recording…")
        add_btn.clicked.connect(self._add_recording)
        lv.addWidget(add_btn)
        split.addWidget(left)

        # --- Right: segment/label table + per-recording tools ---
        right = QWidget()
        rv = QVBoxLayout(right)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Skin type:"))
        self._type_combo = QComboBox()
        for st in known_skin_types():
            self._type_combo.addItem(st, st)
        self._type_combo.currentIndexChanged.connect(self._apply_type_to_current)
        type_row.addWidget(self._type_combo)
        type_row.addStretch(1)
        rv.addLayout(type_row)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Source", "Start (ms)", "Dur (ms)", "Gesture"])
        self._table.horizontalHeader().setStretchLastSection(True)
        rv.addWidget(self._table, stretch=1)

        csv_row = QHBoxLayout()
        imp = QPushButton("Import CSV…")
        imp.clicked.connect(self._import_csv)
        exp = QPushButton("Export CSV…")
        exp.clicked.connect(self._export_csv)
        csv_row.addWidget(imp)
        csv_row.addWidget(exp)
        csv_row.addStretch(1)
        rv.addLayout(csv_row)
        split.addWidget(right)
        split.setSizes([220, 600])

        # --- Bottom: train + report ---
        train_row = QHBoxLayout()
        train_row.addStretch(1)
        self._train_btn = QPushButton("Train models")
        self._train_btn.clicked.connect(self._train)
        train_row.addWidget(self._train_btn)
        root.addLayout(train_row)

        root.addWidget(QLabel("Report:"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(150)
        root.addWidget(self._log)

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
        skin_type = self._type_combo.currentData() or ""
        try:
            events = labeling.gesture_events_from_db(
                labeling.session_id_of(Path(path)))
        except Exception:   # noqa: BLE001 — DB optional; just no auto-labels
            events = []
        rows = labeling.label_rows_for(Path(path), skin_type, events)
        self._recordings[path] = rows
        self._rec_list.addItem(Path(path).name)
        self._rec_list.setCurrentRow(self._rec_list.count() - 1)
        n_auto = sum(1 for r in rows if r.label)
        self._log.appendPlainText(
            f"Loaded {Path(path).name}: {len(rows)} segments, "
            f"{n_auto} auto-labelled from live tags.")

    def _current_path(self) -> str | None:
        row = self._rec_list.currentRow()
        if row < 0 or row >= len(self._recordings):
            return None
        return list(self._recordings)[row]

    def _show_selected(self, _row: int) -> None:
        path = self._current_path()
        self._table.setRowCount(0)
        if path is None:
            return
        for r in self._recordings[path]:
            self._add_table_row(r)

    def _add_table_row(self, lr: labeling.LabelRow) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, text in enumerate((lr.source, f"{lr.start_ms:.0f}",
                                    f"{lr.duration_ms:.0f}")):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, col, item)
        combo = QComboBox()
        combo.addItem("", "")
        for g in GESTURE_CLASSES:
            combo.addItem(g, g)
        if lr.label:
            i = combo.findData(lr.label)
            combo.setCurrentIndex(i if i >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _i, rw=row: self._on_label_edited(rw))
        self._table.setCellWidget(row, 3, combo)

    def _on_label_edited(self, table_row: int) -> None:
        path = self._current_path()
        if path is None or table_row >= len(self._recordings[path]):
            return
        combo = self._table.cellWidget(table_row, 3)
        self._recordings[path][table_row].label = combo.currentData() or ""

    def _apply_type_to_current(self) -> None:
        """Set the chosen skin_type on every row of the selected recording."""
        path = self._current_path()
        if path is None:
            return
        st = self._type_combo.currentData() or ""
        for r in self._recordings[path]:
            r.skin_type = st

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
                if match.skin_type:
                    r.skin_type = match.skin_type
        self._show_selected(self._rec_list.currentRow())
        self._log.appendPlainText(f"Imported labels from {Path(csv_path).name}.")

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
        self._log.appendPlainText(f"Exported {n} labelled segments → {csv_path}")

    # ------------------------------------------------------------------
    # Train
    # ------------------------------------------------------------------

    def _train(self) -> None:
        pairs = [(path, labeling.label_map(rows))
                 for path, rows in self._recordings.items()
                 if any(r.label for r in rows)]
        if not pairs:
            QMessageBox.information(
                self, "Train", "Label at least some segments first.")
            return
        from src.ml.training import MLNotInstalled, train_models
        self._log.appendPlainText("\nTraining…")
        self._train_btn.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            report = train_models(
                pairs, Settings.ROOT / "models",
                on_log=lambda m: self._log.appendPlainText(m))
            trained = sum(1 for r in report.results if r.trained)
            self._log.appendPlainText(
                f"\nDone. {trained}/{len(report.results)} model(s) trained.")
        except MLNotInstalled as exc:
            self._log.appendPlainText(str(exc))
            QMessageBox.warning(
                self, "scikit-learn not installed",
                "Training needs the optional ML dependencies.\n\n"
                "Install them with:\n    pip install -e '.[ml]'")
        except Exception as exc:   # noqa: BLE001 — surface, don't crash
            self._log.appendPlainText(f"Training failed: {exc}")
        finally:
            self.unsetCursor()
            self._train_btn.setEnabled(True)

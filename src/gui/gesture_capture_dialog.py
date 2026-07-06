"""Guided gesture-capture dialog — prompt, collect, train, and score, live.

Drives a :class:`~src.ml.gesture_capture.GestureCaptureSession` from the live
gateway magnet stream: it prompts one gesture at a time ("do a TAP — 2 / 5"), the
user performs it, and each detected touch is captured under the prompt. When the
plan is done it trains a per-skin-type model straight from the captured segments
and shows the cross-validation **accuracy + confusion matrix** — the deliberate
counterpart to the record-a-session-then-label-offline flow in
:class:`~src.gui.train_touch_dialog.TrainTouchDialog`.

Threading mirrors :class:`~src.gui.test_actuators_dialog.TestActuatorsDialog`:
``gateway.on_message`` fires on the gateway read thread, so magnet frames hop to
the GUI thread through a signal before touching any widget or the session. The
raw stream is also saved (``StreamRecorder``) with a labels CSV, so the capture
re-opens in the Train Touch dialog as a reusable dataset.

Layout: ``src/gui/ui/gesture_capture_dialog.ui`` (edit in Qt Designer, recompile
with ``scripts/compile_ui.sh``).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src.config.settings import Settings
from src.gui.async_task import run_async
from src.gui.base_dialog import BaseDialog
from src.gui.sensor_grid_view import SensorGridView
from src.gui.sensor_tester import SensorTester
from src.gui.ui_gesture_capture_dialog import Ui_GestureCaptureDialog
from src.hardware.skin_geometry import (
    SKIN_VARIANTS,
    VARIANT_LABELS,
    geometry_for,
    known_skin_types,
)
from src.ml import gesture_taxonomy as tax
from src.ml import labeling
from src.ml.gesture_capture import GestureCaptureSession
from src.ml.touch_motion import segment_summary

_PAGE_SETUP, _PAGE_CAPTURE, _PAGE_RESULTS = 0, 1, 2

# Confusion-matrix cell tints (theme-agnostic, readable on light backgrounds).
_DIAG_BG = QColor(200, 230, 201)      # correct predictions (green)
_OFFDIAG_BG = QColor(255, 205, 205)   # confusions (red)

# Compact gesture names for the confusion-matrix headers.
_SHORT_LABELS = {
    tax.TAP: "tap", tax.PRESS: "press", tax.COMPRESSIONS: "compr.",
}


class GestureCaptureDialog(BaseDialog, Ui_GestureCaptureDialog):
    """Guided, prompt-driven capture of touch gestures + immediate scoring."""

    # Gateway read thread → GUI thread. Carries one magnet message dict.
    _magnet = Signal(object)
    # Same hop for the bound skin's (pressure-compensated) stream.
    _comp_magnet = Signal(object)

    def __init__(self, gateway: Any, skin_types: list[str] | None = None,
                 skins: list[Any] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._gateway = gateway
        # Configured skins (from the app's robots). When the locked node belongs
        # to one, capture reads that skin's stream — pressure-compensated when
        # coupling calibration is enabled — so gestures can be collected while
        # chambers inflate and still match what live inference sees.
        self._skins = list(skins or [])
        self._bound_skin: Any | None = None
        self._source: str = ""          # locked touch-node MAC (first seen)
        self._session: GestureCaptureSession | None = None
        self._recorder = None
        self._recording_path: Path | None = None
        self._events: list[tuple[float, str]] = []   # (epoch_ms, label) for the CSV
        self._t0: float | None = None
        # Live touch test: a SensorTester (µT bars + adjustable sensitivity) plus
        # spatial grids (setup + capture pages) built once the node streams.
        self._sensor_tester: SensorTester | None = None
        self._grids: list[SensorGridView] = []
        self._panel_type: str | None = None

        # Skin type: the recording targets one model (models/touch_<type>.joblib).
        self.type_combo.addItem("", "")
        for st in (skin_types or known_skin_types()):
            self.type_combo.addItem(st, st)
        # Silicone variant (optional touch feature): blank + known variants.
        self.variant_combo.addItem("", "")
        for v in SKIN_VARIANTS:
            self.variant_combo.addItem(VARIANT_LABELS.get(v, v), v)

        # One checkbox per gesture class (all on by default).
        self._gesture_cbs: dict[str, QCheckBox] = {}
        for code in tax.GESTURE_CLASSES:
            cb = QCheckBox(code.replace("_", " "))
            cb.setChecked(True)
            cb.setToolTip(tax.DEFINITIONS.get(code, ""))
            cb.toggled.connect(self._refresh_start_enabled)
            self.gestures_layout.addWidget(cb)
            self._gesture_cbs[code] = cb

        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.start_btn.clicked.connect(self._start)
        self.undo_btn.clicked.connect(self._undo)
        self.cancel_btn.clicked.connect(self.reject)
        self.close_btn.clicked.connect(self.accept)

        self._magnet.connect(self._on_magnet)
        self._comp_magnet.connect(self._on_comp_magnet)
        self.finished.connect(self._cleanup)

        if self._gateway is not None and self._gateway.is_connected:
            # Bound method (the gateway keeps a WeakMethod), so the dialog must
            # stay alive while listening — it does, for its modal lifetime.
            self._gateway.on_message(self._on_gateway_message)
        else:
            self.source_label.setText(
                "No gateway connected — connect one to capture gestures.")

    # ------------------------------------------------------------------
    # Live source (gateway read thread → GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        """Gateway read thread: forward magnet frames to the GUI thread."""
        if data.get("type") != "magnet":
            return
        # Before a source is locked, take any node; after, only the locked one.
        if self._source and data.get("source") != self._source:
            return
        self._magnet.emit(data)

    def _on_magnet(self, data: dict) -> None:
        """GUI thread, RAW stream: lock the source and drive the sensitivity
        bars (the node's own µT view, which the pushed threshold acts on)."""
        if not self._source:
            self._source = str(data.get("source", ""))
            self.source_label.setText(f"Touch node: {self._source}")
            self._bind_skin()
            self._refresh_start_enabled()
        mag = data.get("mag")
        if isinstance(mag, list):
            self._ensure_sensor_panel(len(mag))
            if self._sensor_tester is not None:
                self._sensor_tester.update_values(mag)
        # With a bound skin the compensated stream drives detection instead
        # (identical at rest; correct while chambers inflate).
        if self._bound_skin is None:
            self._feed_detection(data)

    def _on_comp_magnet(self, data: dict) -> None:
        """GUI thread: the bound skin's stream (compensated when calibrated)."""
        src = data.get("source")
        if src and self._source and str(src) != self._source:
            return
        self._feed_detection(data)

    def _feed_detection(self, data: dict) -> None:
        """Feed the spatial grids and the capture session with one message."""
        mag = data.get("mag")
        if isinstance(mag, list):
            for grid in self._grids:
                grid.feed(mag, data.get("vec"))
        if self._session is None:
            return
        if self._t0 is None:
            self._t0 = time.monotonic() * 1000.0
        t_ms = time.monotonic() * 1000.0 - self._t0
        event = self._session.feed(data, t_ms)
        if event is not None:
            self._events.append((datetime.now().timestamp() * 1000.0, event.label))
            self._show_capture_summary(event)
            self._refresh_capture()
            if self._session.is_complete:
                self._finish()

    def _bind_skin(self) -> None:
        """Adopt the configured skin that owns the locked node, if any.

        Subscribes to its detection stream via ``subscribe_skin_magnet`` — the
        same single entry point live inference uses — so capture sees
        pressure-compensated readings whenever the skin has coupling calibration
        enabled. Also pre-selects the skin's type/variant in the combos."""
        from src.hardware.touch_source import (CompensatedMagnetSource,
                                               subscribe_skin_magnet)
        for skin in self._skins:
            mac = ((getattr(skin, "touch", None) or {}).get("node_mac") or "")
            if not mac or mac.upper() != self._source.upper():
                continue
            if not subscribe_skin_magnet(skin, self._comp_magnet.emit):
                continue
            self._bound_skin = skin
            self._select_combo(self.type_combo,
                               getattr(skin, "skin_type", "") or "")
            self._select_combo(self.variant_combo,
                               getattr(skin, "skin_variant", "") or "")
            compensated = isinstance(getattr(skin, "touch_source", None),
                                     CompensatedMagnetSource)
            self.source_label.setText(
                f"Touch node: {self._source} — skin "
                f"'{getattr(skin, 'skin_id', '?')}'"
                + (" (pressure-compensated stream)" if compensated
                   else " (raw stream — no coupling calibration)"))
            return

    @staticmethod
    def _select_combo(combo, value: str) -> None:
        """Select ``value`` in a data-backed combo if present (no-op otherwise)."""
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _show_capture_summary(self, event) -> None:
        """One line about the gesture just captured — duration, pulses, peak
        and (for slides) the direction of travel across the skin."""
        geo = geometry_for(self.type_combo.currentData() or "")
        positions = list(geo.sensors_mm) if geo is not None else []
        self.last_capture_label.setText(
            "✔ " + segment_summary(event.segment, event.label, positions))

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _refresh_start_enabled(self) -> None:
        ready = bool(self._source) and bool(self.type_combo.currentData()) \
            and any(cb.isChecked() for cb in self._gesture_cbs.values())
        self.start_btn.setEnabled(ready)

    def _on_type_changed(self) -> None:
        """New skin type → re-seed its saved sensitivity and re-shape the grids."""
        self._refresh_start_enabled()
        if self._sensor_tester is not None:
            self._seed_threshold()
            self._rebuild_grids_if_needed()

    # ------------------------------------------------------------------
    # Live touch test + sensitivity (SensorTester bars + spatial grids)
    # ------------------------------------------------------------------

    def _ensure_sensor_panel(self, count: int) -> None:
        """Build the sensitivity bars (once, from the first frame's sensor count)
        and the spatial grids for the current skin type."""
        if self._sensor_tester is None:
            self._sensor_tester = SensorTester(
                count, self._rezero_node, self._push_and_save_threshold)
            self._sensor_tester.threshold_spin.valueChanged.connect(
                self._refresh_grids)
            self.sensor_holder.addWidget(self._sensor_tester)
            self._seed_threshold()
        self._rebuild_grids_if_needed()

    def _rebuild_grids_if_needed(self) -> None:
        """(Re)build the setup + capture grids when the skin type changes."""
        st = self.type_combo.currentData() or ""
        if st == self._panel_type and self._grids:
            return
        self._panel_type = st
        for grid in self._grids:
            grid.setParent(None)
            grid.deleteLater()
        self._grids = []
        geo = geometry_for(st)
        setup_grid = SensorGridView(geo, self._threshold_value)
        self.sensor_holder.insertWidget(0, setup_grid)
        capture_grid = SensorGridView(geo, self._threshold_value)
        self._center_widget(self.capture_grid_row, capture_grid)
        self._grids = [setup_grid, capture_grid]

    @staticmethod
    def _center_widget(layout, widget) -> None:
        """Clear ``layout`` and add ``widget`` centred between two stretches."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        layout.addStretch(1)
        layout.addWidget(widget)
        layout.addStretch(1)

    def _threshold_value(self) -> float:
        return (self._sensor_tester.threshold_spin.value()
                if self._sensor_tester is not None else 100.0)

    def _refresh_grids(self) -> None:
        for grid in self._grids:
            grid.refresh()

    def _seed_threshold(self) -> None:
        """Load this skin type's saved sensitivity into the spin, if any."""
        saved = Settings().touch_threshold_ut(self.type_combo.currentData() or "")
        if saved is not None and self._sensor_tester is not None:
            self._sensor_tester.threshold_spin.setValue(saved)

    def _rezero_node(self) -> None:
        if self._gateway is not None and self._source:
            self._gateway.send(self._source, "rebaseline")

    def _push_and_save_threshold(self, threshold_ut: float) -> None:
        """Push the sensitivity to the node AND save it as this skin type's
        default (the "use generally" part)."""
        if threshold_ut <= 0:
            return
        if self._gateway is not None and self._source:
            self._gateway.send(self._source, "configure", repeat=2,
                               act_threshold_ut=threshold_ut)
        # Keep the bound skin's compensated stream in step: it rederives ``act``
        # at its own threshold, which must match the node's new sensitivity.
        src = getattr(self._bound_skin, "touch_source", None)
        if hasattr(src, "set_threshold_ut"):
            src.set_threshold_ut(threshold_ut)
        st = self.type_combo.currentData() or ""
        if st:
            Settings().set_touch_threshold_ut(st, threshold_ut)

    def _plan(self) -> list[tuple[str, int]]:
        reps = self.reps_spin.value()
        return [(code, reps) for code in tax.GESTURE_CLASSES
                if self._gesture_cbs[code].isChecked()]

    def _start(self) -> None:
        skin_type = self.type_combo.currentData() or ""
        variant = self.variant_combo.currentData() or ""
        self._session = GestureCaptureSession(self._plan())
        self._t0 = None
        self._events = []
        self.last_capture_label.setText("")
        self._start_recording(skin_type, variant)
        self.stack.setCurrentIndex(_PAGE_CAPTURE)
        self._refresh_capture()

    def _start_recording(self, skin_type: str, variant: str) -> None:
        """Persist the raw stream + a header so it re-opens in Train Touch."""
        try:
            from src.data.stream_recorder import StreamRecorder
            rec_dir = Settings().recordings_dir
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._recording_path = rec_dir / f"gesture_calib_{stamp}.jsonl"
            self._recorder = StreamRecorder(
                self._recording_path, session_id=self._recording_path.stem,
                gateway=self._gateway,
                skin_types={self._source: skin_type},
                skin_variants={self._source: variant})
            self._recorder.start()
            if self._bound_skin is not None:
                # Record the compensated lines too, so the saved dataset trains
                # on the same stream this capture (and live inference) consumes.
                self._recorder.attach_compensated(
                    getattr(self._bound_skin, "touch_source", None))
        except Exception:   # noqa: BLE001 — recording is a bonus; capture must go on
            self._recorder = None
            self._recording_path = None

    # ------------------------------------------------------------------
    # Capture page
    # ------------------------------------------------------------------

    def _refresh_capture(self) -> None:
        s = self._session
        if s is None:
            return
        self.prompt_label.setText(s.current_gesture.replace("_", " ").upper())
        self.prompt_hint.setText(tax.DEFINITIONS.get(s.current_gesture, ""))
        self.count_label.setText(f"{s.current_done} / {s.current_total}")
        total = sum(reps for _g, reps in s.plan)
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(s.total_captured)
        tally = "   ".join(f"{g}: {n}" for g, n in s.counts_by_gesture().items())
        self.tally_label.setText(tally)
        self.undo_btn.setEnabled(s.total_captured > 0)

    def _undo(self) -> None:
        if self._session is None:
            return
        if self._session.undo_last() is not None and self._events:
            self._events.pop()
        self._refresh_capture()

    # ------------------------------------------------------------------
    # Finish → persist labels + train + report
    # ------------------------------------------------------------------

    def _finish(self) -> None:
        session = self._session
        self._session = None            # stop feeding; capture is done
        self._stop_recording()
        self._write_labels()
        self.stack.setCurrentIndex(_PAGE_RESULTS)
        self.result_label.setText("Training…")
        skin_type = self.type_combo.currentData() or ""
        variant = self.variant_combo.currentData() or ""
        samples = session.labeled_samples(skin_type, variant)
        run_async(
            lambda: self._train(samples),
            on_done=self._on_trained,
            on_error=self._on_train_error,
            parent=self,
        )

    @staticmethod
    def _train(samples):
        from src.ml.training import train_from_segments
        return train_from_segments(samples, Settings.ROOT / "models")

    def _stop_recording(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:   # noqa: BLE001
                pass

    def _write_labels(self) -> None:
        """Best-effort labels CSV beside the recording (single touches align
        exactly; multi-taps may need re-grouping in Train Touch)."""
        if self._recording_path is None or not self._recording_path.exists():
            return
        skin_type = self.type_combo.currentData() or ""
        variant = self.variant_combo.currentData() or ""
        try:
            rows = labeling.label_rows_for(
                self._recording_path, gesture_events=self._events,
                source_types={self._source: skin_type},
                source_variants={self._source: variant})
            labeling.write_csv(rows, self._recording_path.with_suffix(".labels.csv"))
        except Exception:   # noqa: BLE001 — the model is the real output
            pass

    def _on_trained(self, report) -> None:
        skin_type = self.type_combo.currentData() or ""
        res = next((r for r in report.results if r.skin_type == skin_type), None)
        if res is None:
            self.result_label.setText("No model was trained.")
        elif res.model_acc is not None:
            acc = res.model_acc
            self.result_label.setText(
                f"Accuracy: {acc:.0%}   (error {1 - acc:.0%})")
            self._render_confusion(res.labels, res.confusion)
        elif res.trained:
            self.result_label.setText(
                f"Model trained ({res.n_samples} samples) — do more repetitions "
                "for an accuracy estimate.")
        else:
            self.result_label.setText(
                f"Not enough data to train ({res.n_samples} samples, "
                f"{res.n_classes} gesture(s)). Capture more.")
        note = ""
        if self._recording_path is not None:
            note = f"\nSaved dataset: {self._recording_path.name}"
        self.report_text.setPlainText(report.text() + note)

    def _on_train_error(self, exc: Exception) -> None:
        from src.ml.training import MLNotInstalled
        if isinstance(exc, MLNotInstalled):
            self.result_label.setText("scikit-learn not installed.")
            self.report_text.setPlainText(
                "Training needs the optional ML dependencies:\n"
                "    pip install -e '.[ml]'\n\n"
                "The captured dataset was still saved and can be trained later.")
        else:
            self.result_label.setText("Training failed.")
            self.report_text.setPlainText(str(exc))

    def _render_confusion(self, labels: list[str],
                          matrix: list[list[int]] | None) -> None:
        if not labels or not matrix:
            return
        short = [_SHORT_LABELS.get(g, g) for g in labels]
        table = self.confusion_table
        table.setRowCount(len(labels))
        table.setColumnCount(len(labels))
        table.setHorizontalHeaderLabels(short)
        table.setVerticalHeaderLabels(short)
        for i, row in enumerate(matrix):
            for j, count in enumerate(row):
                item = QTableWidgetItem(str(count))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if count:
                    item.setBackground(_DIAG_BG if i == j else _OFFDIAG_BG)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                table.setItem(i, j, item)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _cleanup(self, *_a) -> None:
        """Stop listening + recording when the dialog closes (any outcome)."""
        remove = getattr(self._gateway, "remove_message_callback", None)
        if remove is not None:
            remove(self._on_gateway_message)
        self._stop_recording()

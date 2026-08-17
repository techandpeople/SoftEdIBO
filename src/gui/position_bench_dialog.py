"""Touch position bench dialog - Tools -> Touch Position Bench....

Phase 0 of docs/TOUCH_POSITION_ML_PLAN.md, all in the GUI: prompts every cell
of a grid marked on the skin (or free-form patterns for multi-touch/squeeze
candidates), captures the press signatures from the live magnet stream, and
reports the finest grid the data supports - ML on the 3-axis signatures versus
magnitude-only features and today's dominant-sensor quadrant logic.

The capture/analysis logic lives in :mod:`src.ml.position_bench`; this dialog
is the front-end. Threading mirrors
:class:`~src.gui.gesture_capture_dialog.GestureCaptureDialog`: gateway frames
hop to the GUI thread through a signal, and the analysis runs off-thread via
``run_async``. The bench reads the RAW stream on purpose - it measures the
physics of a resting skin; pressure compensation only matters under inflation,
which the bench instructions exclude.

Layout: ``src/gui/ui/position_bench_dialog.ui`` (edit in Qt Designer,
recompile with ``scripts/compile_ui.sh``).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from src.config.settings import Settings
from src.gui.async_task import run_async
from src.gui.base_dialog import BaseDialog
from src.gui.bench_target_grid import BenchTargetGrid
from src.gui.ui_position_bench_dialog import Ui_PositionBenchDialog
from src.hardware.skin_geometry import (
    SKIN_VARIANTS,
    VARIANT_LABELS,
    known_skin_types,
)
from src.ml.position_bench import (
    AnalysisUnavailable,
    CaptureSession,
    PaceTracker,
    PressDetector,
    analyze,
    load_dataset,
    save_jsonl,
)

_DIALOG_TITLE = "Touch Position Bench"

_PAGE_SETUP, _PAGE_CAPTURE, _PAGE_RESULTS = 0, 1, 2

# 70 baseline reads at ~28 Hz ~= 2.5 s; leave margin before trusting the stream.
_BASELINE_SETTLE_MS = 4000


class PositionBenchDialog(BaseDialog, Ui_PositionBenchDialog):
    """Guided grid/pattern capture + feasibility report for touch position."""

    # Gateway read thread -> GUI thread. Carries one magnet message dict.
    _magnet = Signal(object)

    def __init__(self, gateway: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._gateway = gateway
        self._source: str = ""              # locked touch-node MAC
        self._session: CaptureSession | None = None
        self._pace = PaceTracker()
        self._targets: list[tuple[str, tuple[int, int] | None]] = []
        self._target_idx = 0
        self._total_reps = 0
        self._done_reps = 0
        self._settling = False              # ignore frames while re-zeroing
        self._saw_vec = False
        self._vec_streaming = False         # we turned the node's vec rows on
        self._dataset_path: Path | None = None

        self.type_combo.addItem("", "")
        for st in known_skin_types():
            self.type_combo.addItem(st, st)
        self.variant_combo.addItem("", "")
        for v in SKIN_VARIANTS:
            self.variant_combo.addItem(VARIANT_LABELS.get(v, v), v)

        self._grid = BenchTargetGrid()
        self.grid_row.addStretch(1)
        self.grid_row.addWidget(self._grid, 4)
        self.grid_row.addStretch(1)
        self.report_text.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        self.start_btn.clicked.connect(self._start)
        self.open_btn.clicked.connect(self._open_dataset)
        self.redo_btn.clicked.connect(self._redo_target)
        self.skip_btn.clicked.connect(self._next_target)
        self.rezero_btn.clicked.connect(lambda: self._rebaseline(None))
        self.finish_btn.clicked.connect(self._finish)
        self.again_btn.clicked.connect(
            lambda: self.stack.setCurrentIndex(_PAGE_SETUP))
        self.close_btn.clicked.connect(self.accept)

        self._magnet.connect(self._on_magnet)
        self.finished.connect(self._cleanup)

        if self._gateway is not None and self._gateway.is_connected:
            self._gateway.on_message(self._on_gateway_message)
        else:
            self.source_label.setText(
                "No gateway connected - you can still analyze saved datasets.")

    # ------------------------------------------------------------------
    # Live source (gateway read thread -> GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        """Gateway read thread: forward magnet frames to the GUI thread."""
        if data.get("type") != "magnet":
            return
        if self._source and data.get("source") != self._source:
            return
        self._magnet.emit(data)

    def _on_magnet(self, data: dict) -> None:
        if not self._source:
            self._source = str(data.get("source", ""))
            self.source_label.setText(f"Touch node: {self._source}")
            self._refresh_start_enabled()
        self._saw_vec = self._saw_vec or isinstance(data.get("vec"), list)
        if self._session is None or self._settling:
            return
        press = self._session.feed_message(data, time.monotonic() * 1000.0)
        if press is None:
            return
        self._done_reps += 1
        self._pace.mark(time.monotonic())
        self._grid.flash_press()
        self.last_label.setText(
            f"ok peak {press.peak_energy:.0f} uT, {press.duration_ms:.0f} ms")
        label, _cell = self._targets[self._target_idx]
        if self._session.target_count(label) >= self.reps_spin.value():
            self._advance()
        else:
            self._refresh_capture()

    # ------------------------------------------------------------------
    # Setup / start
    # ------------------------------------------------------------------

    def _refresh_start_enabled(self) -> None:
        self.start_btn.setEnabled(bool(self._source))

    def _pattern_names(self) -> list[str]:
        return [p.strip() for p in self.patterns_edit.text().split(",")
                if p.strip()]

    def _start(self) -> None:
        # Schmitt hysteresis: the release threshold must sit below the press
        # one, and the two spin boxes are independent. PressDetector raises on
        # a bad pair, which would escape this slot as an unhandled exception.
        if self.exit_spin.value() > self.enter_spin.value():
            QMessageBox.warning(
                self, _DIALOG_TITLE,
                "The release threshold must be at or below the press "
                "threshold, otherwise a press could never end.")
            return
        patterns = self._pattern_names()
        if patterns:
            self._targets = [(name, None) for name in patterns]
        else:
            rows, cols = self.rows_spin.value(), self.cols_spin.value()
            self._targets = [(f"{r},{c}", (r, c))
                             for r in range(rows) for c in range(cols)]
            self._grid.set_grid(rows, cols)
        self._session = CaptureSession(PressDetector(
            enter_ut=self.enter_spin.value(), exit_ut=self.exit_spin.value()))
        self._pace = PaceTracker()
        self._target_idx = 0
        self._done_reps = 0
        self._total_reps = len(self._targets) * self.reps_spin.value()
        self._saw_vec = False
        self.progress_bar.setMaximum(self._total_reps)
        self.last_label.setText("")
        self.stack.setCurrentIndex(_PAGE_CAPTURE)
        # 3-axis rows on (runtime toggle, no reflash), then a clean baseline.
        if self._gateway is not None and self._source:
            self._gateway.send(self._source, "configure", repeat=2,
                               stream_vec=True)
            self._vec_streaming = True
        self._rebaseline(self._refresh_capture)

    def _rebaseline(self, then) -> None:
        """Re-zero the node and pause detection while the baseline settles."""
        self._settling = True
        self.prompt_label.setText("Re-zeroing - hands off the skin...")
        if self._gateway is not None and self._source:
            self._gateway.send(self._source, "rebaseline")

        def _done() -> None:
            self._settling = False
            self._refresh_capture()
            if then is not None:
                then()
        QTimer.singleShot(_BASELINE_SETTLE_MS, _done)

    # ------------------------------------------------------------------
    # Capture flow
    # ------------------------------------------------------------------

    def _current_target(self) -> tuple[str, tuple[int, int] | None]:
        return self._targets[self._target_idx]

    def _refresh_capture(self) -> None:
        if self._session is None or self._settling:
            return
        label, cell = self._current_target()
        self._session.set_target(label, cell)
        if cell is not None:
            self.prompt_label.setText(f"Press cell {label}")
            self._grid.set_target(cell)
        else:
            self.prompt_label.setText(f"Perform: {label}")
            self._grid.set_target(None)
        done = self._session.target_count(label)
        self.count_label.setText(
            f"{done} / {self.reps_spin.value()}   "
            f"(target {self._target_idx + 1} of {len(self._targets)})")
        self.progress_bar.setValue(self._done_reps)
        eta = self._pace.eta_s(self._total_reps - self._done_reps)
        per = self._pace.seconds_per_rep()
        pace = f"   ({per:.1f} s/press)" if per else ""
        self.eta_label.setText(f"ETA {PaceTracker.fmt(eta)}{pace}")

    def _advance(self) -> None:
        if self._target_idx + 1 >= len(self._targets):
            self._finish()
            return
        self._target_idx += 1
        every = self.rebase_spin.value()
        if every and self._target_idx % every == 0:
            self._rebaseline(None)
        else:
            self._refresh_capture()

    def _next_target(self) -> None:
        """Skip: count the missing reps as done so the ETA stays honest."""
        if self._session is None:
            return
        label, _cell = self._current_target()
        self._done_reps += max(
            0, self.reps_spin.value() - self._session.target_count(label))
        self._advance()

    def _redo_target(self) -> None:
        if self._session is None:
            return
        label, _cell = self._current_target()
        self._done_reps -= self._session.drop_target(label)
        self._refresh_capture()

    # ------------------------------------------------------------------
    # Finish -> save + analyze
    # ------------------------------------------------------------------

    def _finish(self) -> None:
        session = self._session
        if session is None:
            return
        self._session = None            # stop feeding; capture is done
        patterns = self._pattern_names()
        meta = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "skin_type": self.type_combo.currentData() or "",
            "skin_variant": self.variant_combo.currentData() or "",
            "node_mac": self._source,
            "rows": 0 if patterns else self.rows_spin.value(),
            "cols": 0 if patterns else self.cols_spin.value(),
            "reps": self.reps_spin.value(),
            "cell_size_mm": self.cell_spin.value() or None,
            "enter_ut": self.enter_spin.value(),
            "exit_ut": self.exit_spin.value(),
            "vec": self._saw_vec,
        }
        saved = ""
        if session.presses:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"position_bench_{meta['skin_type'] or 'skin'}_{stamp}.jsonl"
            self._dataset_path = Settings().recordings_dir / name
            try:
                save_jsonl(self._dataset_path, meta, session.presses)
                saved = f"Dataset saved: {self._dataset_path.name}"
            except OSError as exc:
                self._dataset_path = None
                saved = f"Could not save the dataset: {exc}"
        self._show_analysis(meta, session.presses, saved)

    def _show_analysis(self, meta: dict, presses: list, note: str = "") -> None:
        self.stack.setCurrentIndex(_PAGE_RESULTS)
        if not presses:
            self.result_label.setText("Nothing was captured.")
            self.report_text.setPlainText(note)
            return
        vec_note = "" if any(p.has_vec for p in presses) else (
            "\n\nWARNING: no 3-axis 'vec' rows in this data - the node's "
            "firmware predates the runtime stream_vec toggle. The report used "
            "the weaker magnitude-only features; reflash the touch node and "
            "re-run for the real answer.")
        self.result_label.setText("Analyzing...")
        self.report_text.setPlainText("")
        run_async(
            lambda: analyze(meta, presses),
            on_done=lambda report: self._on_analyzed(report, note + vec_note),
            on_error=lambda exc: self._on_analyze_error(exc, note),
            parent=self,
        )

    def _on_analyzed(self, report: str, note: str) -> None:
        self.result_label.setText("Bench report")
        self.report_text.setPlainText(report + ("\n\n" + note if note else ""))
        if self._dataset_path is not None:
            try:
                self._dataset_path.with_suffix(".report.txt").write_text(
                    report + "\n", encoding="utf-8")
            except OSError:
                pass                    # the on-screen report is the output

    def _on_analyze_error(self, exc: Exception, note: str) -> None:
        if isinstance(exc, AnalysisUnavailable):
            self.result_label.setText("Analysis unavailable")
            self.report_text.setPlainText(
                f"{exc}\n\nThe captured dataset was still saved and can be "
                f"analyzed later from this dialog.\n{note}")
        else:
            self.result_label.setText("Analysis failed")
            self.report_text.setPlainText(f"{exc}\n\n{note}")

    def _open_dataset(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Open bench dataset(s)", str(Settings().recordings_dir),
            "Bench datasets (*.jsonl)")
        if not paths:
            return
        self._dataset_path = None       # re-analysis: don't overwrite reports
        try:
            meta, presses = load_dataset(paths)
        except (OSError, ValueError) as exc:
            self.stack.setCurrentIndex(_PAGE_RESULTS)
            self.result_label.setText("Could not load the dataset")
            self.report_text.setPlainText(str(exc))
            return
        self._show_analysis(meta, presses,
                            f"Loaded {len(presses)} presses from "
                            f"{len(paths)} file(s).")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _cleanup(self, *_a) -> None:
        # stream_vec lives in the node's RAM, so leaving it on would keep the
        # 3-axis rows coming for the rest of its power cycle - and TouchCompensator
        # switches to vector subtraction the moment it sees them.
        if self._vec_streaming and self._gateway is not None and self._source:
            self._gateway.send(self._source, "configure", repeat=2,
                               stream_vec=False)
            self._vec_streaming = False
        remove = getattr(self._gateway, "remove_message_callback", None)
        if remove is not None:
            remove(self._on_gateway_message)

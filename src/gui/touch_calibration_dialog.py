"""Touch↔chamber coupling calibration dialog (Tools → Calibrate Touch Coupling…).

Measures, per skin, how much inflating each chamber shifts every magnet/touch
sensor (in µT), and stores it as the skin's ``touch.coupling`` matrix so the
runtime can subtract that actuation offset and stop a chamber faking a touch
(see :mod:`src.core.touch_compensation`).

Sweep (driven by a timer + the gateway's ``status``/``magnet`` streams):
  1. Deflate every chamber and dwell — establishes the rest baseline.
  2. For each chamber: inflate it alone to full, dwell (magnet samples collected),
     then deflate back to rest.
  3. Build the coupling matrix from the collected samples and preview it.

Sample collection + the matrix maths are the Qt-free, unit-tested
:mod:`src.core.touch_coupling` / :mod:`src.hardware.touch_calibration`; this
dialog only drives the hardware and the UI.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.gui.ui_touch_calibration_dialog import Ui_TouchCalibrationDialog
from src.hardware.touch_calibration import (
    coupling_config_from_samples,
    iter_touch_skins,
    set_compensation,
    set_touch_coupling,
)

# Sweep timing. Dwell well past the coupling analyzer's steady-state guard
# (settle_ms 800) so each chamber contributes plenty of settled samples.
_REST_MS = 2500
_INFLATE_DWELL_MS = 3500
_TICK_MS = 100


class TouchCalibrationDialog(QDialog, Ui_TouchCalibrationDialog):
    """Measure and store the per-skin touch↔chamber coupling matrix."""

    # gateway read thread → GUI thread (so samples are collected single-threaded)
    _msg = Signal(object)
    # Emitted after a matrix is saved, so the app can rebuild robots to apply it.
    saved = Signal()

    def __init__(self, settings: Any, gateway: Any,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._settings = settings
        self._gateway = gateway
        self._active = True

        self._skins = iter_touch_skins(settings.data)
        for s in self._skins:
            self.skin_combo.addItem(
                f"{s['robot_id']}/{s['skin_id']}  ({s['touch_mac']})", userData=s)

        self._pressures: dict[int, float] = {}
        self._samples: list[tuple[float, dict[int, float], list[float]]] = []
        self._result: dict | None = None
        self._job: dict | None = None

        has_targets = bool(self._skins) and gateway is not None
        self.run_btn.setEnabled(has_targets)
        if not self._skins:
            self.status_label.setText(
                "No magnet-capable skins configured (need a node_direct or "
                "node_magnet_sensor touch node).")

        self.run_btn.clicked.connect(self._run)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.suppress_check.toggled.connect(self.suppress_spin.setEnabled)
        self.save_btn.setEnabled(False)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._msg.connect(self._on_msg, Qt.ConnectionType.QueuedConnection)
        if gateway is not None:
            gateway.on_message(self._on_gateway_message)
        self.finished.connect(lambda _=0: self._stop())

    # ------------------------------------------------------------------
    # Sweep driving
    # ------------------------------------------------------------------

    def _current_skin(self) -> dict | None:
        return self.skin_combo.currentData()

    def _run(self) -> None:
        skin = self._current_skin()
        if self._job is not None or skin is None or not skin["slots"]:
            return
        self._samples.clear()
        self._pressures.clear()
        self._result = None
        self.save_btn.setEnabled(False)
        self.preview.clear()
        self._job = {
            "skin": skin, "queue": list(skin["slots"]),
            "done": [], "phase": "rest", "elapsed": 0,
        }
        self._set_running(True)
        self._deflate_all(skin)
        self.status_label.setText("Resting (establishing baseline)…")
        self._tick.start()

    def _deflate_all(self, skin: dict) -> None:
        for slot in skin["slots"]:
            self._gateway.send(skin["chamber_mac"], "deflate", chamber=slot,
                               delta=100)

    def _on_tick(self) -> None:
        job = self._job
        if job is None:
            return
        job["elapsed"] += _TICK_MS
        skin = job["skin"]
        if job["phase"] == "rest":
            if job["elapsed"] >= _REST_MS:
                self._next_chamber(job)
        elif job["phase"] == "inflate":
            if job["elapsed"] >= _INFLATE_DWELL_MS:
                self._gateway.send(skin["chamber_mac"], "deflate",
                                   chamber=job["slot"], delta=100)
                job["phase"] = "settle"
                job["elapsed"] = 0
        elif job["phase"] == "settle":
            if job["elapsed"] >= _REST_MS:
                job["done"].append(job["slot"])
                self._next_chamber(job)

    def _next_chamber(self, job: dict) -> None:
        if not job["queue"]:
            self._finish(job)
            return
        slot = job["queue"].pop(0)
        job["slot"] = slot
        job["phase"] = "inflate"
        job["elapsed"] = 0
        total = len(job["skin"]["slots"])
        self.progress.setValue(int(100 * len(job["done"]) / max(1, total)))
        self.status_label.setText(f"Inflating chamber slot {slot}…")
        self._gateway.send(job["skin"]["chamber_mac"], "set_pressure",
                           chamber=slot, value=100)

    def _finish(self, job: dict) -> None:
        self._tick.stop()
        self._job = None
        self._deflate_all(job["skin"])
        self.progress.setValue(100)
        skin = job["skin"]
        cfg, matrix = coupling_config_from_samples(
            list(self._samples), skin["sensor_count"])
        self._result = cfg
        self.preview.setPlainText(self._format_matrix(matrix, skin))
        self.status_label.setText(
            f"Done — {len(self._samples)} samples over {len(job['done'])} "
            "chamber(s). Review, then Save.")
        self._set_running(False)
        self.save_btn.setEnabled(True)

    @staticmethod
    def _format_matrix(matrix: Any, skin: dict) -> str:
        n = skin["sensor_count"]
        lines = ["Coupling (µT shift per sensor at full inflation):",
                 "chamber │ " + "  ".join(f"S{s}" for s in range(n))]
        if not matrix.deltas:
            lines.append("(no coupling measured — sensors may be away from "
                         "the chambers, or the sweep collected no samples)")
        for chamber in matrix.chambers:
            row = matrix.deltas.get(chamber, [])
            cells = "  ".join(f"{(row[s] if s < len(row) else 0.0):4.0f}"
                              for s in range(n))
            lines.append(f"slot {chamber:<3}│ {cells}")
        return "\n".join(lines)

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running and bool(self._skins))
        self.skin_combo.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _stop(self) -> None:
        """Abort the sweep and halt every chamber (hold, not vacuum-deflate)."""
        self._tick.stop()
        job, self._job = self._job, None
        if self._gateway is not None and job is not None:
            for slot in job["skin"]["slots"]:
                self._gateway.send(job["skin"]["chamber_mac"], "hold", chamber=slot)
        self._set_running(False)
        if job is not None:
            self.status_label.setText("Stopped.")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        skin = self._current_skin()
        if self._result is None or skin is None:
            QMessageBox.information(self, "Save", "Run a sweep first.")
            return
        set_touch_coupling(self._settings.data, skin["robot_id"],
                           skin["skin_id"], self._result)
        suppress = (float(self.suppress_spin.value())
                    if self.suppress_check.isChecked() else None)
        set_compensation(
            self._settings.data, skin["robot_id"], skin["skin_id"],
            enabled=self.enable_check.isChecked(),
            threshold_ut=float(self.threshold_spin.value()),
            suppress_pct=suppress)
        self._settings.save()
        self.saved.emit()
        QMessageBox.information(
            self, "Save",
            f"Saved touch coupling for {skin['robot_id']}/{skin['skin_id']}.")

    # ------------------------------------------------------------------
    # Gateway plumbing (read thread → Signal → GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        if self._active and data.get("type") in ("status", "magnet"):
            self._msg.emit(data)

    def _on_msg(self, data: dict) -> None:
        job = self._job
        if job is None:
            return
        skin = job["skin"]
        source = data.get("source")
        if data.get("type") == "status" and source == skin["chamber_mac"]:
            ch = data.get("chamber")
            pct = data.get("pressure")
            if isinstance(ch, int) and isinstance(pct, (int, float)):
                self._pressures[ch] = float(pct)
        elif data.get("type") == "magnet" and source == skin["touch_mac"]:
            mag = data.get("mag")
            if isinstance(mag, list):
                self._samples.append((time.monotonic() * 1000.0,
                                      dict(self._pressures),
                                      [float(v) for v in mag]))

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._active = False
        self._stop()
        super().closeEvent(ev)

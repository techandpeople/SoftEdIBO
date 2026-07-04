"""Touch↔chamber coupling calibration dialog (Tools → Calibrate Touch Coupling…).

Measures, per skin, how much inflating each chamber shifts every magnet/touch
sensor (in µT) — at several inflation levels — and stores it as the skin's
``touch.coupling`` curves so the runtime can subtract that actuation offset and
stop a chamber faking a touch (see :mod:`src.core.touch_compensation`).

The sweep sequence itself is a pure :class:`SweepProgram` (rest → per chamber
an ascending staircase of levels → deflate); this dialog only executes its
steps against the gateway, holds fast telemetry on the chamber node for dense
level tracking, collects the ``magnet`` samples (including the 3-axis ``vec``
deltas when the node streams them), and previews/saves the result. Sample
analysis is the Qt-free, unit-tested :mod:`src.core.touch_coupling` /
:mod:`src.hardware.touch_calibration`.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from src.gui.base_dialog import BaseDialog
from src.gui.ui_touch_calibration_dialog import Ui_TouchCalibrationDialog
from src.hardware.fast_telemetry import FastTelemetry
from src.hardware.touch_calibration import (
    SweepProgram,
    coupling_config_from_samples,
    iter_touch_skins,
    set_compensation,
    set_touch_coupling,
    sweep_diagnostics,
)
from src.hardware.units import kpa_to_pct

_TICK_MS = 100


class TouchCalibrationDialog(BaseDialog, Ui_TouchCalibrationDialog):
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
        self._samples: list[tuple] = []
        self._result: dict | None = None
        # Sweep execution state: the pure step program + a cursor over it.
        self._program: SweepProgram | None = None
        self._skin: dict | None = None
        self._step_idx = 0
        self._elapsed = 0
        self._telemetry: FastTelemetry | None = None

        has_targets = bool(self._skins) and gateway is not None
        self.run_btn.setEnabled(has_targets)
        if not self._skins:
            self.status_label.setText(
                "No magnet-capable skins configured (need a node_direct or "
                "node_magnet_sensor touch node).")

        self.run_btn.clicked.connect(self._run)
        self.stop_btn.clicked.connect(self._stop)
        self.apply_btn.clicked.connect(self._on_apply)
        self.save_btn.clicked.connect(self._on_save)
        self.suppress_check.toggled.connect(self.suppress_spin.setEnabled)
        self._set_save_enabled(False)

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
        if self._program is not None or skin is None or not skin["slots"]:
            return
        self._samples.clear()
        self._pressures.clear()
        self._result = None
        self._set_save_enabled(False)
        self.preview.clear()
        self._skin = skin
        self._program = SweepProgram(
            skin["slots"], SweepProgram.levels_for(self.levels_spin.value()))
        self._step_idx = -1
        self._set_running(True)
        # Dense pressure telemetry on the chamber node, so level bins track the
        # staircase closely instead of the 500 ms status cadence.
        self._telemetry = FastTelemetry(self._gateway, skin["chamber_mac"])
        self._telemetry.start()
        # Ask the touch node for 3-axis deltas during the sweep, so the curves
        # can carry offset vectors (harmless no-op on older firmware). Left on
        # afterwards: live vector compensation needs the same stream.
        self._gateway.send(skin["touch_mac"], "configure", stream_vec=True)
        self._advance()
        self._tick.start()

    def _deflate_all(self, skin: dict) -> None:
        for slot in skin["slots"]:
            self._gateway.send(skin["chamber_mac"], "deflate", chamber=slot,
                               delta=100)

    def _execute(self, step) -> None:
        """Send one SweepProgram step to the hardware and show its label."""
        skin = self._skin
        self.progress.setValue(step.progress)
        self.status_label.setText(step.label)
        if step.action == "deflate_all":
            self._deflate_all(skin)
        elif step.action == "set_pressure":
            self._gateway.send(skin["chamber_mac"], "set_pressure",
                               chamber=step.slot, value=step.level)
        elif step.action == "deflate":
            self._gateway.send(skin["chamber_mac"], "deflate",
                               chamber=step.slot, delta=100)

    def _advance(self) -> None:
        """Move the cursor to the next program step (or finish)."""
        self._step_idx += 1
        self._elapsed = 0
        if self._program is None or self._step_idx >= len(self._program.steps):
            self._finish()
            return
        self._execute(self._program.steps[self._step_idx])

    def _on_tick(self) -> None:
        if self._program is None:
            return
        if self._telemetry is not None:
            self._telemetry.keepalive()
        self._elapsed += _TICK_MS
        if self._elapsed >= self._program.steps[self._step_idx].wait_ms:
            self._advance()

    def _finish(self) -> None:
        skin, self._skin = self._skin, None
        self._end_sweep()
        if skin is None:
            return
        self._deflate_all(skin)
        self.progress.setValue(100)
        cfg, matrix = coupling_config_from_samples(
            list(self._samples), skin["sensor_count"])
        self._result = cfg
        preview = self._format_matrix(matrix, skin)
        if not matrix.curves:
            # Nothing classified as an inflated chamber — say why, so the
            # operator can fix the setup instead of guessing.
            preview += "\n\n" + sweep_diagnostics(self._samples, skin["slots"])
        self.preview.setPlainText(preview)
        vec_note = " 3-axis data captured." if matrix.has_vec else ""
        self.status_label.setText(
            f"Done — {len(self._samples)} samples over "
            f"{len(matrix.chambers)} chamber(s).{vec_note} Review, then Save.")
        self._set_save_enabled(True)

    def _end_sweep(self) -> None:
        """Stop the timer, the program cursor and fast telemetry."""
        self._tick.stop()
        self._program = None
        if self._telemetry is not None:
            self._telemetry.stop()
            self._telemetry = None
        self._set_running(False)

    def _set_save_enabled(self, on: bool) -> None:
        """Apply and Save commit the same result, so they enable together."""
        self.apply_btn.setEnabled(on)
        self.save_btn.setEnabled(on)

    @staticmethod
    def _format_matrix(matrix: Any, skin: dict) -> str:
        n = skin["sensor_count"]
        lines = ["Coupling (µT shift per sensor at the strongest level):",
                 "chamber │ " + "  ".join(f"S{s}" for s in range(n))]
        deltas = matrix.deltas
        if not deltas:
            lines.append("(no coupling measured — sensors may be away from "
                         "the chambers, or the sweep collected no samples)")
        for chamber in matrix.chambers:
            row = deltas.get(chamber, [])
            cells = "  ".join(f"{(row[s] if s < len(row) else 0.0):4.0f}"
                              for s in range(n))
            points = matrix.curves.get(chamber, [])
            levels = "/".join(f"{p.level_pct:.0f}" for p in points)
            lines.append(f"slot {chamber:<3}│ {cells}   levels: {levels}%")
        return "\n".join(lines)

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running and bool(self._skins))
        self.skin_combo.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _stop(self) -> None:
        """Abort the sweep and halt every chamber (hold, not vacuum-deflate)."""
        skin, self._skin = self._skin, None
        was_running = self._program is not None
        self._end_sweep()
        if self._gateway is not None and skin is not None:
            for slot in skin["slots"]:
                self._gateway.send(skin["chamber_mac"], "hold", chamber=slot)
        if was_running:
            self.status_label.setText("Stopped.")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _commit(self) -> bool:
        """Persist the measured matrix + settings without closing. Returns True
        on success. Shared by Save (which then closes) and Apply (which stays
        open so the user can re-tune the threshold or re-run the sweep)."""
        skin = self._current_skin()
        if self._result is None or skin is None:
            QMessageBox.information(self, "Save", "Run a sweep first.")
            return False
        set_touch_coupling(self._settings.data, skin["robot_id"],
                           skin["skin_id"], self._result)
        suppress = (float(self.suppress_spin.value())
                    if self.suppress_check.isChecked() else None)
        set_compensation(
            self._settings.data, skin["robot_id"], skin["skin_id"],
            enabled=self.enable_check.isChecked(),
            threshold_ut=float(self.threshold_spin.value()),
            margin_frac=self.margin_spin.value() / 100.0,
            guard_ms=float(self.guard_spin.value()),
            suppress_pct=suppress)
        self._settings.save()
        self.saved.emit()
        self.status_label.setText(
            f"Saved touch coupling for {skin['robot_id']}/{skin['skin_id']}.")
        return True

    def _on_apply(self) -> None:
        self._commit()

    def _on_save(self) -> None:
        if self._commit():
            self.accept()

    # ------------------------------------------------------------------
    # Gateway plumbing (read thread → Signal → GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        if self._active and data.get("type") in ("status", "magnet"):
            self._msg.emit(data)

    def _on_msg(self, data: dict) -> None:
        skin = self._skin
        if self._program is None or skin is None:
            return
        source = data.get("source")
        if data.get("type") == "status" and source == skin["chamber_mac"]:
            self._track_level(skin, data)
        elif data.get("type") == "magnet" and source == skin["touch_mac"]:
            mag = data.get("mag")
            vec = data.get("vec")
            if isinstance(mag, list):
                self._samples.append((time.monotonic() * 1000.0,
                                      dict(self._pressures),
                                      [float(v) for v in mag],
                                      vec if isinstance(vec, list) else None))

    def _track_level(self, skin: dict, data: dict) -> None:
        """Fold one chamber ``status`` into the live per-slot levels (%).

        Prefers the measured ``kpa`` recomputed against the *configured* range:
        the firmware ``pressure`` % is computed against the limits the node
        currently holds, which lag the PC config (8 kPa boot default, a dropped
        set_max) — same policy as AirChamber."""
        ch = data.get("chamber")
        if not isinstance(ch, int):
            return
        kpa = data.get("kpa")
        pct = data.get("pressure")
        if isinstance(kpa, (int, float)):
            lo, hi = skin["limits"].get(ch, (0.0, 0.0))
            self._pressures[ch] = float(kpa_to_pct(float(kpa), lo, hi))
        elif isinstance(pct, (int, float)):
            self._pressures[ch] = float(pct)

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._active = False
        self._stop()
        super().closeEvent(ev)

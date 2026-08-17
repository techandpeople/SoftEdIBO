"""Touch<->chamber coupling calibration dialog (Tools -> Calibrate Touch Coupling...).

Measures, per skin, how much inflating each chamber shifts every magnet/touch
sensor (in uT) - at several inflation levels - and stores it as the skin's
``touch.coupling`` curves so the runtime can subtract that actuation offset and
stop a chamber faking a touch (see :mod:`src.core.touch_compensation`).

The sweep sequence itself is a pure :class:`SweepProgram` (rest -> per chamber
an ascending staircase of levels -> deflate); this dialog only executes its
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
from src.gui.calibration_indicator import CalibrationLedIndicator
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
# Ignore the first part of each state's dwell (pumps still moving) before judging
# whether a chamber that should be vented actually vented.
_SETTLE_MARGIN_MS = 1500
# A non-target chamber reading above this % during a measurement is "not vented"
# - it contaminates the state (and would fake a co-inflation combination).
_VENT_MAX = 15.0


class TouchCalibrationDialog(BaseDialog, Ui_TouchCalibrationDialog):
    """Measure and store the per-skin touch<->chamber coupling matrix."""

    # gateway read thread -> GUI thread (so samples are collected single-threaded)
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
        # Softly pulses every actuator ring while the sweep runs so the rig
        # visibly reads as "busy calibrating". Driven from _set_running.
        self._led = CalibrationLedIndicator(settings, gateway)

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
        # Per-state target chambers, and the worst level a *non-target* chamber
        # held while measuring (a chamber that will not vent - see _finish).
        self._targets: set[int] = set()
        self._measuring = False
        self._contam: dict[int, float] = {}
        # Per-chamber atmospheric tare (running min kPa): the pressure sensor
        # reads a nonzero offset at true ambient (no zero calibration), so a
        # vented chamber would otherwise look inflated. See _track_level.
        self._tare: dict[int, float] = {}

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
        self.step_spin.valueChanged.connect(self._update_estimate)
        self.skin_combo.currentIndexChanged.connect(self._update_estimate)
        self._set_save_enabled(False)
        self._update_estimate()

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

    def _update_estimate(self) -> None:
        """Show how many states and roughly how long the sweep will take for the
        current skin and grid step (all combinations, up to the chamber count)."""
        skin = self._current_skin()
        if skin is None or not skin.get("slots"):
            self.est_label.setText("")
            return
        prog = SweepProgram(skin["slots"], step_pct=self.step_spin.value())
        secs = prog.est_seconds
        span = f"{secs:.0f} s" if secs < 90 else f"~{secs / 60:.0f} min"
        self.est_label.setText(f"~= {prog.n_states} states, {span}")

    def _run(self) -> None:
        skin = self._current_skin()
        if self._program is not None or skin is None or not skin["slots"]:
            return
        self._samples.clear()
        self._pressures.clear()
        self._contam.clear()
        self._tare.clear()
        self._result = None
        self._set_save_enabled(False)
        self.preview.clear()
        self._skin = skin
        # All chamber combinations (up to the chamber count) over the chosen
        # per-member grid step; max_order left unbounded.
        self._program = SweepProgram(skin["slots"], step_pct=self.step_spin.value())
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

    def _rest_all(self, skin: dict) -> None:
        """Passively vent every chamber to ambient by opening the **DEFLATE**
        valve (side 1), no pump - the natural release path, so even a still-
        inflated chamber bleeds down to atmosphere (the inflate side pushes air
        back into the dead-ended pressure manifold and wouldn't vent). One frame
        per chamber; it holds for the manual dead-man (5 s), longer than a rest
        dwell, so no keepalive is needed."""
        for slot in skin["slots"]:
            self._gateway.send(skin["chamber_mac"], "valve_manual",
                               chamber=slot, side=1, open=1)

    def _execute(self, step) -> None:
        """Send one SweepProgram step to the hardware and show its label.

        A ``rest`` step passively vents every chamber to ambient (no pump); a
        ``state`` step first releases the vent's manual valves (so the engine owns
        them cleanly), then drives its target chambers to their levels and *holds*
        (closes, pumps off) the others."""
        skin = self._skin
        if skin is None:
            return
        mac = skin["chamber_mac"]
        self.progress.setValue(step.progress)
        self.status_label.setText(step.label)
        self._targets = set(step.levels)
        self._measuring = step.action == "state"
        if step.action == "rest":
            self._rest_all(skin)
            return
        for slot in skin["slots"]:
            self._gateway.send(mac, "valve_manual", chamber=slot, side=1, open=0)
        for slot in skin["slots"]:
            if slot in step.levels:
                self._gateway.send(mac, "set_pressure",
                                   chamber=slot, value=step.levels[slot])
            else:
                self._gateway.send(mac, "hold", chamber=slot)

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
        self._check_vented()
        if self._elapsed >= self._program.steps[self._step_idx].wait_ms:
            self._advance()

    def _check_vented(self) -> None:
        """While measuring a state (past the pump-settle margin), record the
        worst level of any chamber that should be vented but is not - a chamber
        whose valve/deflate is stuck, which would fake a co-inflation."""
        if not self._measuring or self._elapsed < _SETTLE_MARGIN_MS:
            return
        for slot, level in self._pressures.items():
            if slot not in self._targets and level > _VENT_MAX:
                self._contam[slot] = max(self._contam.get(slot, 0.0), level)

    def _finish(self) -> None:
        skin, self._skin = self._skin, None
        self._end_sweep()
        if skin is None:
            return
        self._rest_all(skin)
        self.progress.setValue(100)
        cfg, model = coupling_config_from_samples(
            list(self._samples), skin["sensor_count"])
        self._result = cfg
        preview = self._format_matrix(model, skin)
        # Diagnose whenever a *swept* slot is missing from the model - not only
        # the fully-empty case - so a single dropped chamber says why instead of
        # silently reporting fewer chambers than were swept.
        if len(model.chambers) < len(skin["slots"]):
            preview += "\n\n" + sweep_diagnostics(
                self._samples, skin["slots"], model.chambers)
        if self._contam:
            stuck = ", ".join(f"chamber {c} (up to {lvl:.0f}%)"
                              for c, lvl in sorted(self._contam.items()))
            preview = (f"WARNING: Did not vent while other chambers were measured: "
                       f"{stuck}. Its valve/deflate is likely stuck, so those "
                       f"states are contaminated (spurious combinations). Fix "
                       f"venting, then re-run.\n\n") + preview
        self.preview.setPlainText(preview)
        vec_note = " 3-axis data captured." if model.has_vec else ""
        combos = len(model.combos())
        combo_note = f" {combos} combination(s)." if combos else ""
        self.status_label.setText(
            f"Done - {len(self._samples)} samples over "
            f"{len(model.chambers)} chamber(s).{combo_note}{vec_note} "
            "Review, then Save.")
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
    def _format_matrix(model: Any, skin: dict) -> str:
        n = skin["sensor_count"]
        lines = ["Coupling (uT shift per sensor at the strongest level):",
                 "chamber | " + "  ".join(f"S{s}" for s in range(n))]
        singles = model.singles()
        deltas = model.deltas()
        if not singles:
            lines.append("(no coupling measured - sensors may be away from "
                         "the chambers, or the sweep collected no samples)")
        for chamber in sorted(singles):
            row = deltas.get(chamber, [])
            cells = TouchCalibrationDialog._cells(row, n)
            lines.append(f"slot {chamber:<3}| {cells}   @{singles[chamber].levels[chamber]:.0f}%")
        combos = model.combos()
        if combos:
            lines.append("")
            lines.append("Combinations (uT shift, non-additive):")
            for st in combos:
                who = "+".join(f"{c}@{st.levels[c]:.0f}%" for c in sorted(st.chambers))
                lines.append(f"{who:<14}| {TouchCalibrationDialog._cells(st.mag, n)}")
        return "\n".join(lines)

    @staticmethod
    def _cells(row: Any, n: int) -> str:
        row = list(row or [])
        return "  ".join(f"{(row[s] if s < len(row) else 0.0):4.0f}"
                         for s in range(n))

    def _set_running(self, running: bool) -> None:
        # Pulse every ring while the sweep runs, fade off when it ends/aborts.
        self._led.on() if running else self._led.off()
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
        # Threshold is set from the sensor tester (act_threshold_ut) - the single
        # source; clear any stale compensation.threshold_ut so it falls back there.
        set_compensation(
            self._settings.data, skin["robot_id"], skin["skin_id"],
            enabled=self.enable_check.isChecked(),
            clear_threshold=True,
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
    # Gateway plumbing (read thread -> Signal -> GUI thread)
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

        Recomputes % from the measured ``kpa`` against the *configured* range
        (the firmware ``pressure`` % lags the limits the node holds), minus a
        per-chamber **atmospheric tare**. The XGZP6847A has no zero calibration,
        so at true ambient it reads a few kPa of offset - without taring, a
        *vented* chamber would read ~20 % and be misclassified as inflated (the
        false 'residual' that faked co-inflation combinations). The running
        minimum kPa is that chamber's ambient floor; because the magnet baseline
        is captured at the same floor, its constant contribution cancels."""
        ch = data.get("chamber")
        if not isinstance(ch, int):
            return
        kpa = data.get("kpa")
        pct = data.get("pressure")
        if isinstance(kpa, (int, float)):
            kpa = float(kpa)
            self._tare[ch] = min(self._tare.get(ch, kpa), kpa)
            # Reference ATMOSPHERE (0 kPa) as 0 %, NOT the chamber's configured
            # min: a vacuum-capable chamber has min < 0 (e.g. -5), so against it a
            # *vented* chamber at atmosphere reads ~20-25 % and would look inflated
            # - turning every state into a spurious triple and firing a false
            # "did not vent" warning. Coupling cares about inflation above ambient.
            _lo, hi = skin["limits"].get(ch, (0.0, 8.0))
            self._pressures[ch] = float(kpa_to_pct(kpa - self._tare[ch], 0.0, hi))
        elif isinstance(pct, (int, float)):
            self._pressures[ch] = float(pct)

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._active = False
        self._stop()
        super().closeEvent(ev)

"""Fill-curve calibration dialog (Tools → Calibrate Fill Times…).

Measures, per actuator chamber, its **time→pressure fill curve** — how the
pressure climbs as the inflate valve is held open — using the pressure sensor as
ground truth, and stores it as the chamber's ``fill_profile`` in settings. At
runtime the app converts an inflate target into an open-valve time from that
curve, so the firmware doesn't have to close the loop on the laggy multiplexed
pressure sensor (the firmware ``HARD_MAX`` cutoff + a total-time ceiling stay as
safety nets).

Flow per chamber (one at a time, driven by a timer + gateway status messages):
  1. **Deflate** to empty so the sweep starts at ambient.
  2. **Step** the inflate valve open for a fixed window, let the sensor settle,
     read the pressure %, record a curve point — repeating until the chamber
     reaches the target or a total-time ceiling.
  3. Record the curve, deflate back, show the result.

Re-running a chamber with a smaller step size refines its curve. The live kPa of
each chamber is shown next to its progress, alongside the percentage.

The measurement maths live in the Qt-free :mod:`src.hardware.fill_calibration`
so they're unit-tested; this dialog only drives the hardware and the UI.
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from src.gui.ui_fill_calibration_dialog import Ui_FillCalibrationDialog
from src.hardware.fill_calibration import (
    DEFAULT_STEP_MS,
    STEP_CHOICES_MS,
    FillProfileCalibrator,
    iter_actuator_chambers,
    set_fill_profile,
)
from src.hardware.fill_profile import FillProfile

# How empty the chamber must read before we start the sweep, and how long we'll
# wait for that before giving up on the deflate phase.
_EMPTY_PCT = 5.0
_MAX_DEFLATE_MS = 7000
# Extra settle time after each step's valve-open window, to let the laggy sensor
# catch up before we read the pressure for that step.
_SETTLE_MS = 350
_TICK_MS = 100

# Human labels for the step-size choices (ms → label).
_STEP_LABELS = {600.0: "Coarse", 400.0: "Medium", 250.0: "Fine", 150.0: "Very fine"}


class FillCalibrationDialog(QDialog, Ui_FillCalibrationDialog):
    """Calibrate per-chamber fill curves against the pressure sensor."""

    # gateway read thread → GUI thread: (mac, chamber, pressure_pct, kpa)
    _pressure = Signal(str, int, float, float)
    # Emitted after fill curves are written to settings, so the app can rebuild
    # robots to pick up the new ``fill_profile`` values.
    saved = Signal()

    def __init__(self, settings: Any, gateway: Any,
                 parent: QWidget | None = None,
                 chambers: list[dict] | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._settings = settings
        self._gateway = gateway
        self._active = True
        # ``chambers`` lets a caller scope the dialog to a subset (e.g. one
        # skin's chambers from the skin config dialog). When omitted, calibrate
        # every actuator chamber across all configured robots.
        self._chambers = (chambers if chambers is not None
                          else iter_actuator_chambers(settings.data))
        # measured results: (mac, slot) → fill_profile list ([[ms, pct], ...])
        self._results: dict[tuple[str, int], list[list[float]]] = {}
        # currently-running calibration job, or None
        self._job: dict | None = None
        self._rows: dict[tuple[str, int], dict] = {}

        for ms in STEP_CHOICES_MS:
            label = _STEP_LABELS.get(ms, f"{int(ms)} ms")
            self.step_combo.addItem(f"{label} — {int(ms)} ms", userData=float(ms))
        self._select_step(DEFAULT_STEP_MS)

        # The static frame (intro, scroll area, buttons) lives in the .ui; the
        # per-chamber rows are built here and added to ``rows_layout``.
        if not self._chambers:
            self.rows_layout.addWidget(QLabel(
                "No actuator chambers configured. Add node_direct / "
                "node_multiplexed chambers first."))
        else:
            for ch in self._chambers:
                self.rows_layout.addWidget(self._build_row(ch))
        self.rows_layout.addStretch(1)

        self.all_btn.setEnabled(bool(self._chambers) and gateway is not None)
        self.all_btn.clicked.connect(self._calibrate_all)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._pressure.connect(self._on_pressure)
        if gateway is not None:
            gateway.on_message(self._on_gateway_message)
        self.finished.connect(lambda _=0: self._stop())

    def _select_step(self, ms: float) -> None:
        idx = self.step_combo.findData(float(ms))
        if idx >= 0:
            self.step_combo.setCurrentIndex(idx)

    def _step_ms(self) -> float:
        data = self.step_combo.currentData()
        return float(data) if data is not None else DEFAULT_STEP_MS

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_row(self, ch: dict) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        key = (ch["mac"], ch["slot"])
        name = QLabel(f"{ch['robot_id']}/{ch['skin_id']}  {ch['mac']} slot {ch['slot']}")
        name.setToolTip(name.text())
        name.setMinimumWidth(80)
        # Ignored width policy lets the label shrink below its text (clipping its
        # own text) so a narrow dialog never pushes the buttons off the row.
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setMaximumHeight(10)
        # Live kPa readout (firmware reports it per chamber); "—" until a status
        # message arrives. Fixed width so rows stay aligned.
        kpa = QLabel("—")
        kpa.setFixedWidth(80)
        kpa.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        kpa.setToolTip("Live chamber pressure in kPa.")
        # Calibration result (full fill time / timeout). Pre-fill from any stored
        # curve so the user sees what's already calibrated.
        result = QLabel(self._result_text(ch))
        result.setFixedWidth(120)
        result.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        btn = QPushButton("Calibrate")
        btn.setEnabled(self._gateway is not None)
        btn.clicked.connect(lambda _=False, k=key: self._calibrate_one(k))
        h.addWidget(name, stretch=2)
        h.addWidget(bar, stretch=3)
        h.addWidget(kpa)
        h.addWidget(result)
        h.addWidget(btn)
        self._rows[key] = {"result": result, "bar": bar, "btn": btn,
                           "kpa": kpa, "cfg": ch}
        return w

    @staticmethod
    def _result_text(ch: dict) -> str:
        prof = FillProfile.from_list(ch.get("fill_profile"))
        if prof is not None:
            return f"{int(round(prof.full_time_ms))} ms"
        ms = ch.get("fill_time_ms")
        return f"{int(ms)} ms" if ms else "—"

    # ------------------------------------------------------------------
    # Calibration driving
    # ------------------------------------------------------------------

    def _calibrate_one(self, key: tuple[str, int], *, queue: list | None = None) -> None:
        if self._job is not None:
            return                       # one at a time
        row = self._rows[key]
        row["bar"].setValue(0)
        row["result"].setText("…")
        self._set_buttons_enabled(False)
        self._job = {
            "key": key, "mac": key[0], "slot": key[1], "phase": "deflate",
            "cal": FillProfileCalibrator(step_ms=self._step_ms()),
            "phase_elapsed": 0, "last_pct": 100.0, "queue": queue,
        }
        # Start at ambient: deflate and wait until the chamber reads empty.
        self._gateway.send(key[0], "deflate", chamber=key[1])
        self._tick.start()

    def _calibrate_all(self) -> None:
        if self._job is not None:
            return
        queue = list(self._rows.keys())
        first = queue.pop(0)
        self._calibrate_one(first, queue=queue)

    def _begin_step(self, job: dict) -> None:
        """Open the inflate valve for one step window and wait for it to settle."""
        job["phase"] = "step"
        job["phase_elapsed"] = 0
        # Time-based fill for exactly one step: the firmware opens the inflate
        # valve for ``ms`` then closes it (HARD_MAX is the only pressure cutoff).
        self._gateway.send(job["mac"], "inflate", chamber=job["slot"],
                           ms=int(job["cal"].step_ms))

    def _on_tick(self) -> None:
        job = self._job
        if job is None:
            return
        job["phase_elapsed"] += _TICK_MS
        if job["phase"] == "deflate":
            # Once empty (or after a bounded wait) begin the inflate sweep.
            if job["last_pct"] <= _EMPTY_PCT or job["phase_elapsed"] >= _MAX_DEFLATE_MS:
                self._begin_step(job)
        elif job["phase"] == "step":
            # After the valve-open window + a settle, record the settled reading.
            if job["phase_elapsed"] >= job["cal"].step_ms + _SETTLE_MS:
                done = job["cal"].record(job["last_pct"])
                self._rows[job["key"]]["bar"].setValue(
                    int(max(0.0, min(100.0, job["cal"].profile.top_pct))))
                if done:
                    self._finish_job()
                else:
                    self._begin_step(job)

    def _on_pressure(self, mac: str, chamber: int, pct: float, kpa: float) -> None:
        # Keep every row's live kPa current, whichever chamber is being swept.
        row = self._rows.get((mac, chamber))
        if row is not None:
            row["kpa"].setText(f"{kpa:.2f} kPa" if not math.isnan(kpa) else f"{pct:.0f}%")
        job = self._job
        if job is None or mac != job["mac"] or chamber != job["slot"]:
            return
        job["last_pct"] = pct
        if job["phase"] == "deflate":
            self._rows[job["key"]]["bar"].setValue(int(max(0.0, min(100.0, pct))))

    def _finish_job(self) -> None:
        job = self._job
        if job is None:
            return
        self._job = None
        self._tick.stop()
        cal = job["cal"]
        key = job["key"]
        row = self._rows[key]
        profile = cal.profile
        self._results[key] = profile.to_list()
        if cal.timed_out:
            row["result"].setText(f"≥{int(round(profile.full_time_ms))} ms")
            row["result"].setToolTip(
                f"Timed out at {int(profile.top_pct)}% — chamber did not reach "
                "the target. Curve still saved up to that point.")
        else:
            row["result"].setText(f"{int(round(profile.full_time_ms))} ms ✓")
            row["result"].setToolTip(
                f"Reached {int(profile.top_pct)}% over {cal.steps} steps.")
        # Deflate the (now inflated) chamber back to a safe resting state.
        self._gateway.send(job["mac"], "deflate", chamber=job["slot"])
        queue = job["queue"]
        if queue:
            nxt = queue.pop(0)
            QTimer.singleShot(400, lambda k=nxt, q=queue:
                              self._calibrate_one(k, queue=q))
        else:
            self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, on: bool) -> None:
        self.all_btn.setEnabled(on and bool(self._chambers))
        self.step_combo.setEnabled(on)
        for r in self._rows.values():
            r["btn"].setEnabled(on)

    def _stop(self) -> None:
        """Abort any running calibration and halt everything touched.

        Sends ``hold`` (close valves, pumps off), NOT ``deflate``: the deflate
        pump is an active vacuum, so deflating on Stop would spin the vacuum
        pump rather than simply stopping. Stop should halt, not actuate.
        """
        self._tick.stop()
        self._job = None
        # Best-effort: halt every chamber so nothing is left actuating.
        if self._gateway is not None:
            for (mac, slot) in self._rows:
                self._gateway.send(mac, "hold", chamber=slot)
        self._set_buttons_enabled(True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Save", "Nothing calibrated yet.")
            return
        for (mac, slot), profile in self._results.items():
            set_fill_profile(self._settings.data, mac, slot, profile)
        self._settings.save()
        self.saved.emit()
        QMessageBox.information(
            self, "Save", f"Saved fill curves for {len(self._results)} chamber(s).")

    # ------------------------------------------------------------------
    # Gateway plumbing (read thread → Signal → GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        if not self._active:
            return
        if data.get("type") != "status":
            return
        mac = data.get("source")
        chamber = data.get("chamber")
        pressure = data.get("pressure")
        kpa = data.get("kpa")
        if isinstance(mac, str) and isinstance(chamber, int) \
                and isinstance(pressure, (int, float)):
            self._pressure.emit(
                mac, chamber, float(pressure),
                float(kpa) if isinstance(kpa, (int, float)) else float("nan"))

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._active = False
        self._stop()
        super().closeEvent(ev)

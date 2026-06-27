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

from itertools import combinations

from src.gui.ui_fill_calibration_dialog import Ui_FillCalibrationDialog
from src.hardware.fill_calibration import (
    DEFAULT_STEP_MS,
    STEP_CHOICES_MS,
    MultiChamberFillCalibrator,
    combo_key,
    iter_actuator_chambers,
    set_fill_profile,
    set_fill_profiles,
)
from src.hardware.fill_profile import FillProfile

# How empty the chamber must read before we start the sweep, and how long we'll
# wait for that before giving up on the deflate phase.
_EMPTY_PCT = 5.0
_MAX_DEFLATE_MS = 7000
# Adaptive settle after each step's valve-open window: the multiplexed sensor is
# laggy, so instead of a fixed wait we hold until the reading stops moving
# (|Δpct| < _STABLE_DELTA_PCT for _STABLE_TICKS consecutive ticks), bounded by a
# floor (let the sensor begin to move) and a hard ceiling (never wait forever).
_STABLE_DELTA_PCT = 1.0
_STABLE_TICKS = 3
_MIN_SETTLE_MS = 250
_MAX_SETTLE_MS = 2500
_TICK_MS = 100
# Rough per-sweep time estimate, only for the "Calibrate all" confirmation.
_EST_SECS_PER_RUN = 12

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
        # measured solo results: (mac, slot) → fill_profile list ([[ms, pct], ...])
        self._results: dict[tuple[str, int], list[list[float]]] = {}
        # measured combination results: (mac, slot) → {combo_key: curve}, the
        # chamber's fill curve under each co-active slot set it was swept in.
        self._combo_results: dict[tuple[str, int], dict[str, list[list[float]]]] = {}
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

    def _calibrate_one(self, key: tuple[str, int]) -> None:
        """Per-row button: a solo (single-chamber) sweep."""
        if self._job is not None:
            return                       # one at a time
        self._run_queue([{"mac": key[0], "slots": [key[1]], "idx": 1, "total": 1}])

    def _calibrate_all(self) -> None:
        if self._job is not None:
            return
        specs = self._build_all_specs()
        if not specs:
            return
        combos = sum(1 for s in specs if len(s["slots"]) > 1)
        if combos:
            est = len(specs) * _EST_SECS_PER_RUN
            if QMessageBox.question(
                    self, "Calibrate all",
                    f"This runs {len(specs)} sweeps — every chamber alone plus all "
                    f"{combos} multi-chamber combinations (chambers sharing a pump "
                    f"fill slower together). Roughly ~{est // 60}m{est % 60:02d}s.\n\n"
                    "Each set is inflated from empty. Keep hands clear; Stop aborts "
                    "at any time.") != QMessageBox.StandardButton.Yes:
                return
        self._run_queue(specs)

    def _build_all_specs(self) -> list[dict]:
        """All sweeps for "Calibrate all": per node, every non-empty subset of its
        chambers (solos + combinations). Combinations are per node because pumps
        are shared per node."""
        groups: dict[str, list[int]] = {}
        for ch in self._chambers:
            groups.setdefault(ch["mac"], []).append(int(ch["slot"]))
        specs: list[dict] = []
        for mac, slots in groups.items():
            slots = sorted(set(slots))
            for r in range(1, len(slots) + 1):
                for combo in combinations(slots, r):
                    specs.append({"mac": mac, "slots": list(combo)})
        total = len(specs)
        for i, sp in enumerate(specs, 1):
            sp["idx"], sp["total"] = i, total
        return specs

    def _run_queue(self, specs: list[dict]) -> None:
        """Start the first spec, carrying the rest as the queue."""
        if not specs:
            self._set_buttons_enabled(True)
            self.combo_status.setText("")
            return
        spec = specs[0]
        self._start_job(spec["mac"], spec["slots"], queue=specs[1:],
                        combo_index=(spec["idx"], spec["total"]))

    def _start_job(self, mac: str, slots: list[int], *, queue: list[dict],
                   combo_index: tuple[int, int]) -> None:
        if self._job is not None:
            return
        slots = sorted(int(s) for s in slots)
        for s in slots:
            row = self._rows.get((mac, s))
            if row is not None:
                row["bar"].setValue(0)
                if len(slots) == 1:
                    row["result"].setText("…")
        self._set_buttons_enabled(False)
        self._job = {
            "mac": mac, "slots": slots, "phase": "deflate", "phase_elapsed": 0,
            "cal": MultiChamberFillCalibrator(slots, step_ms=self._step_ms()),
            "last_pct": dict.fromkeys(slots, 100.0), "settle": {},
            "queue": queue, "combo_index": combo_index,
        }
        self._update_combo_status(mac, slots, combo_index)
        # Start at ambient: deflate every slot in the set and wait until empty.
        for s in slots:
            self._gateway.send(mac, "deflate", chamber=s)
        self._tick.start()

    def _begin_step(self, job: dict) -> None:
        """Open the inflate valves of the still-unfinished slots for one step."""
        job["phase"] = "step"
        job["phase_elapsed"] = 0
        # Time-based fill for exactly one step: the firmware opens each inflate
        # valve for ``ms`` then closes it (HARD_MAX is the only pressure cutoff).
        for s in job["cal"].pending_slots():
            self._gateway.send(job["mac"], "inflate", chamber=s,
                               ms=int(job["cal"].step_ms))

    def _begin_settle(self, job: dict) -> None:
        """Start watching the (now valve-closed) readings settle before recording."""
        job["phase"] = "settle"
        job["phase_elapsed"] = 0
        job["settle"] = {s: {"prev": job["last_pct"][s], "stable": 0}
                         for s in job["cal"].pending_slots()}

    def _on_tick(self) -> None:
        job = self._job
        if job is None:
            return
        job["phase_elapsed"] += _TICK_MS
        if job["phase"] == "deflate":
            empty = all(job["last_pct"][s] <= _EMPTY_PCT for s in job["slots"])
            if empty or job["phase_elapsed"] >= _MAX_DEFLATE_MS:
                self._begin_step(job)
        elif job["phase"] == "step":
            # Wait out the firmware's valve-open window, then let the sensor settle.
            if job["phase_elapsed"] >= job["cal"].step_ms:
                self._begin_settle(job)
        elif job["phase"] == "settle":
            self._update_settle(job)
            if self._settle_ready(job):
                self._record_step(job)

    def _update_settle(self, job: dict) -> None:
        """Per-tick stability count: a slot is stable once its reading stops moving."""
        for s in job["cal"].pending_slots():
            st = job["settle"].setdefault(s, {"prev": job["last_pct"][s], "stable": 0})
            if abs(job["last_pct"][s] - st["prev"]) < _STABLE_DELTA_PCT:
                st["stable"] += 1
            else:
                st["stable"] = 0
            st["prev"] = job["last_pct"][s]

    @staticmethod
    def _settle_ready(job: dict) -> bool:
        if job["phase_elapsed"] >= _MAX_SETTLE_MS:
            return True                  # ceiling — record whatever we have
        if job["phase_elapsed"] < _MIN_SETTLE_MS:
            return False                 # floor — let the laggy sensor start moving
        return all(job["settle"].get(s, {}).get("stable", 0) >= _STABLE_TICKS
                   for s in job["cal"].pending_slots())

    def _record_step(self, job: dict) -> None:
        cal = job["cal"]
        readings = {s: job["last_pct"][s] for s in cal.pending_slots()}
        done = cal.record(readings)
        for s in job["slots"]:
            row = self._rows.get((job["mac"], s))
            if row is not None:
                row["bar"].setValue(int(max(0.0, min(
                    100.0, cal.calibrator(s).profile.top_pct))))
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
        if job is None or mac != job["mac"] or chamber not in job["last_pct"]:
            return
        job["last_pct"][chamber] = pct
        if job["phase"] == "deflate" and row is not None:
            row["bar"].setValue(int(max(0.0, min(100.0, pct))))

    def _finish_job(self) -> None:
        job = self._job
        if job is None:
            return
        self._job = None
        self._tick.stop()
        cal = job["cal"]
        mac, slots = job["mac"], job["slots"]
        profiles = cal.profiles()
        if len(slots) == 1:
            s = slots[0]
            self._results[(mac, s)] = profiles[s]
            self._set_solo_result(mac, s, cal.calibrator(s))
        else:
            key = combo_key(slots)
            for s in slots:
                self._combo_results.setdefault((mac, s), {})[key] = profiles[s]
        # Deflate the (now inflated) chambers back to a safe resting state.
        for s in slots:
            self._gateway.send(mac, "deflate", chamber=s)
        queue = job["queue"]
        if queue:
            QTimer.singleShot(400, lambda q=queue: self._run_queue(q))
        else:
            self.combo_status.setText("")
            self._set_buttons_enabled(True)

    def _set_solo_result(self, mac: str, slot: int, cal: Any) -> None:
        row = self._rows.get((mac, slot))
        if row is None:
            return
        profile = cal.profile
        if cal.timed_out:
            row["result"].setText(f"≥{int(round(profile.full_time_ms))} ms")
            row["result"].setToolTip(
                f"Timed out at {int(profile.top_pct)}% — chamber did not reach "
                "the target. Curve still saved up to that point.")
        else:
            row["result"].setText(f"{int(round(profile.full_time_ms))} ms ✓")
            row["result"].setToolTip(
                f"Reached {int(profile.top_pct)}% over {cal.steps} steps.")

    def _update_combo_status(self, mac: str, slots: list[int],
                             combo_index: tuple[int, int]) -> None:
        idx, total = combo_index
        kind = "solo" if len(slots) == 1 else "combo"
        slot_txt = ",".join(str(s) for s in slots)
        self.combo_status.setText(
            f"Run {idx}/{total} — {mac} slots {slot_txt} ({kind})")

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
        self.combo_status.setText("")
        # Best-effort: halt every chamber so nothing is left actuating.
        if self._gateway is not None:
            for (mac, slot) in self._rows:
                self._gateway.send(mac, "hold", chamber=slot)
        self._set_buttons_enabled(True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if not self._results and not self._combo_results:
            QMessageBox.information(self, "Save", "Nothing calibrated yet.")
            return
        for (mac, slot), profile in self._results.items():
            set_fill_profile(self._settings.data, mac, slot, profile)
        for (mac, slot), combos in self._combo_results.items():
            set_fill_profiles(self._settings.data, mac, slot, combos)
        self._settings.save()
        self.saved.emit()
        n = len(set(self._results) | set(self._combo_results))
        QMessageBox.information(
            self, "Save", f"Saved fill curves for {n} chamber(s).")

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

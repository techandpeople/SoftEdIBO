"""Fill-curve calibration dialog (Tools → Calibrate Fill Times…).

Measures, per actuator chamber, its **time→pressure fill curve** — how the
pressure climbs as the inflate valve is held open — using the pressure sensor as
ground truth, and stores it as the chamber's ``fill_profile`` in settings. At
runtime the app converts an inflate target into an open-valve time from that
curve, so the firmware doesn't have to close the loop on the laggy multiplexed
pressure sensor (the firmware ``HARD_MAX`` cutoff + a total-time ceiling stay as
safety nets).

The sweep is **continuous**: the inflate valve is held open (a bench ``test_run``)
while the node streams pressure at a fast cadence (``status_rate`` — see
:class:`~src.hardware.fast_telemetry.FastTelemetry`), timestamping each reading
into the curve. One valve-open pass per chamber, no per-step settle.

Flow per chamber (one at a time, driven by a timer + gateway status messages):
  1. **Deflate** to empty so the sweep starts at ambient.
  2. **Sweep**: hold the inflate valve open and record ``(elapsed_ms, pressure%)``
     from the fast telemetry until the chamber reaches the target or a time ceiling.
  3. Record the curve, deflate back, show the result and its fill-order rank.

The measurement maths live in the Qt-free :mod:`src.hardware.fill_calibration`
so they're unit-tested; this dialog only drives the hardware and the UI.
"""

from __future__ import annotations

import math
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from src.gui.base_dialog import BaseDialog
from src.gui.calibration_indicator import CalibrationLedIndicator
from src.gui.ui_fill_calibration_dialog import Ui_FillCalibrationDialog
from src.hardware.fast_telemetry import FastTelemetry
from src.hardware.fill_calibration import (
    DEFAULT_TARGET_PCT,
    DEFAULT_TIMEOUT_MS,
    ContinuousDeflateCalibrator,
    ContinuousFillCalibrator,
    PlateauDetector,
    get_type_min_duty,
    get_type_profile,
    iter_actuator_chambers,
    set_deflate_profile,
    set_duty_curve,
    set_fill_profile,
    set_type_deflate_profile,
    set_type_min_duty,
    set_type_profile,
    type_slug,
)
from src.hardware.fill_profile import FillProfile
from src.hardware.fill_scaling import duty_sweep

# Every sweep — and especially each duty step of a duty-curve run — MUST fill from
# the same empty baseline or their times aren't comparable. Ambient does NOT read
# 0 kPa on this gauge (it sits ~6 kPa), so instead of a fixed threshold we deflate
# until the reading stops dropping — i.e. it has reached its floor (true ambient,
# whatever value that is on this sensor). "Stopped dropping" = no further drop of at
# least _DEFLATE_MIN_DROP_KPA for _DEFLATE_SETTLE_MS, after a short minimum so the
# vacuum can start pulling. Bounded by _MAX_DEFLATE_MS so a stuck sensor can't hang.
_DEFLATE_MIN_DROP_KPA = 0.3
_DEFLATE_SETTLE_MS = 600
_MIN_DEFLATE_MS = 400
_EMPTY_PCT = 5.0                 # fallback floor when no kPa is reported
_MAX_DEFLATE_MS = 7000
_TICK_MS = 100
# Re-send test_run + status_rate this often to refresh the firmware dead-mans
# (both revert after ~3 s without a keepalive).
_KEEPALIVE_MS = 1000
# Wall-clock grace past the curve's own time ceiling before we force a stuck sweep
# to finish (e.g. the fast telemetry never arrived because the node wasn't flashed).
_SWEEP_GRACE_MS = 2000

# Telemetry cadence choices offered by the "Detail" combo (label → ms). Finer =
# more curve points at a little more radio traffic.
_RATE_CHOICES: tuple[tuple[str, int], ...] = (("Fine", 20), ("Normal", 40), ("Coarse", 60))
_DEFAULT_RATE_MS = 40

# PWM duties swept (fastest → slowest) when measuring a chamber's duty→fill-speed
# curve. Each is swept from empty; the fastest is the reference for the slowdown.
# Geometrically spaced from full duty down to the stall floor (see duty_sweep):
# the pump's response is exponential, so the points bunch up near the floor where
# the curve is steep rather than wasting samples on duties too low to move air.
_DUTY_SWEEP: tuple[int, ...] = duty_sweep()

# Deflate-curve sweep: prefill the chamber to about this level before recording
# the falling curve, giving up (and sweeping from wherever it got) after the cap.
_PREFILL_TARGET_PCT = 95.0
_MAX_PREFILL_MS = 12000


class FillCalibrationDialog(BaseDialog, Ui_FillCalibrationDialog):
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
        # Softly pulses every actuator ring while a sweep runs so the rig
        # visibly reads as "busy calibrating". Driven from _set_buttons_enabled.
        self._led = CalibrationLedIndicator(settings, gateway)
        # ``chambers`` lets a caller scope the dialog to a subset (e.g. one
        # skin's chambers from the skin config dialog). When omitted, calibrate
        # every actuator chamber across all configured robots.
        self._chambers = (chambers if chambers is not None
                          else iter_actuator_chambers(settings.data))
        # measured solo results: (mac, slot) → fill_profile list ([[ms, pct], ...])
        self._results: dict[tuple[str, int], list[list[float]]] = {}
        # measured duty→speed sweeps: (mac, slot) → [[duty, full_time_ms], ...]
        self._duty_results: dict[tuple[str, int], list[list[float]]] = {}
        # measured falling deflate curves: (mac, slot) → [[ms, pct], ...]
        self._deflate_results: dict[tuple[str, int], list[list[float]]] = {}
        # currently-running calibration job, or None
        self._job: dict | None = None
        self._rows: dict[tuple[str, int], dict] = {}

        for label, ms in _RATE_CHOICES:
            self.detail_combo.addItem(f"{label} — {ms} ms", userData=int(ms))
        idx = self.detail_combo.findData(_DEFAULT_RATE_MS)
        self.detail_combo.setCurrentIndex(idx if idx >= 0 else 0)

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
        self._refresh_ranks()
        self._prefill_min_power()

        self.all_btn.setEnabled(bool(self._chambers) and gateway is not None)
        self.duty_btn.setEnabled(bool(self._chambers) and gateway is not None)
        self.deflate_btn.setEnabled(bool(self._chambers) and gateway is not None)
        self.all_btn.clicked.connect(self._calibrate_all)
        self.duty_btn.clicked.connect(self._calibrate_duty_all)
        self.deflate_btn.clicked.connect(self._calibrate_deflate_all)
        self.stop_btn.clicked.connect(self._stop)
        self.apply_btn.clicked.connect(self._on_apply)
        self.save_btn.clicked.connect(self._on_save)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._pressure.connect(self._on_pressure)
        if gateway is not None:
            gateway.on_message(self._on_gateway_message)
        self.finished.connect(lambda _=0: self._stop())

    def _rate_ms(self) -> int:
        data = self.detail_combo.currentData()
        return int(data) if data is not None else _DEFAULT_RATE_MS

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
        # Calibration result (full fill time + fill-order rank). Pre-filled from any
        # stored curve so the user sees what's already calibrated.
        result = QLabel("—")
        result.setFixedWidth(140)
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
                           "kpa": kpa, "cfg": ch, "timed_out": False}
        return w

    def _profile_for(self, key: tuple[str, int]) -> FillProfile | None:
        """The best-known fill curve for a row: a fresh measurement if any, else the
        chamber's own stored curve, else its skin-type template — used for the
        result text and the fill-order ranking."""
        fresh = self._results.get(key)
        if fresh is not None:
            return FillProfile.from_list(fresh)
        row = self._rows.get(key)
        if row is None:
            return None
        cfg = row["cfg"]
        own = FillProfile.from_list(cfg.get("fill_profile"))
        if own is not None:
            return own
        tmpl = get_type_profile(self._settings.data, cfg.get("skin_type"),
                                cfg.get("skin_variant"), key[1])
        return FillProfile.from_list(tmpl)

    def _refresh_ranks(self) -> None:
        """Recompute every row's result text + fill-order rank.

        Chambers are ranked by how long their solo curve takes to fill (shorter =
        fills first). The rank answers "which fills first / last" so a bulk fill can
        close valves in that order without waiting for the sensor."""
        profiles = {key: self._profile_for(key) for key in self._rows}
        timed = [(key, prof.full_time_ms) for key, prof in profiles.items()
                 if prof is not None]
        order = sorted(timed, key=lambda kv: kv[1])
        rank = {key: i + 1 for i, (key, _) in enumerate(order)}
        total = len(order)
        for key, row in self._rows.items():
            self._set_row_result(key, row, profiles[key], rank.get(key), total)

    def _duty_curve_for(self, key: tuple[str, int], row: dict) -> Any:
        """The best-known duty→speed sweep for a row: a fresh measurement if any,
        else the chamber's stored curve."""
        return self._duty_results.get(key) or row["cfg"].get("duty_curve")

    def _set_row_result(self, key: tuple[str, int], row: dict,
                        prof: FillProfile | None, rank: int | None, total: int) -> None:
        """Render one row's result label from its curve + fill-order rank."""
        label = row["result"]
        if prof is None:
            duty = self._duty_curve_for(key, row)
            label.setText("⚡ duty" if duty else "—")
            label.setToolTip(f"Duty curve: {len(duty)} points; fill curve not measured."
                             if duty else "Not calibrated yet.")
            return
        ms = int(round(prof.full_time_ms))
        tag = f" (#{rank}/{total})" if rank and total > 1 else ""
        if row.get("timed_out"):
            label.setText(f"≥{ms} ms{tag}")
            label.setToolTip(
                f"Timed out at {int(prof.top_pct)}% — chamber did not reach the "
                "target. Curve still saved up to that point.")
            return
        mark = " ✓" if key in self._results else ""
        rank_txt = f" Fills #{rank} of {total} (shorter = first)." if rank and total > 1 else ""
        marks, extra_txt = self._row_markers(key, row)
        label.setText(f"{ms} ms{mark}{marks}{tag}")
        label.setToolTip(f"Fills to {int(prof.top_pct)}% in {ms} ms.{rank_txt}{extra_txt}")

    def _row_markers(self, key: tuple[str, int], row: dict) -> tuple[str, str]:
        """Markers + tooltip suffix for the extra curves a chamber has: ⚡ = a
        measured duty→speed curve, ▼ = a measured deflate curve."""
        marks, txt = "", ""
        duty = self._duty_curve_for(key, row)
        if duty:
            marks += " ⚡"
            txt += f" Duty curve: {len(duty)} points."
        if self._deflate_results.get(key) or row["cfg"].get("deflate_profile"):
            marks += " ▼"
            txt += " Deflate curve measured."
        return marks, txt

    # ------------------------------------------------------------------
    # Calibration driving
    # ------------------------------------------------------------------

    def _calibrate_one(self, key: tuple[str, int]) -> None:
        """Per-row button: a solo (single-chamber) continuous sweep."""
        if self._job is not None:
            return                       # one at a time
        self._run_queue([{"mac": key[0], "slot": key[1], "idx": 1, "total": 1}])

    def _calibrate_all(self) -> None:
        self._run_specs([{"mac": ch["mac"], "slot": int(ch["slot"]), "kind": "fill"}
                         for ch in self._chambers])

    def _calibrate_duty_all(self) -> None:
        """Sweep every chamber's duty→speed curve (each at several PWM duties)."""
        self._run_specs([{"mac": ch["mac"], "slot": int(ch["slot"]), "kind": "duty"}
                         for ch in self._chambers])

    def _calibrate_deflate_all(self) -> None:
        """Sweep every chamber's deflate curve (fill → record the fall to the floor)."""
        self._run_specs([{"mac": ch["mac"], "slot": int(ch["slot"]), "kind": "deflate"}
                         for ch in self._chambers])

    def _run_specs(self, specs: list[dict]) -> None:
        if self._job is not None or not specs:
            return
        for i, sp in enumerate(specs, 1):
            sp["idx"], sp["total"] = i, len(specs)
        self._run_queue(specs)

    def _run_queue(self, specs: list[dict]) -> None:
        """Start the first spec, carrying the rest as the queue."""
        if not specs:
            self._set_buttons_enabled(True)
            self.combo_status.setText("")
            return
        spec = specs[0]
        self._start_job(spec["mac"], spec["slot"], kind=spec.get("kind", "fill"),
                        queue=specs[1:], combo_index=(spec["idx"], spec["total"]))

    def _start_job(self, mac: str, slot: int, *, kind: str, queue: list[dict],
                   combo_index: tuple[int, int]) -> None:
        if self._job is not None:
            return
        slot = int(slot)
        row = self._rows.get((mac, slot))
        if row is not None:
            row["bar"].setValue(0)
            row["timed_out"] = False
            row["result"].setText("…")
        self._set_buttons_enabled(False)
        self._job = {
            "mac": mac, "slot": slot, "kind": kind, "phase": "deflate",
            "phase_elapsed": 0,
            # Built by _begin_sweep / _begin_downsweep when the recording phase
            # starts; the vent/prefill phases never record.
            "cal": None,
            "ft": FastTelemetry(self._gateway, mac, rate_ms=self._rate_ms()),
            "last_pct": 100.0, "last_kpa": float("nan"),
            "plateau": self._new_vent_plateau(),
            "t0": None, "keepalive_acc": 0,
            # Duty jobs sweep the chamber once per duty in _DUTY_SWEEP, collecting
            # (duty, full_time_ms); a fill job is a single full-duty sweep.
            "duties": list(_DUTY_SWEEP) if kind == "duty" else [0],
            "duty_i": 0, "duty_samples": [],
            "queue": queue, "combo_index": combo_index,
        }
        self._update_combo_status(mac, slot, combo_index)
        # Start from a true ambient baseline: vent (valves open, no pump) and wait
        # until the reading settles at its floor.
        self._send_vent(mac, slot)
        self._tick.start()

    def _begin_sweep(self, job: dict) -> None:
        """Hold the inflate valve open and start streaming the fast pressure curve."""
        job["phase"] = "sweep"
        job["phase_elapsed"] = 0
        job["keepalive_acc"] = 0
        job["dir"] = 0
        job["t0"] = time.monotonic()
        job["cal"] = ContinuousFillCalibrator(target_pct=DEFAULT_TARGET_PCT,
                                              max_total_ms=DEFAULT_TIMEOUT_MS)
        job["ft"].start()
        self._send_test_run(job)

    def _send_test_run(self, job: dict) -> None:
        """(Re)assert the continuous valve+pump hold for this sweep's chamber/duty.

        test_run holds one chamber's valve + pump open continuously (bypassing
        the closed loop) so the curve is a clean ramp, not stepped. ``job["dir"]``
        picks the side (0 = inflate for fill/duty/prefill, 1 = deflate for the
        falling sweep); a duty job runs the pump at the current sweep's duty
        (0 = full). Re-sent periodically as the firmware dead-man keepalive."""
        duty = job["duties"][job["duty_i"]]
        payload = {"dir": job.get("dir", 0), "chamber": job["slot"]}
        if duty:
            payload["duty"] = duty
        self._gateway.send(job["mac"], "test_run", **payload)

    def _send_vent(self, mac: str, slot: int) -> None:
        """Passively vent a chamber to ambient: open BOTH valves with NO pump, so it
        equalises to atmosphere. The vacuum pump (``deflate``) would instead pull
        below ambient — which the gauge can't read — so it never settles at the true
        ambient baseline every sweep must start from. Re-sent as the manual dead-man
        keepalive; the same open-valves-no-pump the Test Actuators bench uses."""
        self._gateway.send(mac, "valve_manual", chamber=slot, side=0, open=1)
        self._gateway.send(mac, "valve_manual", chamber=slot, side=1, open=1)

    def _close_valves(self, mac: str, slot: int) -> None:
        """Close both of a chamber's manual valves (end a vent)."""
        self._gateway.send(mac, "valve_manual", chamber=slot, side=0, open=0)
        self._gateway.send(mac, "valve_manual", chamber=slot, side=1, open=0)

    @staticmethod
    def _new_vent_plateau() -> PlateauDetector:
        """Fresh floor detector for a vent-to-ambient phase (Qt-free core class)."""
        return PlateauDetector(min_drop=_DEFLATE_MIN_DROP_KPA,
                               settle_ms=_DEFLATE_SETTLE_MS,
                               min_ms=_MIN_DEFLATE_MS)

    def _deflate_settled(self, job: dict) -> bool:
        """True once venting has reached the chamber's ambient floor.

        Ambient may not read 0 kPa (this gauge rests ~6 kPa), so "empty" is where
        the reading stops dropping (the shared :class:`PlateauDetector`), never a
        fixed threshold. Falls back to a %-of-range threshold when no kPa is
        reported."""
        kpa = job["last_kpa"]
        if math.isnan(kpa):
            return job["last_pct"] <= _EMPTY_PCT
        return job["plateau"].update(job["phase_elapsed"], kpa)

    def _on_tick(self) -> None:
        job = self._job
        if job is None:
            return
        job["phase_elapsed"] += _TICK_MS
        phase = job["phase"]
        if phase == "deflate":
            self._tick_vent(job)
        elif phase == "prefill":
            self._tick_prefill(job)
        elif phase in ("sweep", "downsweep"):
            self._tick_sweep(job)

    def _keepalive_hold(self, job: dict) -> None:
        """Refresh the firmware dead-mans (test_run + status_rate) periodically."""
        job["keepalive_acc"] += _TICK_MS
        if job["keepalive_acc"] >= _KEEPALIVE_MS:
            job["keepalive_acc"] = 0
            job["ft"].keepalive()
            self._send_test_run(job)

    def _tick_vent(self, job: dict) -> None:
        # Keep the vent valves open (refresh the firmware manual dead-man).
        job["keepalive_acc"] += _TICK_MS
        if job["keepalive_acc"] >= _KEEPALIVE_MS:
            job["keepalive_acc"] = 0
            self._send_vent(job["mac"], job["slot"])
        if self._deflate_settled(job) or job["phase_elapsed"] >= _MAX_DEFLATE_MS:
            self._close_valves(job["mac"], job["slot"])
            if job["kind"] == "deflate":
                self._begin_prefill(job)
            else:
                self._begin_sweep(job)

    def _tick_prefill(self, job: dict) -> None:
        # Filling toward ~full before the falling sweep; keepalives as in a sweep.
        self._keepalive_hold(job)
        if job["phase_elapsed"] >= _MAX_PREFILL_MS:
            # Chamber never made ~full (leak/slow) — sweep down from wherever.
            self._begin_downsweep(job)

    def _tick_sweep(self, job: dict) -> None:
        self._keepalive_hold(job)
        # Safety: if the sweep runs well past the curve's own ceiling (e.g. the
        # fast telemetry never arrived), stop rather than hang.
        if (time.monotonic() - job["t0"]) * 1000 >= job["cal"].max_total_ms + _SWEEP_GRACE_MS:
            job["cal"].timed_out = True
            job["cal"].done = True
            self._on_sweep_done(job)

    def _begin_prefill(self, job: dict) -> None:
        """Fill toward ~full (no recording) so the falling sweep spans the range."""
        job["phase"] = "prefill"
        job["phase_elapsed"] = 0
        job["keepalive_acc"] = 0
        job["dir"] = 0                            # continuous inflate hold
        job["ft"].start()
        self._send_test_run(job)

    def _begin_downsweep(self, job: dict) -> None:
        """Hold the deflate valve open and record the falling pressure curve."""
        self._gateway.send(job["mac"], "test_stop")    # end the prefill hold
        job["phase"] = "downsweep"
        job["phase_elapsed"] = 0
        job["keepalive_acc"] = 0
        job["dir"] = 1                            # continuous deflate (vacuum) hold
        job["t0"] = time.monotonic()
        job["cal"] = ContinuousDeflateCalibrator(max_total_ms=DEFAULT_TIMEOUT_MS)
        self._send_test_run(job)

    def _on_pressure(self, mac: str, chamber: int, pct: float, kpa: float) -> None:
        # Keep every row's live kPa current, whichever chamber is being swept.
        row = self._rows.get((mac, chamber))
        if row is not None:
            row["kpa"].setText(f"{kpa:.2f} kPa" if not math.isnan(kpa) else f"{pct:.0f}%")
        job = self._job
        if job is None or mac != job["mac"] or chamber != job["slot"]:
            return
        job["last_pct"] = pct
        job["last_kpa"] = kpa
        self._feed_job_pressure(job, row, pct)

    def _feed_job_pressure(self, job: dict, row: dict | None, pct: float) -> None:
        """Advance the running job with one live reading of its chamber."""
        if job["phase"] in ("deflate", "prefill", "downsweep") and row is not None:
            row["bar"].setValue(int(max(0.0, min(100.0, pct))))
        if job["phase"] == "prefill" and pct >= _PREFILL_TARGET_PCT:
            self._begin_downsweep(job)
        elif job["phase"] == "downsweep":
            elapsed_ms = (time.monotonic() - job["t0"]) * 1000.0
            if job["cal"].record(elapsed_ms, pct):
                self._on_sweep_done(job)
        elif job["phase"] == "sweep":
            elapsed_ms = (time.monotonic() - job["t0"]) * 1000.0
            done = job["cal"].record(elapsed_ms, pct)
            if row is not None:
                row["bar"].setValue(int(job["cal"].top_pct))
            if done:
                self._on_sweep_done(job)

    def _on_sweep_done(self, job: dict) -> None:
        """One valve-open sweep finished (target/timeout/floor). Stop the hold +
        vent. A fill or deflate job finishes here; a duty job records this duty's
        fill time and moves to the next duty (re-emptying first)."""
        cal = job["cal"]
        mac, slot = job["mac"], job["slot"]
        self._gateway.send(mac, "test_stop")     # end the continuous hold
        job["ft"].stop()                         # revert telemetry cadence
        self._send_vent(mac, slot)               # passively vent back to ambient
        if job["kind"] == "duty":
            duty = job["duties"][job["duty_i"]]
            job["duty_samples"].append([int(duty), int(round(cal.profile.full_time_ms))])
            job["duty_i"] += 1
            if job["duty_i"] < len(job["duties"]):
                # Re-empty to the ambient floor, then sweep the next duty (the tick
                # keeps running). Fresh floor detector for the new descent.
                job["phase"] = "deflate"
                job["phase_elapsed"] = 0
                job["keepalive_acc"] = 0
                job["plateau"] = self._new_vent_plateau()
                self._update_combo_status(mac, slot, job["combo_index"])
                return
            self._duty_results[(mac, slot)] = job["duty_samples"]
        elif job["kind"] == "deflate":
            self._deflate_results[(mac, slot)] = cal.profile.to_list()
        else:
            self._results[(mac, slot)] = cal.profile.to_list()
            row = self._rows.get((mac, slot))
            if row is not None:
                row["timed_out"] = cal.timed_out
        self._refresh_ranks()
        self._finish_job()

    def _finish_job(self) -> None:
        """End the current job and start the next queued one, if any."""
        job = self._job
        if job is None:
            return
        self._job = None
        self._tick.stop()
        queue = job["queue"]
        if queue:
            QTimer.singleShot(400, lambda q=queue: self._run_queue(q))
        else:
            self.combo_status.setText("")
            self._set_buttons_enabled(True)

    def _update_combo_status(self, mac: str, slot: int,
                             combo_index: tuple[int, int]) -> None:
        idx, total = combo_index
        extra = ""
        job = self._job
        if job is not None and job.get("kind") == "duty":
            extra = f" · duty {job['duties'][job['duty_i']]}"
        elif job is not None and job.get("kind") == "deflate":
            extra = " · deflate curve"
        self.combo_status.setText(f"Run {idx}/{total} — {mac} slot {slot}{extra}")

    def _set_buttons_enabled(self, on: bool) -> None:
        # Buttons disabled == a sweep (or queued batch) is running: pulse the
        # rings while busy, fade them off when control returns to the operator.
        self._led.off() if on else self._led.on()
        self.all_btn.setEnabled(on and bool(self._chambers))
        self.duty_btn.setEnabled(on and bool(self._chambers))
        self.deflate_btn.setEnabled(on and bool(self._chambers))
        self.detail_combo.setEnabled(on)
        for r in self._rows.values():
            r["btn"].setEnabled(on)

    def _stop(self) -> None:
        """Abort any running calibration and halt everything touched.

        Sends ``test_stop`` + closes the vent valves + ``hold`` (pumps off, valves
        closed), NOT ``deflate``: the deflate pump is an active vacuum, so deflating
        on Stop would spin the vacuum pump rather than simply stopping. Stop halts,
        it does not actuate.
        """
        self._tick.stop()
        if self._job is not None:
            self._job["ft"].stop()
        self._job = None
        self.combo_status.setText("")
        # Best-effort: halt every chamber so nothing is left actuating or venting.
        if self._gateway is not None:
            for (mac, slot) in self._rows:
                self._gateway.send(mac, "test_stop")
                self._close_valves(mac, slot)
                self._gateway.send(mac, "hold", chamber=slot)
        self._set_buttons_enabled(True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _prefill_min_power(self) -> None:
        """Show the stored power-level-1 PWM floor for the skin type(s) here.

        Uses the first typed chamber's saved value; the .ui default (190) stands
        when nothing has a skin type or none is stored."""
        for ch in self._chambers:
            if type_slug(ch.get("skin_type", ""), ch.get("skin_variant", "")):
                self.min_power_spin.setValue(
                    get_type_min_duty(self._settings.data,
                                      ch.get("skin_type", ""),
                                      ch.get("skin_variant", "")))
                return

    def _save_min_power(self) -> int:
        """Persist the power-level-1 PWM floor for each distinct skin type shown,
        writing only the ones whose value actually changed. Returns how many were
        updated (so a value-only edit still counts as a save)."""
        val = int(self.min_power_spin.value())
        seen: set[str] = set()
        n = 0
        for ch in self._chambers:
            st, sv = ch.get("skin_type", ""), ch.get("skin_variant", "")
            slug = type_slug(st, sv)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            if get_type_min_duty(self._settings.data, st, sv) != val:
                set_type_min_duty(self._settings.data, st, sv, val)
                n += 1
        return n

    def _commit(self) -> bool:
        """Persist the measured fill curves (and the min-power floor) without
        closing. Returns True on success. Shared by Save (which then closes) and
        Apply (which stays open so the user can keep refining other chambers).

        With "Save as skin-type template" ticked, each curve is stored against its
        (skin_type, skin_variant, slot) so every skin of that type inherits it, and
        the per-chamber override is cleared so the template shows through. Chambers
        with no skin type fall back to a per-chamber save so nothing is lost."""
        n_min = self._save_min_power()
        if not (self._results or self._duty_results or self._deflate_results):
            if n_min:                       # a min-power-only edit still saves
                self._settings.save()
                self.saved.emit()
                self.combo_status.setText(
                    f"Saved min power for {n_min} skin type(s).")
                return True
            QMessageBox.information(self, "Save", "Nothing calibrated yet.")
            return False
        as_type = self.save_type_check.isChecked()
        n_type = self._save_curves(self._results, as_type,
                                   set_type_profile, set_fill_profile)
        n_type += self._save_curves(self._deflate_results, as_type,
                                    set_type_deflate_profile, set_deflate_profile)
        # Duty curves are a per-chamber pump property (not per skin type).
        for (mac, slot), curve in self._duty_results.items():
            set_duty_curve(self._settings.data, mac, slot, curve)
        self._settings.save()
        self.saved.emit()
        n_curves = len(self._results) + len(self._deflate_results)
        parts = []
        if n_type:
            parts.append(f"{n_type} type template(s)")
        if n_curves - n_type:
            parts.append(f"{n_curves - n_type} chamber override(s)")
        if self._duty_results:
            parts.append(f"{len(self._duty_results)} duty curve(s)")
        if n_min:
            parts.append(f"min power for {n_min} type(s)")
        self.combo_status.setText("Saved " + ", ".join(parts) + ".")
        return True

    def _save_curves(self, results: dict, as_type: bool,
                     set_type: Any, set_chamber: Any) -> int:
        """Persist one family of measured curves (fill or deflate): as its skin-type
        template (clearing the per-chamber override so the template shows through)
        when requested and the chamber has a typed skin, else onto the chamber.
        Returns how many went to a type template."""
        n_type = 0
        for (mac, slot), profile in results.items():
            cfg = self._rows.get((mac, slot), {}).get("cfg", {})
            st, sv = cfg.get("skin_type", ""), cfg.get("skin_variant", "")
            if as_type and type_slug(st, sv):
                set_type(self._settings.data, st, sv, slot, profile)
                set_chamber(self._settings.data, mac, slot, None)
                n_type += 1
            else:
                set_chamber(self._settings.data, mac, slot, profile)
        return n_type

    def _on_apply(self) -> None:
        self._commit()

    def _on_save(self) -> None:
        if self._commit():
            self.accept()

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

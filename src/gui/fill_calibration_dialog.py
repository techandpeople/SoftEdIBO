"""Fill-time calibration dialog (Tools → Calibrate Fill Times…).

Measures, per actuator chamber, how long it takes to inflate from empty to (near)
its maximum — using the pressure sensor as ground truth — and stores that as the
chamber's ``fill_time_ms`` in settings. At runtime the firmware can then inflate
by time instead of closing the loop on the laggy multiplexed pressure sensor (a
hard 5 s ceiling + the firmware ``HARD_MAX`` pressure cutoff stay as safety nets).

Flow per chamber (one at a time, driven by a timer + gateway status messages):
  1. **Deflate** to empty (settle until pressure is low, or a timeout).
  2. **Inflate** to max, timing until pressure reaches the target %.
  3. Record the elapsed time, deflate back, show the result.

Talks to the node directly through the gateway (same pattern as the Test
Actuators dialog). The measurement maths live in the Qt-free
:mod:`src.hardware.fill_calibration` so they're unit-tested.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.hardware.fill_calibration import (
    MAX_FILL_MS,
    FillTimeCalibrator,
    iter_actuator_chambers,
    set_fill_time,
)

# How empty the chamber must read before we start timing an inflation, and how
# long we'll wait for that before giving up on the deflate phase.
_EMPTY_PCT = 5.0
_MAX_DEFLATE_MS = 7000
_TICK_MS = 100


class FillCalibrationDialog(QDialog):
    """Calibrate per-chamber fill times against the pressure sensor."""

    # gateway read thread → GUI thread: (mac, chamber, pressure_pct)
    _pressure = Signal(str, int, float)
    # Emitted after fill times are written to settings, so the app can rebuild
    # robots to pick up the new ``fill_time_ms`` values.
    saved = Signal()

    def __init__(self, settings: Any, gateway: Any,
                 parent: QWidget | None = None,
                 chambers: list[dict] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calibrate Fill Times")
        self.resize(560, 420)
        self.setMaximumWidth(720)
        self._settings = settings
        self._gateway = gateway
        self._active = True
        # ``chambers`` lets a caller scope the dialog to a subset (e.g. one
        # skin's chambers from the skin config dialog). When omitted, calibrate
        # every actuator chamber across all configured robots.
        self._chambers = (chambers if chambers is not None
                          else iter_actuator_chambers(settings.data))
        # measured results: (mac, slot) → fill_time_ms
        self._results: dict[tuple[str, int], float] = {}
        # currently-running calibration, or None
        self._job: dict | None = None
        self._rows: dict[tuple[str, int], dict] = {}

        root = QVBoxLayout(self)
        intro = QLabel(
            "Inflates each chamber from empty and times it to its max, using the "
            "pressure sensor. The measured time is saved as the chamber's fill "
            "time (used instead of live pressure control). Keep hands clear.")
        intro.setWordWrap(True)   # wrap instead of forcing the dialog very wide
        root.addWidget(intro)

        if not self._chambers:
            root.addWidget(QLabel(
                "No actuator chambers configured. Add node_direct / "
                "node_multiplexed chambers first."))
            root.addStretch(1)
        else:
            # Rows live in a scroll area so many chambers don't overflow the
            # dialog — the buttons below stay pinned.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            inner = QWidget()
            rows_layout = QVBoxLayout(inner)
            rows_layout.setContentsMargins(0, 0, 0, 0)
            for ch in self._chambers:
                rows_layout.addWidget(self._build_row(ch))
            rows_layout.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, stretch=1)

        btns = QHBoxLayout()
        self._all_btn = QPushButton("Calibrate all")
        self._all_btn.setEnabled(bool(self._chambers) and gateway is not None)
        self._all_btn.clicked.connect(self._calibrate_all)
        stop = QPushButton("⏹ Stop")
        stop.clicked.connect(self._stop)
        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._save)
        btns.addWidget(self._all_btn)
        btns.addWidget(stop)
        btns.addStretch(1)
        btns.addWidget(self._save_btn)
        root.addLayout(btns)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._pressure.connect(self._on_pressure)
        if gateway is not None:
            gateway.on_message(self._on_gateway_message)
        self.finished.connect(lambda _=0: self._stop())

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_row(self, ch: dict) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        key = (ch["mac"], ch["slot"])
        name = QLabel(f"{ch['robot_id']}/{ch['skin_id']}  {ch['mac']} slot {ch['slot']}")
        name.setMinimumWidth(280)
        cur = ch["fill_time_ms"]
        result = QLabel(f"{cur} ms" if cur is not None else "—")
        # Fixed width (not just a minimum) so a wide value like the timeout text
        # never steals space from the progress bar — keeps every row aligned.
        result.setFixedWidth(110)
        result.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setMaximumHeight(10)
        btn = QPushButton("Calibrate")
        btn.setEnabled(self._gateway is not None)
        btn.clicked.connect(lambda _=False, k=key: self._calibrate_one(k))
        h.addWidget(name)
        h.addWidget(bar, stretch=1)
        h.addWidget(result)
        h.addWidget(btn)
        self._rows[key] = {"result": result, "bar": bar, "btn": btn, "cfg": ch}
        return w

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
            "cal": FillTimeCalibrator(), "elapsed": 0, "queue": queue,
        }
        # Start empty: deflate and wait until the chamber reads low.
        self._gateway.send(key[0], "deflate", chamber=key[1])
        self._tick.start()

    def _calibrate_all(self) -> None:
        if self._job is not None:
            return
        queue = list(self._rows.keys())
        first = queue.pop(0)
        self._calibrate_one(first, queue=queue)

    def _on_tick(self) -> None:
        job = self._job
        if job is None:
            return
        job["elapsed"] += _TICK_MS
        if job["phase"] == "deflate":
            # Give up waiting for "empty" after a bounded time and inflate anyway.
            if job["elapsed"] >= _MAX_DEFLATE_MS:
                self._begin_inflate(job)
        elif job["phase"] == "inflate":
            if job["cal"].tick() is not None:        # 5 s ceiling hit
                self._finish_job(timed_out=True)

    def _begin_inflate(self, job: dict) -> None:
        job["phase"] = "inflate"
        job["cal"].start()
        self._gateway.send(job["mac"], "inflate", chamber=job["slot"], value=255)

    def _on_pressure(self, mac: str, chamber: int, pct: float) -> None:
        job = self._job
        if job is None or mac != job["mac"] or chamber != job["slot"]:
            return
        row = self._rows[job["key"]]
        row["bar"].setValue(int(max(0.0, min(100.0, pct))))
        if job["phase"] == "deflate":
            if pct <= _EMPTY_PCT:
                self._begin_inflate(job)
        elif job["phase"] == "inflate":
            if job["cal"].update(pct) is not None:
                self._finish_job(timed_out=job["cal"].timed_out)

    def _finish_job(self, *, timed_out: bool) -> None:
        job = self._job
        if job is None:
            return
        self._job = None
        self._tick.stop()
        cal = job["cal"]
        key = job["key"]
        row = self._rows[key]
        # Deflate back to a safe resting state.
        self._gateway.send(job["mac"], "deflate", chamber=job["slot"])
        if cal.result_ms is not None and not timed_out:
            self._results[key] = cal.result_ms
            row["result"].setText(f"{int(round(cal.result_ms))} ms")
        else:
            row["result"].setText(f"≥{int(MAX_FILL_MS)} ms")
            row["result"].setToolTip("Timed out — chamber did not reach target.")
        queue = job["queue"]
        if queue:
            nxt = queue.pop(0)
            QTimer.singleShot(300, lambda k=nxt, q=queue:
                              self._calibrate_one(k, queue=q))
        else:
            self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, on: bool) -> None:
        self._all_btn.setEnabled(on and bool(self._chambers))
        for r in self._rows.values():
            r["btn"].setEnabled(on)

    def _stop(self) -> None:
        """Abort any running calibration and deflate everything touched."""
        self._tick.stop()
        macs = {k[0] for k in self._rows}
        if self._job is not None:
            job, self._job = self._job, None
            self._gateway.send(job["mac"], "deflate", chamber=job["slot"])
        # Best-effort: deflate all chambers so nothing is left inflated.
        if self._gateway is not None:
            for (mac, slot) in self._rows:
                self._gateway.send(mac, "deflate", chamber=slot)
            _ = macs
        self._set_buttons_enabled(True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Save", "Nothing calibrated yet.")
            return
        for (mac, slot), ms in self._results.items():
            set_fill_time(self._settings.data, mac, slot, ms)
        self._settings.save()
        self.saved.emit()
        QMessageBox.information(
            self, "Save", f"Saved fill times for {len(self._results)} chamber(s).")

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
        if isinstance(mac, str) and isinstance(chamber, int) \
                and isinstance(pressure, (int, float)):
            self._pressure.emit(mac, chamber, float(pressure))

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._active = False
        self._stop()
        super().closeEvent(ev)

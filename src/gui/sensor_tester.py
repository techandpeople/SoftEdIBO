"""Magnet sensor tester - a live per-sensor uT readout for the Test Actuators dialog.

Shows one bar per magnet/touch sensor (S0..S{n-1}) driven by the node's ``magnet``
stream: each bar is that sensor's baseline-subtracted field magnitude in uT (the
firmware ``mag`` field), so pressing the skin above a sensor makes its bar rise.
A sensor lights green once its reading passes the "Active threshold", giving a
visible, adjustable sensitivity - the whole point of the panel is that you can
watch a real press (~100 uT) cross the line, instead of tuning blind.

The widget is hardware-agnostic: it renders values and reports intents via two
callbacks the host (Test Actuators dialog) wires to the gateway:

  * ``rezero_cb()``            - user asked to re-zero the node's baseline.
  * ``configure_cb(threshold)`` - user asked to push this uT threshold to the
    node so the board's own touch detection fires at the same level.

Layout lives in ``src/gui/ui/sensor_tester.ui`` (edit in Qt Designer + recompile
with ``scripts/compile_ui.sh``); the per-sensor rows are built here because the
sensor count is only known at runtime, from the first ``magnet`` frame.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QProgressBar,
                               QWidget)

from src.gui.ui_sensor_tester import Ui_SensorTester

# Bar scale: full-scale is a few times the threshold so a resting sensor sits
# near the left and a firm press fills a good part of the bar without clipping.
_BAR_HEADROOM = 3.0
_BAR_MIN_FULLSCALE = 200      # uT floor so a low threshold still leaves a usable bar

_ACTIVE_CHUNK = "QProgressBar::chunk { background-color: #2ecc71; }"   # touched
_IDLE_CHUNK   = "QProgressBar::chunk { background-color: #3498db; }"   # resting

# Re-zero completion detection: the node stops streaming ``magnet`` frames while
# it rebuilds its baseline (70 samples), then resumes at ~0 uT. So a gap in the
# stream marks the re-zero in progress, and the next frame after it = done.
_REZERO_SILENCE_MS = 500      # a stream gap this long means the node went quiet
_REZERO_MIN_MS     = 1500     # ignore earlier gaps (transient ESP-NOW drops)
_REZERO_TIMEOUT_MS = 12000    # give up waiting for the stream to resume


class SensorTester(QGroupBox, Ui_SensorTester):
    """Live uT readout + adjustable sensitivity for a node's magnet sensors."""

    # Emitted when the user flips the "Compensate actuation coupling" checkbox.
    # The host owns the compensator + chamber levels, so it re-feeds the last
    # reading (raw or compensated) in response - this widget only renders values.
    compensation_toggled = Signal(bool)

    def __init__(
        self,
        count: int,
        rezero_cb: Callable[[], None],
        configure_cb: Callable[[float], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._rezero = rezero_cb
        self._configure = configure_cb
        self._count = max(1, int(count))
        # Whether the host has a coupling to apply. Tracked as an explicit flag,
        # NOT via the checkbox's isVisible(): visibility is False until the whole
        # widget tree is realised on screen, which would wrongly gate the logic.
        self._compensation_available = False

        self._bars: list[QProgressBar] = []
        self._value_labels: list[QLabel] = []
        self._peaks: list[float] = [0.0] * self._count
        self._last_mag: list[float] = [0.0] * self._count
        self._active_shown: list[bool] = [False] * self._count

        # Re-zero-in-progress tracking (see the module-level constants).
        self._awaiting_rezero = False
        self._rezero_click_ms = 0.0
        self._last_frame_ms = 0.0
        self._rezero_timeout = QTimer(self)
        self._rezero_timeout.setSingleShot(True)
        self._rezero_timeout.setInterval(_REZERO_TIMEOUT_MS)
        self._rezero_timeout.timeout.connect(self._finish_rezero)

        for i in range(self._count):
            row = QHBoxLayout()
            name = QLabel(f"S{i}")
            name.setMinimumWidth(28)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setStyleSheet(_IDLE_CHUNK)
            value = QLabel("-")
            value.setMinimumWidth(150)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(name)
            row.addWidget(bar, 1)
            row.addWidget(value)
            self.sensors_vbox.addLayout(row)
            self._bars.append(bar)
            self._value_labels.append(value)

        self._apply_bar_scale()
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)
        self.rezero_btn.clicked.connect(self._on_rezero)
        self.push_btn.clicked.connect(self._on_push)
        self.compensate_cb.toggled.connect(self.compensation_toggled)

    # ------------------------------------------------------------------
    # Actuation-coupling compensation toggle
    # ------------------------------------------------------------------

    def set_compensation_available(self, available: bool) -> None:
        """Show the raw<->compensated toggle only when the host has a calibrated,
        enabled coupling to apply. Hidden (the default) means the panel just
        renders the raw stream, so nothing changes for uncalibrated skins."""
        self._compensation_available = bool(available)
        self.compensate_cb.setVisible(self._compensation_available)

    def compensation_enabled(self) -> bool:
        """True when the host should feed compensated values (toggle available
        and checked). Gated on the availability flag, not the checkbox's
        visibility, which is only realised once the panel is shown."""
        return self._compensation_available and self.compensate_cb.isChecked()

    # ------------------------------------------------------------------
    # Public API (called by the host on every ``magnet`` frame)
    # ------------------------------------------------------------------

    def update_values(self, mag: Sequence[float]) -> None:
        """Render the latest per-sensor magnitudes (uT).

        ``mag`` is the firmware ``mag`` array (baseline-subtracted, so ~0 at rest).
        The active highlight is computed here against the local threshold so it
        tracks the spin box live - no round-trip to the node needed to *see* the
        effect of changing sensitivity."""
        now = time.monotonic() * 1000.0
        gap = now - self._last_frame_ms
        self._last_frame_ms = now
        # A frame arriving after the stream fell silent is the node resuming with
        # its fresh baseline: the re-zero is done. Gate on a minimum elapsed time
        # so a transient early drop can't end the wait before the real gap.
        if (self._awaiting_rezero and gap > _REZERO_SILENCE_MS
                and now - self._rezero_click_ms > _REZERO_MIN_MS):
            self._finish_rezero()

        self._last_mag = [float(v) for v in mag[: self._count]]
        while len(self._last_mag) < self._count:
            self._last_mag.append(0.0)
        for i, value in enumerate(self._last_mag):
            self._peaks[i] = max(self._peaks[i], value)
        self._render()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _threshold(self) -> float:
        return float(self.threshold_spin.value())

    def _fullscale(self) -> int:
        return max(_BAR_MIN_FULLSCALE, int(self._threshold() * _BAR_HEADROOM))

    def _apply_bar_scale(self) -> None:
        fs = self._fullscale()
        for bar in self._bars:
            bar.setRange(0, fs)

    def _render(self) -> None:
        threshold = self._threshold()
        active_names: list[str] = []
        for i in range(self._count):
            mag = self._last_mag[i]
            self._bars[i].setValue(int(min(mag, self._fullscale())))
            is_active = mag >= threshold
            if is_active:
                active_names.append(f"S{i}")
            if is_active != self._active_shown[i]:
                self._bars[i].setStyleSheet(_ACTIVE_CHUNK if is_active else _IDLE_CHUNK)
                self._active_shown[i] = is_active
            dot = " *" if is_active else ""
            self._value_labels[i].setText(
                f"{mag:5.0f} uT | pk {self._peaks[i]:.0f}{dot}")
        self.status_label.setText(
            "Active: " + (", ".join(active_names) if active_names else "none"))

    def _on_threshold_changed(self) -> None:
        # Rescale the bars and re-evaluate the active highlight against the new
        # threshold using the last values, so dragging the spin shows its effect
        # immediately (even between magnet frames).
        self._apply_bar_scale()
        self._render()

    def _on_rezero(self) -> None:
        # After a re-zero the field reads ~0 again, so the local peaks no longer
        # reflect anything meaningful - clear them too.
        self._peaks = [0.0] * self._count
        self._rezero()
        self._begin_rezero()
        self._render()

    def _begin_rezero(self) -> None:
        """Grey the button out to ``Re-zeroing...`` until the node's stream resumes
        (or a fallback timeout fires), so the label reflects the real ~2.5-7 s the
        node spends rebuilding its baseline rather than pretending it is instant."""
        self._awaiting_rezero = True
        now = time.monotonic() * 1000.0
        self._rezero_click_ms = now
        self._last_frame_ms = now
        self.rezero_btn.setEnabled(False)
        self.rezero_btn.setText("Re-zeroing...")
        self._rezero_timeout.start()

    def _finish_rezero(self) -> None:
        if not self._awaiting_rezero:
            return
        self._awaiting_rezero = False
        self._rezero_timeout.stop()
        self.rezero_btn.setText("Re-zero")
        self.rezero_btn.setEnabled(True)

    def _on_push(self) -> None:
        self._configure(self._threshold())

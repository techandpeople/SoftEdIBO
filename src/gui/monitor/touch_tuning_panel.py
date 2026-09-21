"""TouchTuningPanel - live tuning for a skin's quadrant touch detection.

Shown under the SkinGridView when a skin has 4-sensor touch tracking. Lets the
operator adjust the per-quadrant detection thresholds + hysteresis while the
activity runs (applied immediately to the skin's QuadrantDetector), re-zero the
magnetic sensors on the node over ESP-NOW, and toggle the node's adaptive
baseline.

The layout lives in ``src/gui/ui_touch_tuning_panel.ui`` (edit it in Qt Designer
and recompile with ``scripts/compile_ui.sh``). This module keeps only the wiring
+ behaviour. Threshold changes are saved to the user's SoftEdIBO settings and
restored for the same touch node on the next session.
"""

from __future__ import annotations

import time
from collections import deque
from math import ceil

from PySide6.QtCore import Qt, QTimer, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QGridLayout, QGroupBox,
                               QLabel, QPushButton, QProgressBar, QSizePolicy,
                               QWidget)

from src.gui.ui_touch_tuning_panel import Ui_TouchTuningPanel
from src.hardware.skin import Skin


class MagnitudePlot(QWidget):
    """Rolling magnitude plot fed by the live sensor window."""

    _colors = (QColor("#e74c3c"), QColor("#2ecc71"),
               QColor("#3498db"), QColor("#f1c40f"))

    def __init__(self, sensor_count: int = 4,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = [deque(maxlen=240) for _ in range(sensor_count)]
        self._thresholds = [100.0] * sensor_count
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def add_sample(self, magnitudes: list[float], thresholds: list[float]) -> None:
        self._thresholds = list(thresholds)
        for index, history in enumerate(self._history):
            value = magnitudes[index] if index < len(magnitudes) else 0.0
            history.append(max(0.0, float(value)))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#202124"))
        painter.setPen(QColor("#e8eaed"))
        painter.drawText(12, 16, "Live magnitude (uT)")
        painter.setPen(QColor("#bdc1c6"))
        painter.drawText(self.width() - 105, 16, "recent samples")

        gap = 8
        cell_width = (self.width() - gap * 3) // 2
        cell_height = (self.height() - 32 - gap * 3) // 2
        if cell_width <= 0 or cell_height <= 0:
            return

        for index, history in enumerate(self._history):
            column = index % 2
            row = index // 2
            cell = QRect(column * (cell_width + gap) + gap,
                         row * (cell_height + gap) + 24,
                         cell_width, cell_height)
            plot = cell.adjusted(38, 22, -8, -22)
            maximum = max(list(history) + [self._thresholds[index], 100.0])
            y_max = max(100.0, ceil(maximum / 100.0) * 100.0)

            painter.setPen(QPen(QColor("#5f6368"), 1))
            painter.drawRect(cell)
            painter.setPen(QColor("#e8eaed"))
            painter.drawText(cell.left() + 8, cell.top() + 15,
                             f"Q{index + 1}")
            painter.setPen(QColor("#bdc1c6"))
            painter.drawText(5, plot.top() + 5, f"{y_max:.0f}")
            painter.drawText(18, plot.bottom() + 16, "0")

            painter.setPen(QPen(QColor("#45484d"), 1))
            painter.drawLine(plot.topLeft(), plot.topRight())
            painter.drawLine(plot.bottomLeft(), plot.bottomRight())
            painter.drawLine(plot.topLeft(), plot.bottomLeft())
            threshold_y = plot.bottom() - (
                float(self._thresholds[index]) / y_max * plot.height())
            painter.setPen(QPen(self._colors[index % len(self._colors)], 1,
                                Qt.PenStyle.DashLine))
            painter.drawLine(plot.left(), int(threshold_y),
                             plot.right(), int(threshold_y))

            if len(history) < 2:
                continue
            points = []
            for point, value in enumerate(history):
                x = plot.left() + point * plot.width() / (len(history) - 1)
                y = plot.bottom() - (value / y_max) * plot.height()
                points.append((int(x), int(y)))
            painter.setPen(QPen(self._colors[index % len(self._colors)], 2))
            for start, end in zip(points, points[1:]):
                painter.drawLine(*start, *end)


class LiveSensorWindow(QDialog):
    """Live per-sensor readout for a skin's magnet stream."""

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Live touch sensors - {skin.skin_id}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._skin = skin
        self._value_labels: list[QLabel] = []
        self._state_labels: list[QLabel] = []
        self._bars: list[QProgressBar] = []
        self._frequency_labels: list[QLabel] = []
        self._synchrony_label = QLabel("Current synchrony: -- ms")
        self._cpr_conditions = QLabel()
        self._cpr_conditions.setWordWrap(True)
        self._cpr_conditions.setMinimumWidth(180)
        self._cpr_conditions.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._magnitude_plot = MagnitudePlot(parent=self)

        layout = QGridLayout(self)
        layout.addWidget(self._synchrony_label, 0, 5, 1, 2)
        # The free area below "Current synchrony" is the live checklist for
        # the CPR activity. It stays hidden in ordinary touch-monitor use.
        layout.addWidget(self._cpr_conditions, 1, 5, 4, 2)
        layout.addWidget(QLabel("Sensor"), 0, 0)
        layout.addWidget(QLabel("Magnitude"), 0, 1)
        layout.addWidget(QLabel("Level"), 0, 2)
        layout.addWidget(QLabel("State"), 0, 3)
        layout.addWidget(QLabel("Frequency"), 0, 4)
        for index in range(4):
            layout.addWidget(QLabel(f"Q{index + 1}"), index + 1, 0)
            value = QLabel("-- uT")
            layout.addWidget(value, index + 1, 1)
            bar = QProgressBar()
            bar.setRange(0, 2000)
            bar.setTextVisible(False)
            layout.addWidget(bar, index + 1, 2)
            state = QLabel("inactive")
            layout.addWidget(state, index + 1, 3)
            frequency = QLabel("-- Hz")
            layout.addWidget(frequency, index + 1, 4)
            self._value_labels.append(value)
            self._bars.append(bar)
            self._state_labels.append(state)
            self._frequency_labels.append(frequency)
        layout.addWidget(self._magnitude_plot, 5, 0, 1, 7)
        self.resize(720, 460)
        self._update_cpr_conditions()

    def _update_cpr_conditions(self) -> None:
        """Show exactly what is still needed for the CPR LED to go green."""
        status = getattr(self._skin, "cpr_sync_status", None)
        if not isinstance(status, dict) or not status.get("active"):
            self._cpr_conditions.hide()
            return
        self._cpr_conditions.show()
        rounds = int(status.get("rounds", 0))
        needed = int(status.get("rounds_required", 0))
        if status.get("complete"):
            self._cpr_conditions.setStyleSheet(
                "color: #16803c; font-weight: bold;")
            self._cpr_conditions.setText(
                f"CPR LED: GREEN\n"
                f"✓ {rounds}/{needed} synchronized rounds\n"
                "✓ All conditions met")
            return

        target = float(status.get("target_interval_ms", 0))
        cadence = float(status.get("cadence_tolerance_ms", 0))
        phase = float(status.get("phase_tolerance_ms", 0))
        sensors = status.get("sensors", [])
        sensor_text = ", ".join(f"T{value}" for value in sensors)
        self._cpr_conditions.setStyleSheet("color: #6b4b00;")
        self._cpr_conditions.setText(
            "CPR to green LED:\n"
            f"• Rounds: {rounds}/{needed}\n"
            f"• {status.get('reason', 'Keep compressing')}\n"
            f"• Together: {phase:.0f} ms max ({sensor_text})\n"
            f"• Rhythm: {target:.0f} ± {cadence:.0f} ms")

    def update_data(self, data: dict) -> None:
        self._update_cpr_conditions()
        magnitudes = data.get("mag")
        active = {int(value) for value in (data.get("act") or [])
                  if str(value).lstrip("-").isdigit()}
        thresholds = self._skin.touch_thresholds or [100.0] * 4
        frequencies = getattr(self.parent(), "_frequency_hz", {})
        press_times = [getattr(self.parent(), "_last_press_ms", {}).get(index)
                       for index in active]
        press_times = [value for value in press_times if value is not None]
        if len(press_times) >= 2:
            synchrony_ms = max(press_times) - min(press_times)
            self._synchrony_label.setText(
                f"Current synchrony: {synchrony_ms:.0f} ms")
        else:
            self._synchrony_label.setText("Current synchrony: -- ms")
        if not isinstance(magnitudes, (list, tuple)):
            return
        values = [float(value) for value in magnitudes]
        self._magnitude_plot.add_sample(values, [
            float(thresholds[index]) if index < len(thresholds) else 100.0
            for index in range(4)
        ])
        for index in range(4):
            magnitude = values[index] if index < len(values) else 0.0
            threshold = float(thresholds[index]) if index < len(thresholds) else 100.0
            self._value_labels[index].setText(f"{magnitude:.1f} uT")
            self._bars[index].setRange(0, max(200, int(threshold * 3)))
            self._bars[index].setValue(min(int(magnitude), self._bars[index].maximum()))
            is_active = index in active
            self._state_labels[index].setText("ACTIVE" if is_active else "inactive")
            frequency = frequencies.get(index)
            self._frequency_labels[index].setText(
                f"{frequency:.2f} Hz" if frequency is not None else "-- Hz")


class TouchTuningPanel(QGroupBox, Ui_TouchTuningPanel):
    """Per-quadrant threshold/hysteresis tuning + sensor re-zero for a skin."""

    _live_data = Signal(object)

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._skin = skin
        self._frequency_hz: dict[int, float] = {}
        self._last_press_ms: dict[int, float] = {}
        self._active_sensors: set[int] = set()
        self._frequency_reset_ms = self._saved_frequency_reset_ms()
        self._live_window: LiveSensorWindow | None = None

        # Compact styling so the panel stays small under the skin grid view.
        # Applied in code (not the .ui) because it is relative to the inherited
        # point size; the groupbox title keeps the normal size.
        compact = self.font()
        compact.setPointSizeF(max(7.0, compact.pointSizeF() - 1.0))
        for child in self.findChildren(QWidget):
            child.setFont(compact)

        # Quadrant thresholds (index 0..3 = Q1..Q4), seeded from the skin.
        self._threshold_spins = [self.thr0, self.thr1, self.thr2, self.thr3]
        thresholds = skin.touch_thresholds or [100.0, 100.0, 100.0, 100.0]
        for i, spin in enumerate(self._threshold_spins):
            spin.setValue(float(thresholds[i]) if i < len(thresholds) else 100.0)
            spin.valueChanged.connect(self._apply_thresholds)

        hysteresis = skin.touch_hysteresis if skin.touch_hysteresis is not None else 20.0
        self.hyst_spin.setValue(float(hysteresis))
        self.hyst_spin.valueChanged.connect(self._apply_hysteresis)

        self.spike_spin.setValue(self._saved_spike_threshold())
        self.spike_spin.valueChanged.connect(self._apply_spike_threshold)
        self.frequency_reset_spin.setValue(self._frequency_reset_ms)
        self.frequency_reset_spin.valueChanged.connect(self._apply_frequency_reset)
        self.sync_tolerance_spin.setValue(self._saved_sync_tolerance_ms())
        self.sync_tolerance_spin.valueChanged.connect(self._apply_sync_tolerance)

        self.apply_btn.clicked.connect(self._apply_node_config)
        self.rebaseline_btn.clicked.connect(self._rebaseline)
        self.adaptive_chk.toggled.connect(self._apply_adaptive_baseline)
        self.tau_spin.valueChanged.connect(self._apply_adaptive_baseline)

        self.live_btn = QPushButton("Live sensor data", self)
        self.live_btn.setToolTip("Open a live readout of the four touch sensors")
        self.bottom_row.addWidget(self.live_btn)
        self.live_btn.clicked.connect(self._show_live_window)
        self._live_data.connect(self._update_live_data,
                                Qt.ConnectionType.QueuedConnection)
        skin.on_magnet(lambda data: self._live_data.emit(data))

    # ------------------------------------------------------------------

    def _show_live_window(self) -> None:
        if self._live_window is None:
            self._live_window = LiveSensorWindow(self._skin, self)
        self._live_window.show()
        self._live_window.raise_()
        self._live_window.activateWindow()

    def _update_live_data(self, data: dict) -> None:
        active = data.get("act") or []
        if not isinstance(active, list):
            return
        timestamp = time.monotonic() * 1000.0
        current = {int(value) for value in active
                   if str(value).lstrip("-").isdigit()}
        for index in current - self._active_sensors:
            previous = self._last_press_ms.get(index)
            if previous is not None and timestamp > previous:
                self._frequency_hz[index] = 1000.0 / (timestamp - previous)
            self._last_press_ms[index] = timestamp
        self._active_sensors = current
        stale_ms = self._frequency_reset_ms
        self._frequency_hz = {
            index: frequency for index, frequency in self._frequency_hz.items()
            if timestamp - self._last_press_ms.get(index, 0.0) < stale_ms
        }
        if self._live_window is not None:
            self._live_window.update_data(data)

    def _apply_thresholds(self) -> None:
        thresholds = [s.value() for s in self._threshold_spins]
        self._skin.set_touch_thresholds(thresholds)
        source = getattr(self._skin, "touch_source", None)
        if source is not None and hasattr(source, "set_thresholds_ut"):
            source.set_thresholds_ut(thresholds)
        from src.config.settings import Settings
        touch = getattr(self._skin, "touch", None) or {}
        key = str(touch.get("node_mac") or getattr(self._skin, "skin_type", ""))
        Settings().set_touch_quadrant_thresholds(key, thresholds)

    def _apply_hysteresis(self) -> None:
        self._skin.set_touch_hysteresis(self.hyst_spin.value())

    def _touch_settings_key(self) -> str:
        touch = getattr(self._skin, "touch", None) or {}
        return str(touch.get("node_mac") or getattr(self._skin, "skin_type", ""))

    def _saved_spike_threshold(self) -> float:
        from src.config.settings import Settings
        saved = Settings().touch_spike_threshold(self._touch_settings_key())
        if saved is not None:
            return saved
        thresholds = self._skin.touch_thresholds or [100.0]
        return max(20.0, float(thresholds[0]) * 0.25)

    def _apply_spike_threshold(self) -> None:
        value = self.spike_spin.value()
        touch = getattr(self._skin, "touch", None)
        if isinstance(touch, dict):
            touch["rhythm_spike_ut"] = value
        from src.config.settings import Settings
        Settings().set_touch_spike_threshold(self._touch_settings_key(), value)

    def _saved_frequency_reset_ms(self) -> float:
        from src.config.settings import Settings
        saved = Settings().touch_frequency_reset_ms(self._touch_settings_key())
        return 10000.0 if saved is None else saved

    def _apply_frequency_reset(self) -> None:
        value = self.frequency_reset_spin.value()
        self._frequency_reset_ms = value
        touch = getattr(self._skin, "touch", None)
        if isinstance(touch, dict):
            touch["frequency_reset_ms"] = value
        from src.config.settings import Settings
        Settings().set_touch_frequency_reset_ms(self._touch_settings_key(), value)

    def _saved_sync_tolerance_ms(self) -> float:
        from src.config.settings import Settings
        saved = Settings().touch_sync_tolerance_ms(self._touch_settings_key())
        return 150.0 if saved is None else saved

    def _apply_sync_tolerance(self) -> None:
        value = self.sync_tolerance_spin.value()
        touch = getattr(self._skin, "touch", None)
        if isinstance(touch, dict):
            touch["rhythm_sync_tolerance_ms"] = value
        from src.config.settings import Settings
        Settings().set_touch_sync_tolerance_ms(self._touch_settings_key(), value)

    def _rebaseline(self) -> None:
        sent = self._skin.rebaseline_touch()
        # Brief visual confirmation on the button.
        self.rebaseline_btn.setText("Re-zeroed" if sent else "Re-zeroed (local)")
        self.rebaseline_btn.setEnabled(False)
        QTimer.singleShot(900, self._restore_button)

    def _restore_button(self) -> None:
        self.rebaseline_btn.setText("Re-zero sensors")
        self.rebaseline_btn.setEnabled(True)

    def _apply_node_config(self) -> None:
        """Apply the saved uT activation threshold to the PC source."""
        from src.config.settings import Settings
        saved = Settings().touch_threshold_ut(
            getattr(self._skin, "skin_type", "") or "")
        source = getattr(self._skin, "touch_source", None)
        if source is not None and hasattr(source, "set_threshold_ut"):
            source.set_threshold_ut(saved or 300.0)

    def _apply_adaptive_baseline(self) -> None:
        """Toggle the node's adaptive baseline (and its time constant)."""
        ctrl = getattr(self._skin, "touch_controller", None)
        if ctrl is None or not hasattr(ctrl, "send_command"):
            return
        ctrl.send_command("configure",
                          adaptive_baseline=self.adaptive_chk.isChecked(),
                          baseline_tau_ms=self.tau_spin.value())

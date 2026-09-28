"""LiveSensorWindow - live per-sensor readout of a skin's touch stream.

The layout lives in ``src/gui/ui/live_sensor_window.ui``; one row per sensor
is added to its ``sensor_grid`` from the first frame, so any sensor count
works. Rates come from the skin's shared :class:`PressRateMeter`.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QWidget

from src.gui.ui_live_sensor_window import Ui_LiveSensorWindow
from src.hardware.skin import Skin

_DEFAULT_THRESHOLD_UT = 100.0


class _SensorRow:
    """The widgets of one sensor's row."""

    def __init__(self, window: "LiveSensorWindow", index: int) -> None:
        self.value = QLabel("-- uT", window)
        self.bar = QProgressBar(window)
        self.bar.setTextVisible(False)
        self.state = QLabel("inactive", window)
        self.frequency = QLabel("-- Hz", window)
        row = index + 1                      # row 0 holds the headers
        grid = window.sensor_grid
        grid.addWidget(QLabel(f"T{index}", window), row, 0)
        grid.addWidget(self.value, row, 1)
        grid.addWidget(self.bar, row, 2)
        grid.addWidget(self.state, row, 3)
        grid.addWidget(self.frequency, row, 4)

    def show(self, magnitude: float, threshold: float, active: bool,
             frequency_hz: float | None) -> None:
        self.value.setText(f"{magnitude:.1f} uT")
        self.bar.setRange(0, max(200, int(threshold * 3)))
        self.bar.setValue(min(int(magnitude), self.bar.maximum()))
        self.state.setText("ACTIVE" if active else "inactive")
        self.frequency.setText(
            f"{frequency_hz:.2f} Hz" if frequency_hz is not None else "-- Hz")


class LiveSensorWindow(QDialog, Ui_LiveSensorWindow):
    """Live per-sensor readout for a skin's magnet stream."""

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self.setWindowTitle(f"Live touch sensors - {skin.skin_id}")
        self._skin = skin
        self._rows: list[_SensorRow] = []

    def update_data(self, data: dict) -> None:
        """Show one magnet frame (called on the GUI thread)."""
        magnitudes = data.get("mag")
        if not isinstance(magnitudes, (list, tuple)):
            return
        values = [float(value) for value in magnitudes]
        active = {int(value) for value in (data.get("act") or [])
                  if str(value).lstrip("-").isdigit()}
        while len(self._rows) < len(values):
            self._rows.append(_SensorRow(self, len(self._rows)))
        thresholds = [self._threshold(i) for i in range(len(values))]
        frequencies = self._skin.press_rate.frequencies_hz()
        self._show_synchrony(active)
        self.magnitude_plot.add_sample(values, thresholds)
        for index, row in enumerate(self._rows[:len(values)]):
            row.show(values[index], thresholds[index], index in active,
                     frequencies.get(index))

    def _threshold(self, index: int) -> float:
        thresholds = self._skin.touch_thresholds or []
        if not thresholds:
            return _DEFAULT_THRESHOLD_UT
        return float(thresholds[index] if index < len(thresholds) else thresholds[-1])

    def _show_synchrony(self, active: set[int]) -> None:
        last_press = self._skin.press_rate.last_press_ms()
        times = [last_press[idx] for idx in active if idx in last_press]
        self.synchrony_label.setText(
            f"Current synchrony: {max(times) - min(times):.0f} ms"
            if len(times) >= 2 else "Current synchrony: -- ms")

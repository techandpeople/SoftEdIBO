"""TouchTuningPanel - live tuning for a skin's quadrant touch detection.

Shown under the SkinGridView when a skin has 4-sensor touch tracking. Lets the
operator adjust the per-quadrant detection thresholds + hysteresis while the
activity runs (applied immediately to the skin's QuadrantDetector), re-zero the
magnetic sensors on the node over ESP-NOW, toggle the node's adaptive baseline
and open the live per-sensor readout.

The layout lives in ``src/gui/ui/touch_tuning_panel.ui`` (edit it in Qt Designer
and recompile with ``scripts/compile_ui.sh``). This module keeps only the wiring
+ behaviour. Thresholds, spike delta and frequency reset are saved per touch
node (``Settings.touch_tuning``) and restored on the next session.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QGroupBox, QWidget

from src.config.settings import Settings
from src.core.touch_zones import QUADRANT_LABELS, quadrant_names
from src.gui.monitor.live_sensor_window import LiveSensorWindow
from src.gui.ui_touch_tuning_panel import Ui_TouchTuningPanel
from src.hardware.skin import Skin


class TouchTuningPanel(QGroupBox, Ui_TouchTuningPanel):
    """Per-quadrant threshold/hysteresis tuning + sensor re-zero for a skin."""

    _live_data = Signal(object)

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._skin = skin
        self._settings_key = Settings.touch_tuning_key(
            getattr(skin, "touch", None), getattr(skin, "skin_type", ""))
        self._live_window: LiveSensorWindow | None = None

        # Compact styling so the panel stays small under the skin grid view.
        # Applied in code (not the .ui) because it is relative to the inherited
        # point size; the groupbox title keeps the normal size.
        compact = self.font()
        compact.setPointSizeF(max(7.0, compact.pointSizeF() - 1.0))
        for child in self.findChildren(QWidget):
            child.setFont(compact)

        # Per-sensor thresholds (index 0..3), seeded from the skin. Each
        # column is titled with the corner that sensor is configured to sit
        # in (the skin's ``sensor_quadrants``), so the labels match what the
        # detector reports.
        self._threshold_spins = [self.thr0, self.thr1, self.thr2, self.thr3]
        touch_cfg = getattr(skin, "touch", None) or {}
        names = quadrant_names(4, touch_cfg.get("sensor_quadrants"))
        for i, label in enumerate([self.q0_label, self.q1_label,
                                   self.q2_label, self.q3_label]):
            short = "".join(w[0] for w in QUADRANT_LABELS[names[i]].split("-"))
            label.setText(f"{names[i]} ({short.upper()})")
        thresholds = skin.touch_thresholds or [100.0, 100.0, 100.0, 100.0]
        for i, spin in enumerate(self._threshold_spins):
            spin.setValue(float(thresholds[i]) if i < len(thresholds) else 100.0)
            spin.valueChanged.connect(self._apply_thresholds)

        hysteresis = skin.touch_hysteresis if skin.touch_hysteresis is not None else 20.0
        self.hyst_spin.setValue(float(hysteresis))
        self.hyst_spin.valueChanged.connect(self._apply_hysteresis)

        self.spike_spin.setValue(self._initial_spike_threshold())
        self.spike_spin.valueChanged.connect(self._apply_spike_threshold)
        self.frequency_reset_spin.setValue(skin.press_rate.stale_ms)
        self.frequency_reset_spin.valueChanged.connect(self._apply_frequency_reset)

        self.apply_btn.clicked.connect(self._apply_node_config)
        self.rebaseline_btn.clicked.connect(self._rebaseline)
        self.adaptive_chk.toggled.connect(self._apply_adaptive_baseline)
        self.tau_spin.valueChanged.connect(self._apply_adaptive_baseline)

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
        if self._live_window is not None and self._live_window.isVisible():
            self._live_window.update_data(data)

    def _apply_thresholds(self) -> None:
        thresholds = [s.value() for s in self._threshold_spins]
        self._skin.set_touch_thresholds(thresholds)
        source = getattr(self._skin, "touch_source", None)
        if source is not None and hasattr(source, "set_thresholds_ut"):
            source.set_thresholds_ut(thresholds)
        self._save_tuning("quadrant_thresholds", thresholds)

    def _save_tuning(self, field: str, value: float | list[float]) -> None:
        # A fresh Settings reads the file first, so other saves are kept.
        Settings().set_touch_tuning(self._settings_key, field, value)

    def _apply_hysteresis(self) -> None:
        self._skin.set_touch_hysteresis(self.hyst_spin.value())

    def _initial_spike_threshold(self) -> float:
        """The skin's live spike delta, else a quarter of the first threshold."""
        saved = (getattr(self._skin, "touch", None) or {}).get("rhythm_spike_ut")
        if isinstance(saved, (int, float)):
            return float(saved)
        thresholds = self._skin.touch_thresholds or [100.0]
        return max(20.0, float(thresholds[0]) * 0.25)

    def _apply_spike_threshold(self) -> None:
        value = self.spike_spin.value()
        self._skin.set_touch_spike_ut(value)
        self._save_tuning("spike_threshold_ut", value)

    def _apply_frequency_reset(self) -> None:
        value = self.frequency_reset_spin.value()
        self._skin.press_rate.set_stale_ms(value)
        self._save_tuning("frequency_reset_ms", value)

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

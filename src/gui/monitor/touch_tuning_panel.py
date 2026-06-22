"""TouchTuningPanel — live tuning for a skin's quadrant touch detection.

Shown under the SkinGridView when a skin has 4-sensor touch tracking. Lets the
operator adjust the per-quadrant detection thresholds + hysteresis while the
activity runs (applied immediately to the skin's QuadrantDetector), re-zero the
magnetic sensors on the node over ESP-NOW, and toggle the node's adaptive
baseline.

The layout lives in ``src/gui/ui/touch_tuning_panel.ui`` (edit it in Qt Designer
and recompile with ``scripts/compile_ui.sh``). This module keeps only the wiring
+ behaviour. Changes are runtime-only — they tune the live detector but are not
written back to ``settings.yaml``; copy good values into the skin's ``touch:``
block to keep them.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGroupBox, QWidget

from src.gui.ui_touch_tuning_panel import Ui_TouchTuningPanel
from src.hardware.skin import Skin


class TouchTuningPanel(QGroupBox, Ui_TouchTuningPanel):
    """Per-quadrant threshold/hysteresis tuning + sensor re-zero for a skin."""

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._skin = skin

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

        self.apply_btn.clicked.connect(self._apply_node_config)
        self.rebaseline_btn.clicked.connect(self._rebaseline)
        self.adaptive_chk.toggled.connect(self._apply_adaptive_baseline)
        self.tau_spin.valueChanged.connect(self._apply_adaptive_baseline)

    # ------------------------------------------------------------------

    def _apply_thresholds(self) -> None:
        self._skin.set_touch_thresholds([s.value() for s in self._threshold_spins])

    def _apply_hysteresis(self) -> None:
        self._skin.set_touch_hysteresis(self.hyst_spin.value())

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
        """Send configure to the firmware to ensure fullscale_mt is set high
        (so 'mag' values are in a useful range for Serial debug) and
        act_threshold matches a sensible default."""
        ctrl = getattr(self._skin, "touch_controller", None)
        if ctrl is None or not hasattr(ctrl, "send_command"):
            return
        ctrl.send_command("configure", fullscale_mt=1000.0, act_threshold=0.3)

    def _apply_adaptive_baseline(self) -> None:
        """Toggle the node's adaptive baseline (and its time constant)."""
        ctrl = getattr(self._skin, "touch_controller", None)
        if ctrl is None or not hasattr(ctrl, "send_command"):
            return
        ctrl.send_command("configure",
                          adaptive_baseline=self.adaptive_chk.isChecked(),
                          baseline_tau_ms=self.tau_spin.value())

"""TouchTuningPanel — live tuning for a skin's quadrant touch detection.

Shown under the SkinGridView when a skin has 4-sensor touch tracking. Lets the
operator adjust the per-quadrant detection thresholds + hysteresis while the
activity runs (applied immediately to the skin's QuadrantDetector), and re-zero
the magnetic sensors on the node over ESP-NOW.

Changes are runtime-only — they tune the live detector but are not written back
to ``settings.yaml``; copy good values into the skin's ``touch:`` block to keep
them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QWidget,
)

from src.hardware.skin import Skin

# Sensor index -> quadrant label (matches QuadrantDetector: S0..S3 = Q1..Q4).
_QUADRANTS = ("Q1 (TL)", "Q2 (TR)", "Q3 (BL)", "Q4 (BR)")


class TouchTuningPanel(QGroupBox):
    """Per-quadrant threshold/hysteresis tuning + sensor re-zero for a skin."""

    def __init__(self, skin: Skin, parent: QWidget | None = None) -> None:
        super().__init__("Touch tuning", parent)
        self._skin = skin

        thresholds = skin.touch_thresholds or [0.3, 0.3, 0.3, 0.3]
        hysteresis = skin.touch_hysteresis if skin.touch_hysteresis is not None else 0.05

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)

        self._threshold_spins: list[QDoubleSpinBox] = []
        for i, label in enumerate(_QUADRANTS):
            grid.addWidget(QLabel(label), 0, i, alignment=Qt.AlignmentFlag.AlignHCenter)
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.01)
            spin.setDecimals(2)
            spin.setValue(float(thresholds[i]) if i < len(thresholds) else 0.3)
            spin.setToolTip(f"Activation threshold for {label} (0–1 of full scale)")
            spin.valueChanged.connect(self._apply_thresholds)
            grid.addWidget(spin, 1, i)
            self._threshold_spins.append(spin)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Hysteresis:"))
        self._hyst_spin = QDoubleSpinBox()
        self._hyst_spin.setRange(0.0, 0.5)
        self._hyst_spin.setSingleStep(0.01)
        self._hyst_spin.setDecimals(2)
        self._hyst_spin.setValue(float(hysteresis))
        self._hyst_spin.setToolTip("Drop-out margin below threshold (anti-flicker)")
        self._hyst_spin.valueChanged.connect(self._apply_hysteresis)
        bottom.addWidget(self._hyst_spin)
        bottom.addStretch(1)

        self._rebaseline_btn = QPushButton("Re-zero sensors")
        self._rebaseline_btn.setToolTip(
            "Tell the touch node to recapture its baseline (ESP-NOW rebaseline)")
        self._rebaseline_btn.clicked.connect(self._rebaseline)
        bottom.addWidget(self._rebaseline_btn)

        grid.addLayout(bottom, 2, 0, 1, len(_QUADRANTS))

    # ------------------------------------------------------------------

    def _apply_thresholds(self) -> None:
        self._skin.set_touch_thresholds([s.value() for s in self._threshold_spins])

    def _apply_hysteresis(self) -> None:
        self._skin.set_touch_hysteresis(self._hyst_spin.value())

    def _rebaseline(self) -> None:
        sent = self._skin.rebaseline_touch()
        # Brief visual confirmation on the button.
        self._rebaseline_btn.setText("Re-zeroed" if sent else "Re-zeroed (local)")
        self._rebaseline_btn.setEnabled(False)
        QTimer.singleShot(900, self._restore_button)

    def _restore_button(self) -> None:
        self._rebaseline_btn.setText("Re-zero sensors")
        self._rebaseline_btn.setEnabled(True)

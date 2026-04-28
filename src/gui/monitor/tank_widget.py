"""TankWidget — visualises a reservoir tank (pressure or vacuum) on a robot.

One TankWidget per AirReservoir. The tank pressure is read from
``reservoir.pressure`` (already a 0-100 percent reading reported by the firmware).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.hardware.air_reservoir import AirReservoir


class _TankBar(QWidget):
    """Vertical bar showing the tank pressure as a percentage."""

    _PRESSURE_COLOR = QColor("#27ae60")   # green for positive tank
    _VACUUM_COLOR   = QColor("#8e44ad")   # purple for vacuum tank
    _BG_COLOR       = QColor("#e0e0e0")
    _BORDER_COLOR   = QColor("#7f8c8d")

    def __init__(self, kind: str) -> None:
        super().__init__()
        self._kind = kind
        self._current: int = 0
        self.setFixedWidth(22)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_value(self, current: int) -> None:
        if self._current != current:
            self._current = current
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, self._BG_COLOR)
        if self._current > 0:
            color = self._PRESSURE_COLOR if self._kind == "pressure" else self._VACUUM_COLOR
            fill_h = int(h * self._current / 100)
            p.fillRect(0, h - fill_h, w, fill_h, color)
        p.setPen(QPen(self._BORDER_COLOR, 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class TankWidget(QGroupBox):
    """Widget for a single reservoir tank (pressure or vacuum)."""

    def __init__(self, reservoir: AirReservoir) -> None:
        title = "Pressure" if reservoir.kind == "pressure" else "Vacuum"
        super().__init__(title)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._reservoir = reservoir
        self.setFixedWidth(60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        self._bar = _TankBar(reservoir.kind)
        layout.addWidget(self._bar, alignment=Qt.AlignmentFlag.AlignHCenter)

        _SMALL = "font-size: 9px;"
        self._pressure_lbl = QLabel("0%")
        self._pressure_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pressure_lbl.setStyleSheet(_SMALL)
        layout.addWidget(self._pressure_lbl)

        self._state_lbl = QLabel("OFF")
        self._state_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_lbl.setStyleSheet(_SMALL)
        layout.addWidget(self._state_lbl)

    def refresh(self) -> None:
        current = self._reservoir.pressure
        self._bar.set_value(current)
        self._pressure_lbl.setText(f"{current}%")
        if not self._reservoir.is_connected:
            self._state_lbl.setText("OFF")
        else:
            self._state_lbl.setText("ON")

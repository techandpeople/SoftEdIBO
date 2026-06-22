"""TankBar — vertical bar showing a reservoir tank's pressure as a percentage.

A pure QPainter widget, kept in its own module so it can be used as a *promoted
widget* inside ``ui/tank_widget.ui``. Promoted widgets are built by ``setupUi``
with the default constructor, so ``kind`` defaults to ``"pressure"`` and the
panel calls :meth:`set_kind` afterwards to pick the colour.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class TankBar(QWidget):
    """Vertical bar showing the tank pressure as a percentage."""

    _PRESSURE_COLOR = QColor("#27ae60")   # green for positive tank
    _VACUUM_COLOR   = QColor("#8e44ad")   # purple for vacuum tank
    _BG_COLOR       = QColor("#e0e0e0")
    _BORDER_COLOR   = QColor("#7f8c8d")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = "pressure"
        self._current: int = 0
        self.setFixedWidth(22)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_kind(self, kind: str) -> None:
        if self._kind != kind:
            self._kind = kind
            self.update()

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

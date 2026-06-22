"""PressureBar — vertical bar showing current pressure (fill) + target (line).

A pure QPainter widget, kept in its own module so it can be used as a *promoted
widget* inside ``ui/chamber_widget.ui`` without a circular import (the generated
``ui_chamber_widget`` imports this; the panel imports the generated class).
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class PressureBar(QWidget):
    """Vertical bar showing current pressure (fill) and target pressure (line)."""

    _BAR_COLOR    = QColor("#3daee9")   # current fill
    _TARGET_COLOR = QColor("#da4453")   # target line
    _BG_COLOR     = QColor("#e0e0e0")   # empty portion

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current: int = 0
        self._target:  int = 0
        self.setFixedWidth(18)
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_values(self, current: int, target: int) -> None:
        if self._current != current or self._target != target:
            self._current = current
            self._target  = target
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, self._BG_COLOR)

        # Current pressure fill (from bottom)
        if self._current > 0:
            fill_h = int(h * self._current / 100)
            p.fillRect(0, h - fill_h, w, fill_h, self._BAR_COLOR)

        # Target pressure line (2 px, full width)
        if self._target > 0:
            target_y = h - int(h * self._target / 100)
            target_y = max(1, min(h - 2, target_y))
            pen = QPen(self._TARGET_COLOR, 2)
            p.setPen(pen)
            p.drawLine(0, target_y, w, target_y)

        p.end()

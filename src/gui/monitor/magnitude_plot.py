"""MagnitudePlot - rolling per-sensor magnitude plot (uT).

A custom-painted widget, placed in ``live_sensor_window.ui`` as a promoted
widget. One small plot per sensor, sized to however many sensors the stream
carries, each with its activation threshold as a dashed line.
"""

from __future__ import annotations

from collections import deque
from math import ceil, sqrt

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

_HISTORY = 240
_DEFAULT_THRESHOLD_UT = 100.0


class MagnitudePlot(QWidget):
    """Rolling magnitude plot fed by the live sensor window."""

    _colors = (QColor("#e74c3c"), QColor("#2ecc71"),
               QColor("#3498db"), QColor("#f1c40f"))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: list[deque[float]] = []
        self._thresholds: list[float] = []
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def add_sample(self, magnitudes: list[float], thresholds: list[float]) -> None:
        """Append one frame; the plot grows to the frame's sensor count."""
        while len(self._history) < len(magnitudes):
            self._history.append(deque(maxlen=_HISTORY))
        self._thresholds = [
            float(thresholds[i]) if i < len(thresholds) else _DEFAULT_THRESHOLD_UT
            for i in range(len(self._history))]
        for index, history in enumerate(self._history):
            value = magnitudes[index] if index < len(magnitudes) else 0.0
            history.append(max(0.0, float(value)))
        self.update()

    def _grid(self) -> tuple[int, int]:
        count = max(1, len(self._history))
        cols = 2 if count <= 4 else ceil(sqrt(count))
        return cols, ceil(count / cols)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#202124"))
        painter.setPen(QColor("#e8eaed"))
        painter.drawText(12, 16, "Live magnitude (uT)")
        painter.setPen(QColor("#bdc1c6"))
        painter.drawText(self.width() - 105, 16, "recent samples")

        cols, rows = self._grid()
        gap = 8
        cell_width = (self.width() - gap * (cols + 1)) // cols
        cell_height = (self.height() - 32 - gap * (rows + 1)) // rows
        if cell_width <= 0 or cell_height <= 0:
            return

        for index, history in enumerate(self._history):
            color = self._colors[index % len(self._colors)]
            cell = QRect((index % cols) * (cell_width + gap) + gap,
                         (index // cols) * (cell_height + gap) + 24,
                         cell_width, cell_height)
            plot = cell.adjusted(38, 22, -8, -22)
            threshold = self._thresholds[index]
            y_max = max(100.0, ceil(max([*history, threshold, 100.0]) / 100.0) * 100.0)

            painter.setPen(QPen(QColor("#5f6368"), 1))
            painter.drawRect(cell)
            painter.setPen(QColor("#e8eaed"))
            painter.drawText(cell.left() + 8, cell.top() + 15, f"T{index}")
            painter.setPen(QColor("#bdc1c6"))
            painter.drawText(cell.left() + 5, plot.top() + 5, f"{y_max:.0f}")
            painter.drawText(cell.left() + 18, plot.bottom() + 16, "0")

            painter.setPen(QPen(QColor("#45484d"), 1))
            painter.drawLine(plot.topLeft(), plot.topRight())
            painter.drawLine(plot.bottomLeft(), plot.bottomRight())
            painter.drawLine(plot.topLeft(), plot.bottomLeft())
            threshold_y = int(plot.bottom() - threshold / y_max * plot.height())
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            painter.drawLine(plot.left(), threshold_y, plot.right(), threshold_y)

            if len(history) < 2:
                continue
            step = plot.width() / (len(history) - 1)
            points = [(int(plot.left() + i * step),
                       int(plot.bottom() - (value / y_max) * plot.height()))
                      for i, value in enumerate(history)]
            painter.setPen(QPen(color, 2))
            for start, end in zip(points, points[1:]):
                painter.drawLine(*start, *end)

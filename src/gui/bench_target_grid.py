"""Target-grid display for the touch position bench.

Draws the press grid the user marked on the skin and highlights the cell being
prompted; a short green flash acknowledges each accepted press. Row-major
numbering from the top-left, matching the capture prompts and the dataset
labels. Built in code and inserted into the dialog's placeholder layout, like
:class:`~src.gui.sensor_grid_view.SensorGridView`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

_TARGET = QColor(255, 193, 7)        # amber - cell to press now
_FLASH = QColor(76, 175, 80)         # green - press accepted
_GRID_PEN = QColor(128, 128, 128)
_TEXT = QColor(128, 128, 128)
_FLASH_MS = 350


class BenchTargetGrid(QWidget):
    """The prompted press grid: amber target cell, green accepted-press flash."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = 4
        self._cols = 4
        self._target: tuple[int, int] | None = None
        self._flash = False
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(220, 160)
        self.setWhatsThis(
            "The press grid as marked on the skin, seen facing it: the "
            "highlighted cell is the one to press now. Cells are numbered "
            "row,col from the top-left. A green flash confirms each accepted "
            "press.")

    def set_grid(self, rows: int, cols: int) -> None:
        self._rows = max(1, rows)
        self._cols = max(1, cols)
        self._target = None
        self.update()

    def set_target(self, cell: tuple[int, int] | None) -> None:
        self._target = cell
        self.update()

    def flash_press(self) -> None:
        self._flash = True
        self._flash_timer.start(_FLASH_MS)
        self.update()

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cell = min(w / self._cols, h / self._rows)
        gw, gh = cell * self._cols, cell * self._rows
        x0, y0 = (w - gw) / 2, (h - gh) / 2

        if self._target is not None:
            r, c = self._target
            color = _FLASH if self._flash else _TARGET
            painter.fillRect(int(x0 + c * cell), int(y0 + r * cell),
                             int(cell), int(cell), color)

        painter.setPen(QPen(_GRID_PEN, 1))
        for r in range(self._rows + 1):
            y = y0 + r * cell
            painter.drawLine(int(x0), int(y), int(x0 + gw), int(y))
        for c in range(self._cols + 1):
            x = x0 + c * cell
            painter.drawLine(int(x), int(y0), int(x), int(y0 + gh))

        painter.setPen(QPen(_TEXT, 1))
        font = painter.font()
        font.setPointSizeF(max(7.0, cell / 5.0))
        painter.setFont(font)
        for r in range(self._rows):
            for c in range(self._cols):
                painter.drawText(
                    int(x0 + c * cell), int(y0 + r * cell),
                    int(cell), int(cell),
                    Qt.AlignmentFlag.AlignCenter, f"{r},{c}")

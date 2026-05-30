"""Skin layout grid editor.

A small custom widget that paints a NxM grid representing a skin's physical
surface. The user can paint two independent layers:

- **chamber layer**: which cell belongs to which inflatable chamber
  (-1 = no chamber). Index points into the skin's ``chambers`` list.
- **sensor layer**: which cell maps to which touch sensor index
  (-1 = no sensor). Sensor indices come from the linked ``node_imu``.

Both layers are drawn simultaneously (sensor as background colour, chamber
shown as a rounded rectangle on top); cell labels follow the active layer
(``C*`` in chamber mode, ``S*`` in sensor mode) to avoid visual overlap.

Editing:
  * **Left-click** paints the focused cell with the selected palette value.
  * **Right-click** clears the focused cell on the current layer.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

CHAMBER_PALETTE = [
    QColor("#e74c3c"), QColor("#3498db"), QColor("#2ecc71"),
    QColor("#f39c12"), QColor("#9b59b6"), QColor("#1abc9c"),
]

SENSOR_PALETTE = [
    QColor("#fadbd8"), QColor("#d6eaf8"), QColor("#d4efdf"),
    QColor("#fdebd0"), QColor("#ebdef0"), QColor("#d1f2eb"),
    QColor("#fcf3cf"), QColor("#f5cba7"),
]

EMPTY = -1


class SkinGridEditor(QWidget):
    """Editable two-layer grid for a skin's chamber/sensor layout."""

    cell_changed = Signal()

    def __init__(self, cols: int = 8, rows: int = 4, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cols = max(1, cols)
        self._rows = max(1, rows)
        self._chamber_grid: list[list[int]] = [[EMPTY] * self._cols for _ in range(self._rows)]
        self._sensor_grid:  list[list[int]] = [[EMPTY] * self._cols for _ in range(self._rows)]
        self._layer = "chamber"   # "chamber" or "sensor"
        self._paint_value = 0     # which chamber/sensor index to paint with
        self.setMinimumSize(320, 160)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_dimensions(self, cols: int, rows: int) -> None:
        cols = max(1, cols)
        rows = max(1, rows)
        if cols == self._cols and rows == self._rows:
            return
        # Resize grids, preserving overlap top-left.
        def resize(g: list[list[int]]) -> list[list[int]]:
            out = [[EMPTY] * cols for _ in range(rows)]
            for r in range(min(rows, self._rows)):
                for c in range(min(cols, self._cols)):
                    out[r][c] = g[r][c]
            return out
        self._chamber_grid = resize(self._chamber_grid)
        self._sensor_grid  = resize(self._sensor_grid)
        self._cols, self._rows = cols, rows
        self.update()
        self.cell_changed.emit()

    def set_layer(self, layer: str) -> None:
        if layer in ("chamber", "sensor"):
            self._layer = layer
            self.update()

    def set_paint_target(self, value: int) -> None:
        """Select which chamber/sensor index is being painted with left-click."""
        self._paint_value = value

    def chamber_grid(self) -> list[list[int]]:
        return [row[:] for row in self._chamber_grid]

    def sensor_grid(self) -> list[list[int]]:
        return [row[:] for row in self._sensor_grid]

    def set_chamber_grid(self, g: list[list[int]] | None) -> None:
        self._chamber_grid = self._normalise(g)
        self.update()

    def set_sensor_grid(self, g: list[list[int]] | None) -> None:
        self._sensor_grid = self._normalise(g)
        self.update()

    def cols(self) -> int:
        return self._cols

    def rows(self) -> int:
        return self._rows

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(self._cols * 32, self._rows * 32)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._paint_cell_at(ev.position().toPoint(), self._paint_value)
        elif ev.button() == Qt.MouseButton.RightButton:
            self._paint_cell_at(ev.position().toPoint(), EMPTY)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        # Click-and-drag continues painting (or erasing) over the cells the
        # cursor visits, so multiple cells can be selected in one gesture.
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self._paint_cell_at(ev.position().toPoint(), self._paint_value)
        elif ev.buttons() & Qt.MouseButton.RightButton:
            self._paint_cell_at(ev.position().toPoint(), EMPTY)

    def paintEvent(self, _: QPaintEvent) -> None:
        cw, ch, ox, oy = self._cell_metrics()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for r in range(self._rows):
            for c in range(self._cols):
                cell = QRect(ox + c * cw, oy + r * ch, cw, ch)
                self._paint_cell(p, cell, self._chamber_grid[r][c],
                                 self._sensor_grid[r][c])

    def _paint_cell(self, p: QPainter, cell: QRect,
                    ch_idx: int, s_idx: int) -> None:
        """Render a single grid cell — only the active layer is shown so each
        mode gives a clean, unambiguous view of what's being edited."""
        # Empty background + border (always drawn — gives the grid structure).
        p.fillRect(cell, QColor("#fdfefe"))
        p.setPen(QColor("#bdc3c7"))
        p.drawRect(cell)
        if self._layer == "chamber" and ch_idx >= 0:
            inner = cell.adjusted(2, 2, -2, -2)
            p.fillRect(inner, CHAMBER_PALETTE[ch_idx % len(CHAMBER_PALETTE)])
            p.setPen(QColor("white"))
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, f"C{ch_idx}")
        elif self._layer == "sensor" and s_idx >= 0:
            inner = cell.adjusted(2, 2, -2, -2)
            p.fillRect(inner, SENSOR_PALETTE[s_idx % len(SENSOR_PALETTE)])
            p.setPen(QColor("#566573"))
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, f"S{s_idx}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _normalise(self, g: list[list[int]] | None) -> list[list[int]]:
        out = [[EMPTY] * self._cols for _ in range(self._rows)]
        if not g:
            return out
        for r in range(min(self._rows, len(g))):
            row = g[r]
            for c in range(min(self._cols, len(row))):
                out[r][c] = int(row[c])
        return out

    def _cell_metrics(self) -> tuple[int, int, int, int]:
        w, h = self.width(), self.height()
        cw = w // self._cols
        ch = h // self._rows
        ox = (w - cw * self._cols) // 2
        oy = (h - ch * self._rows) // 2
        return max(cw, 1), max(ch, 1), ox, oy

    def _paint_cell_at(self, pos: QPoint, value: int) -> None:
        cw, ch, ox, oy = self._cell_metrics()
        c = (pos.x() - ox) // cw
        r = (pos.y() - oy) // ch
        if not (0 <= c < self._cols and 0 <= r < self._rows):
            return
        grid = self._chamber_grid if self._layer == "chamber" else self._sensor_grid
        if grid[r][c] == value:
            return
        grid[r][c] = value
        self.update()
        self.cell_changed.emit()

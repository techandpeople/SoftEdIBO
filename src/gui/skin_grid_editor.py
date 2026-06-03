"""Skin layout grid editor.

A small custom widget that paints a NxM grid representing a skin's physical
surface. The user can paint two independent layers:

- **chamber layer**: which cell belongs to which inflatable chamber
  (-1 = no chamber). Index points into the skin's ``chambers`` list.
- **sensor layer**: which cell maps to which touch sensor index
  (-1 = no sensor). Sensor indices come from the linked ``node_magnet_sensor``.

The two layers can have **different dimensions** (e.g. 4×2 for chambers,
8×4 for sensors) — the editor swaps the visible resolution when the user
switches mode. Cell labels follow the active layer (``C*`` in chamber
mode, ``S*`` in sensor mode) to avoid visual overlap.

Skin **shape** is one of:

- ``"rect"`` — every cell is paintable (the whole rectangle).
- ``"round"`` — only cells whose centroid sits inside the inscribed
  circle are paintable; off-mask cells are drawn muted so the user can
  still see where the boundary lies.

Editing:
  * **Left-click** (or drag) paints the focused cell with the selected value.
  * **Right-click** (or drag) clears the focused cell on the current layer.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
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

_ROUND_BORDER = QColor("#566573")  # outline of the round-skin clip


class SkinGridEditor(QWidget):
    """Editable two-layer grid for a skin's chamber/sensor layout."""

    cell_changed = Signal()

    def __init__(self, cols: int = 8, rows: int = 4,
                 shape: str = "rect",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Per-layer dimensions. Both layers start with the same dims for
        # backward compatibility — the dialog drives them independently.
        self._cols = {"chamber": max(1, cols), "sensor": max(1, cols)}
        self._rows = {"chamber": max(1, rows), "sensor": max(1, rows)}
        self._chamber_grid: list[list[int]] = self._blank("chamber")
        self._sensor_grid:  list[list[int]] = self._blank("sensor")
        self._layer = "chamber"
        self._paint_value = 0
        self._shape = shape if shape in ("rect", "round") else "rect"
        self.setMinimumSize(320, 160)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_dimensions(self, cols: int, rows: int,
                       layer: str | None = None) -> None:
        """Resize a layer (default: the active one). Existing cells in the
        top-left overlap are preserved; new cells default to EMPTY."""
        layer = layer or self._layer
        if layer not in ("chamber", "sensor"):
            return
        cols = max(1, cols)
        rows = max(1, rows)
        old_cols = self._cols[layer]
        old_rows = self._rows[layer]
        if cols == old_cols and rows == old_rows:
            return
        old_grid = (self._chamber_grid if layer == "chamber"
                    else self._sensor_grid)
        out = [[EMPTY] * cols for _ in range(rows)]
        for r in range(min(rows, old_rows)):
            for c in range(min(cols, old_cols)):
                out[r][c] = old_grid[r][c]
        self._cols[layer] = cols
        self._rows[layer] = rows
        if layer == "chamber":
            self._chamber_grid = out
        else:
            self._sensor_grid = out
        self.update()
        self.cell_changed.emit()

    def set_layer(self, layer: str) -> None:
        if layer in ("chamber", "sensor"):
            self._layer = layer
            self.update()

    def set_shape(self, shape: str) -> None:
        """Switch between ``"rect"`` and ``"round"``. Round masks out cells
        whose centroid falls outside the inscribed circle."""
        if shape in ("rect", "round") and shape != self._shape:
            self._shape = shape
            self.update()

    def set_paint_target(self, value: int) -> None:
        """Select which chamber/sensor index is being painted with left-click."""
        self._paint_value = value

    def chamber_grid(self) -> list[list[int]]:
        return [row[:] for row in self._chamber_grid]

    def sensor_grid(self) -> list[list[int]]:
        return [row[:] for row in self._sensor_grid]

    def set_chamber_grid(self, g: list[list[int]] | None) -> None:
        self._chamber_grid = self._normalise(g, "chamber")
        self.update()

    def set_sensor_grid(self, g: list[list[int]] | None) -> None:
        self._sensor_grid = self._normalise(g, "sensor")
        self.update()

    def cols(self) -> int:
        return self._cols[self._layer]

    def rows(self) -> int:
        return self._rows[self._layer]

    def chamber_cols(self) -> int:
        return self._cols["chamber"]

    def chamber_rows(self) -> int:
        return self._rows["chamber"]

    def sensor_cols(self) -> int:
        return self._cols["sensor"]

    def sensor_rows(self) -> int:
        return self._rows["sensor"]

    def shape(self) -> str:
        return self._shape

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(self.cols() * 32, self.rows() * 32)

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
        cols, rows = self.cols(), self.rows()
        grid = self._chamber_grid if self._layer == "chamber" else self._sensor_grid
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Round skins clip painting to the inscribed ellipse — cells on the
        # border get partially shown (the bits outside the circle are clipped),
        # cells fully outside disappear. No cell is "invalidated"; the user
        # can still paint them and the data is preserved verbatim.
        if self._shape == "round":
            ellipse_rect = QRectF(ox, oy, cols * cw, rows * ch)
            path = QPainterPath()
            path.addEllipse(ellipse_rect)
            p.setClipPath(path)

        for r in range(rows):
            for c in range(cols):
                cell = QRect(ox + c * cw, oy + r * ch, cw, ch)
                value = grid[r][c] if (r < len(grid) and c < len(grid[r])) else -1
                self._paint_cell(p, cell, value)

        # Draw the circle outline on top, unclipped, so the round boundary
        # is always visible regardless of which cells are painted underneath.
        if self._shape == "round":
            p.setClipping(False)
            p.setPen(QPen(_ROUND_BORDER, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(ox, oy, cols * cw, rows * ch))

    def _paint_cell(self, p: QPainter, cell: QRect, value: int) -> None:
        """Render a single grid cell — only the active layer is shown so each
        mode gives a clean, unambiguous view of what's being edited."""
        # Empty background + border (always drawn — gives the grid structure).
        p.fillRect(cell, QColor("#fdfefe"))
        p.setPen(QColor("#bdc3c7"))
        p.drawRect(cell)
        if value < 0:
            return
        inner = cell.adjusted(2, 2, -2, -2)
        if self._layer == "chamber":
            p.fillRect(inner, CHAMBER_PALETTE[value % len(CHAMBER_PALETTE)])
            p.setPen(QColor("white"))
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, f"C{value}")
        else:
            p.fillRect(inner, SENSOR_PALETTE[value % len(SENSOR_PALETTE)])
            p.setPen(QColor("#566573"))
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, f"S{value}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _blank(self, layer: str) -> list[list[int]]:
        return [[EMPTY] * self._cols[layer] for _ in range(self._rows[layer])]

    def _normalise(self, g: list[list[int]] | None,
                   layer: str) -> list[list[int]]:
        cols, rows = self._cols[layer], self._rows[layer]
        out = [[EMPTY] * cols for _ in range(rows)]
        if not g:
            return out
        for r in range(min(rows, len(g))):
            row = g[r]
            for c in range(min(cols, len(row))):
                out[r][c] = int(row[c])
        return out

    def _cell_metrics(self) -> tuple[int, int, int, int]:
        w, h = self.width(), self.height()
        cols, rows = self.cols(), self.rows()
        cw = w // cols
        ch = h // rows
        ox = (w - cw * cols) // 2
        oy = (h - ch * rows) // 2
        return max(cw, 1), max(ch, 1), ox, oy

    def _paint_cell_at(self, pos: QPoint, value: int) -> None:
        cw, ch, ox, oy = self._cell_metrics()
        cols, rows = self.cols(), self.rows()
        c = (pos.x() - ox) // cw
        r = (pos.y() - oy) // ch
        if not (0 <= c < cols and 0 <= r < rows):
            return
        # No off-mask gating on round skins — clipping is purely visual now,
        # so any cell whose centre lies inside the widget is paintable. Cells
        # fully outside the inscribed circle simply render invisible.
        grid = self._chamber_grid if self._layer == "chamber" else self._sensor_grid
        if grid[r][c] == value:
            return
        grid[r][c] = value
        self.update()
        self.cell_changed.emit()

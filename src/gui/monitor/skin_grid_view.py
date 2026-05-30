"""SkinGridView — read-only spatial view of a skin during activities.

Renders the skin's ``chamber_grid`` (rows × cols of local chamber indices)
as one merged region per chamber — adjacent cells with the same chamber
index are grouped into a single connected component, filled with the
chamber's palette colour at an opacity proportional to its current pressure,
and labelled once (``Cn / NN%``) in the centre of the component.

The widget is read-only — it does not handle clicks; activities still use
ChamberWidget for inflate / deflate / touch controls. SkinGridView is purely
to give the user a visual map of where each chamber sits and how full it is.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from src.gui.skin_grid_editor import CHAMBER_PALETTE
from src.hardware.skin import Skin

_EMPTY_BG     = QColor("#fdfefe")
_CELL_PEN     = QColor("#bdc3c7")
_LABEL_PEN    = QColor("#1c2833")
_REGION_PEN   = QColor("#566573")  # outline of each chamber region

# 4-connectivity offsets used when grouping cells into regions.
_NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1))


class SkinGridView(QWidget):
    """Spatial view of a skin's chambers + current pressure levels.

    Adjacent cells sharing the same chamber index are merged into a single
    visual region (one outline, one label) so the view stays uncluttered
    even on large grids.
    """

    def __init__(self, skin: Skin, cell_px: int = 28,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._skin = skin
        self._cell_px = max(8, int(cell_px))
        self._cols, self._rows, self._grid = self._read_layout(skin)
        # Connected components per chamber index (computed once — chamber_grid
        # is static for the lifetime of this widget).
        self._regions: list[tuple[int, list[tuple[int, int]]]] = \
            self._find_regions(self._grid, self._rows, self._cols)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Trigger a repaint with the chambers' latest pressure values."""
        self.update()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(self._cols * self._cell_px, self._rows * self._cell_px)

    def paintEvent(self, _: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self._paint_empty_grid(p)
        for ch_idx, cells in self._regions:
            self._paint_region(p, ch_idx, cells)

    # ------------------------------------------------------------------
    # Painting helpers
    # ------------------------------------------------------------------

    def _paint_empty_grid(self, p: QPainter) -> None:
        """Background + thin cell borders for cells without a chamber."""
        cw = self._cell_px
        p.fillRect(self.rect(), _EMPTY_BG)
        p.setPen(_CELL_PEN)
        for r in range(self._rows):
            for c in range(self._cols):
                if self._grid[r][c] < 0:
                    p.drawRect(c * cw, r * cw, cw, cw)

    def _paint_region(self, p: QPainter, ch_idx: int,
                      cells: list[tuple[int, int]]) -> None:
        """Fill the region, draw its outline once, and place a single label."""
        cw = self._cell_px
        pressure = self._pressure_for(ch_idx)
        fill = self._tint(ch_idx, pressure)
        cell_set = set(cells)

        # Fill every cell of the region.
        for (r, c) in cells:
            p.fillRect(c * cw, r * cw, cw, cw, fill)

        # Outline: draw each external edge once. A cell edge is "external"
        # when the neighbour on the other side is not in this region.
        p.setPen(QPen(_REGION_PEN, 2))
        for (r, c) in cells:
            x, y = c * cw, r * cw
            if (r - 1, c) not in cell_set:
                p.drawLine(x, y, x + cw, y)
            if (r + 1, c) not in cell_set:
                p.drawLine(x, y + cw, x + cw, y + cw)
            if (r, c - 1) not in cell_set:
                p.drawLine(x, y, x, y + cw)
            if (r, c + 1) not in cell_set:
                p.drawLine(x + cw, y, x + cw, y + cw)

        # Single label: place it across the region's bounding box so the
        # text has room to fit even when each cell alone is narrow. The
        # centroid cell anchors the position to stay inside the shape on
        # L-shaped regions.
        ar, _ = self._centroid_cell(cells)
        min_r = min(r for r, _ in cells)
        max_r = max(r for r, _ in cells)
        min_c = min(c for _, c in cells)
        max_c = max(c for _, c in cells)
        # Width-wise we span the whole bbox; height-wise we keep it on the
        # centroid row plus its neighbours so the label sits visually inside.
        label_w = max(cw, (max_c - min_c + 1) * cw)
        label_x = min_c * cw
        label_y = max(0, (ar - 0) * cw)
        label_h = cw
        if min_r <= ar - 1:
            label_y -= cw // 2
            label_h += cw // 2
        if max_r >= ar + 1:
            label_h += cw // 2
        label_rect = QRect(label_x, label_y, label_w, label_h)

        font = p.font()
        font.setPointSizeF(max(7.0, cw * 0.32))
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(_LABEL_PEN)
        p.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextDontClip,
            f"C{ch_idx}  {pressure}%",
        )

    # ------------------------------------------------------------------
    # Geometry / data helpers
    # ------------------------------------------------------------------

    def _tint(self, ch_idx: int, pressure: int) -> QColor:
        """Lerp white → chamber colour by pressure/100 so 0 % is barely tinted
        and 100 % is the full palette colour."""
        base = CHAMBER_PALETTE[ch_idx % len(CHAMBER_PALETTE)]
        t = max(0, min(100, pressure)) / 100.0
        return QColor(
            int(255 + (base.red()   - 255) * t),
            int(255 + (base.green() - 255) * t),
            int(255 + (base.blue()  - 255) * t),
        )

    def _pressure_for(self, ch_idx: int) -> int:
        ch = self._skin.chambers.get(ch_idx)
        return int(ch.pressure) if ch is not None else 0

    @staticmethod
    def _centroid_cell(cells: list[tuple[int, int]]) -> tuple[int, int]:
        cy = sum(r for r, _ in cells) / len(cells)
        cx = sum(c for _, c in cells) / len(cells)
        return min(cells, key=lambda rc: (rc[0] - cy) ** 2 + (rc[1] - cx) ** 2)

    @staticmethod
    def _find_regions(grid: list[list[int]], rows: int, cols: int
                      ) -> list[tuple[int, list[tuple[int, int]]]]:
        """Group adjacent cells with the same chamber index into 4-connected
        components. Returns a list of (chamber_idx, cells)."""
        visited = [[False] * cols for _ in range(rows)]
        regions: list[tuple[int, list[tuple[int, int]]]] = []
        for r0 in range(rows):
            for c0 in range(cols):
                if visited[r0][c0] or grid[r0][c0] < 0:
                    continue
                ch_idx = grid[r0][c0]
                cells = SkinGridView._flood(grid, visited, r0, c0,
                                            rows, cols, ch_idx)
                regions.append((ch_idx, cells))
        return regions

    @staticmethod
    def _flood(grid: list[list[int]], visited: list[list[bool]],
               r0: int, c0: int, rows: int, cols: int,
               ch_idx: int) -> list[tuple[int, int]]:
        """BFS from (r0, c0) collecting all 4-connected cells with ``ch_idx``."""
        cells: list[tuple[int, int]] = []
        queue: deque[tuple[int, int]] = deque([(r0, c0)])
        visited[r0][c0] = True
        while queue:
            r, c = queue.popleft()
            cells.append((r, c))
            for dr, dc in _NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if (0 <= nr < rows and 0 <= nc < cols
                        and not visited[nr][nc]
                        and grid[nr][nc] == ch_idx):
                    visited[nr][nc] = True
                    queue.append((nr, nc))
        return cells

    @staticmethod
    def _read_layout(skin: Skin) -> tuple[int, int, list[list[int]]]:
        grid_cfg = skin.grid or {}
        cols = max(1, int(grid_cfg.get("cols", 0)))
        rows = max(1, int(grid_cfg.get("rows", 0)))
        raw = skin.chamber_grid or []
        if raw:
            rows = max(rows, len(raw))
            cols = max(cols, max((len(row) for row in raw), default=0))
        grid = [[-1] * cols for _ in range(rows)]
        for r in range(min(rows, len(raw))):
            row = raw[r]
            for c in range(min(cols, len(row))):
                grid[r][c] = int(row[c])
        return cols, rows, grid


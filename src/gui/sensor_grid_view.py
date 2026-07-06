"""SensorGridView — a live, geometry-driven spatial map of a skin's touch sensors.

Draws one cell per sensor in the skin's *natural* sensor grid (2×2 for the
Thymio's four magnetic sensors), clipped to the skin outline — the 'D' for the
``thymio`` shape — so a touch lights up where it physically happens. Unlike
:class:`~src.gui.monitor.skin_grid_view.SkinGridView` this needs no live ``Skin``
(no chambers/pressure): it takes a :class:`~src.hardware.skin_geometry.SkinGeometry`
and is fed raw ``mag`` arrays, so it drops into any dialog that sees the magnet
stream (guided gesture capture, Test Actuators).

A sensor is highlighted when its magnitude reaches the current **threshold**
(read live from a provider callable), so dragging a sensitivity control changes
what lights up immediately — no round-trip to the node needed to *see* the
effect. A fading **motion trail** follows the magnitude-weighted touch centroid,
so a slide draws its own arrow across the skin. Capacitive touch later: a skin
with more sensors just yields a finer grid from its geometry (or pass an
explicit grid) — no change here. See ``CAPACITIVE_GRID_EXAMPLE`` for the ready
bigger-grid hypothesis.
"""

from __future__ import annotations

import time
from math import atan2, cos, hypot, pi, sin
from typing import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from src.gui.skin_shapes import shape_path
from src.hardware.skin_geometry import SkinGeometry
from src.ml import gesture_taxonomy as _tax
from src.ml.gesture_taxonomy import SLIDE_MAX_Z_FRAC, SLIDE_MIN_STRAIGHTNESS
from src.ml.touch_motion import frame_z_frac, path_straightness, weighted_centroid

_BG = QColor("#fdfefe")
_CELL_PEN = QColor("#bdc3c7")
_OUTLINE_PEN = QColor("#566573")
_ACTIVE = QColor(46, 204, 113)     # touched sensor (green), matches SensorTester
_IDLE = QColor(214, 234, 248)      # resting cell (pale blue)
_LABEL = QColor("#1c2833")
_TRAIL = QColor(230, 126, 34)      # motion trail (orange), fades with age

# Motion trail: centroid samples older than this fade out completely; the
# centroid only registers while some sensor is at ≥ this fraction of the
# threshold (so idle noise doesn't wander the trail around).
_TRAIL_MS = 900.0
_TRAIL_FLOOR_FRAC = 0.3
_ARROW_LEN_PX = 10.0
_ARROW_MIN_TRAVEL_PX = 12.0
# A contact gap longer than this breaks the trail: only CONTINUOUS contact
# draws a path, so touching one spot and then another does not fake a slide.
# Short enough that separate taps (≥ ~200 ms apart) never bridge; long enough
# that a mid-slide dip between sensors (a few frames at ~28 Hz) survives.
_TRAIL_BREAK_MS = 150.0

# 4-sensor fallback when a skin type has no registry geometry: a plain 2×2.
_FALLBACK_GRID = (2, 2, [[0, 1], [2, 3]])

# Ready hypothesis for capacitive touch: a denser hardcoded grid. Pass this (or a
# geometry with more sensors) as ``grid=`` and the same widget renders it — the
# sensor count and layout are the only things that change.
CAPACITIVE_GRID_EXAMPLE = (4, 4, [[r * 4 + c for c in range(4)] for r in range(4)])


def _cells_by_value(grid: list[list[int]]) -> dict[int, tuple[int, int]]:
    """``{sensor_idx: (row, col)}`` for every non-negative cell."""
    out: dict[int, tuple[int, int]] = {}
    for r, row in enumerate(grid):
        for c, v in enumerate(row):
            if v >= 0:
                out[int(v)] = (r, c)
    return out


class SensorGridView(QWidget):
    """Live spatial grid of a skin's touch sensors, highlighted by threshold."""

    def __init__(self, geometry: SkinGeometry | None,
                 threshold_provider: Callable[[], float],
                 grid: tuple[int, int, list[list[int]]] | None = None,
                 cell_px: int = 70, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._threshold = threshold_provider
        self._shape = geometry.shape if geometry is not None else "rect"
        self._size_mm = geometry.size_mm if geometry is not None else None
        if grid is not None:
            self._cols, self._rows, self._grid = grid
        elif geometry is not None and geometry.sensor_count:
            self._cols, self._rows, self._grid = geometry.natural_sensor_grid()
        else:
            self._cols, self._rows, self._grid = (
                _FALLBACK_GRID[0], _FALLBACK_GRID[1],
                [list(row) for row in _FALLBACK_GRID[2]])
        self._cells = _cells_by_value(self._grid)
        self._cell_px = max(16, int(cell_px))
        self._mag: list[float] = []
        self._peaks: dict[int, float] = {}
        # Per-sensor position as a (0..1, 0..1) fraction of the widget — real
        # coordinates when the geometry has them, cell centres otherwise. Drives
        # the motion trail (centroid path of the current touch).
        self._pos_frac = self._position_fractions(geometry)
        self._trail: list[tuple[float, float, float]] = []   # (fx, fy, t_ms)
        # Per-frame |z|-fractions of the CURRENT trail (when vec data streams):
        # z-dominated contact = vertical push, so the arrow is suppressed.
        self._trail_z: list[float] = []
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _position_fractions(self, geometry: SkinGeometry | None
                            ) -> dict[int, tuple[float, float]]:
        if (geometry is not None and geometry.sensors_mm
                and geometry.size_mm[0] and geometry.size_mm[1]):
            w, h = geometry.size_mm
            return {i: (x / w, y / h)
                    for i, (x, y) in enumerate(geometry.sensors_mm)}
        return {i: ((c + 0.5) / self._cols, (r + 0.5) / self._rows)
                for i, (r, c) in self._cells.items()}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, mag, vec=None) -> None:
        """Render the latest per-sensor magnitudes (µT, baseline-subtracted).

        ``vec`` is the message's optional per-sensor 3-axis deltas — when
        present, the trail's arrow only shows for lateral (x/y) drags, not for
        vertical pushes that happen to migrate sensors."""
        self._mag = [float(v) for v in mag]
        for i in self._cells:
            if i < len(self._mag):
                self._peaks[i] = max(self._peaks.get(i, 0.0), self._mag[i])
        # Slide/direction visuals are behind a master switch (temporarily off).
        if _tax.SLIDE_DETECTION_ENABLED:
            self._update_trail(vec)
        self.update()

    def _update_trail(self, vec=None) -> None:
        """Append the touch centroid (if any) and expire old trail points.

        A gap in contact longer than ``_TRAIL_BREAK_MS`` starts a NEW trail:
        the path (and its arrow) only ever describes one continuous touch, so
        two separate taps on different sensors cannot read as a slide."""
        now = time.monotonic() * 1000.0
        positions = [self._pos_frac.get(i, (0.5, 0.5))
                     for i in range(len(self._mag))]
        floor = float(self._threshold()) * _TRAIL_FLOOR_FRAC
        c = weighted_centroid(self._mag, positions, floor=floor)
        if c is not None:
            if self._trail and now - self._trail[-1][2] > _TRAIL_BREAK_MS:
                self._trail = []          # contact was released — new touch
                self._trail_z = []
            self._trail.append((c[0], c[1], now))
            z = frame_z_frac(self._mag, vec)
            if z is not None:
                self._trail_z.append(z)
        self._trail = [p for p in self._trail if now - p[2] <= _TRAIL_MS]

    def _trail_is_slide(self) -> bool:
        """True when the current trail behaves like a real slide.

        A finger drags in a roughly straight line; an inflating chamber shifts
        the magnet so the centroid wanders/circles between coupled sensors — so
        require the path to be reasonably STRAIGHT (net travel ÷ path length).
        With vec data the contact must also show lateral shear (not a vertical
        push). Same rules as :func:`src.ml.touch_motion.segment_direction`."""
        if len(self._trail) >= 3:
            pts = [(fx, fy) for fx, fy, _t in self._trail]
            if path_straightness(pts) < SLIDE_MIN_STRAIGHTNESS:
                return False             # wandered/circled — e.g. inflation
        if self._trail_z:
            mean_z = sum(self._trail_z) / len(self._trail_z)
            if mean_z >= SLIDE_MAX_Z_FRAC:
                return False
        return True

    def refresh(self) -> None:
        """Repaint against the current threshold (e.g. after the slider moved)."""
        self.update()

    def active_sensors(self) -> set[int]:
        """Indices whose latest magnitude reaches the current threshold."""
        thr = float(self._threshold())
        return {i for i in self._cells
                if i < len(self._mag) and self._mag[i] >= thr}

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        # Keep the skin's real aspect ratio (e.g. the Thymio 'D' is 134×120, a
        # touch wider than tall) so the outline isn't squashed into the grid's
        # cell squares; fall back to square cells when the size is unknown.
        h = self._rows * self._cell_px
        if self._size_mm and self._size_mm[1]:
            w = round(h * self._size_mm[0] / self._size_mm[1])
        else:
            w = self._cols * self._cell_px
        return QSize(max(int(w), self._cell_px), h)

    def paintEvent(self, _: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        outline = shape_path(self._shape, QRectF(0, 0, self.width(), self.height()))
        if outline is not None:
            p.setClipPath(outline)
        p.fillRect(self.rect(), _BG)

        active = self.active_sensors()
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF()))
        p.setFont(font)
        for idx, (r, c) in self._cells.items():
            rect = self._cell_rect(r, c)
            p.fillRect(rect, _ACTIVE if idx in active else _IDLE)
            p.setPen(_CELL_PEN)
            p.drawRect(rect)
            self._draw_label(p, rect, idx)

        self._paint_trail(p)

        if outline is not None:
            p.setClipping(False)
            p.setPen(QPen(_OUTLINE_PEN, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(outline)

    def _paint_trail(self, p: QPainter) -> None:
        """Fading centroid path of the current touch, with an arrowhead at the
        newest point once the touch has actually travelled (a slide)."""
        if len(self._trail) < 2:
            return
        now = time.monotonic() * 1000.0
        w, h = self.width(), self.height()
        pts = [QPointF(fx * w, fy * h) for fx, fy, _t in self._trail]
        for i in range(1, len(pts)):
            age = now - self._trail[i][2]
            colour = QColor(_TRAIL)
            colour.setAlpha(max(0, int(255 * (1.0 - age / _TRAIL_MS))))
            p.setPen(QPen(colour, 3, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(pts[i - 1], pts[i])

        start, end = pts[0], pts[-1]
        if hypot(end.x() - start.x(), end.y() - start.y()) < _ARROW_MIN_TRAVEL_PX:
            return
        if not self._trail_is_slide():
            return                        # vertical push / wander — no arrow
        # Point along the NET travel (start→end), not the last two samples, so
        # the head stays steady instead of jittering frame to frame.
        angle = atan2(end.y() - start.y(), end.x() - start.x())
        p.setPen(QPen(_TRAIL, 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        for side in (pi * 5 / 6, -pi * 5 / 6):
            p.drawLine(end, QPointF(
                end.x() + _ARROW_LEN_PX * cos(angle + side),
                end.y() + _ARROW_LEN_PX * sin(angle + side)))

    # ------------------------------------------------------------------
    # Painting helpers
    # ------------------------------------------------------------------

    def _draw_label(self, p: QPainter, rect: QRect, idx: int) -> None:
        mag = self._mag[idx] if idx < len(self._mag) else 0.0
        text = f"S{idx}\n{mag:.0f} µT"
        p.setPen(_LABEL)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _cell_rect(self, r: int, c: int) -> QRect:
        w, h = self.width(), self.height()
        cw, ch = w / max(1, self._cols), h / max(1, self._rows)
        x, y = int(c * cw), int(r * ch)
        return QRect(x, y, int((c + 1) * cw) - x + 1, int((r + 1) * ch) - y + 1)

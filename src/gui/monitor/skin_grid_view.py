"""SkinGridView — read-only spatial view of a skin during activities.

Renders the skin's ``chamber_grid`` (rows × cols of local chamber indices)
as one merged region per chamber — adjacent cells with the same chamber
index are grouped into a single connected component, filled with the
chamber's palette colour at an opacity proportional to its current pressure,
and labelled once (``Cn / NN%``) in the centre of the component.

The widget supports **per-layer grid dimensions** (chamber and sensor
grids can be different resolutions, e.g. 3×3 chambers + 8×4 sensors) and
both **rectangular** and **round** skin shapes. For round skins, cells
whose centroid falls outside the inscribed circle are masked out (drawn
muted), matching the editor's behaviour.

The widget is read-only — it does not handle clicks; activities still use
ChamberWidget for inflate / deflate / touch controls. SkinGridView is purely
to give the user a visual map of where each chamber sits and how full it is.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from src.gui.skin_grid_editor import CHAMBER_PALETTE
from src.hardware.skin import Skin

logger = logging.getLogger(__name__)

_EMPTY_BG         = QColor("#fdfefe")
_CELL_PEN         = QColor("#bdc3c7")
_LABEL_PEN        = QColor("#1c2833")
_REGION_PEN       = QColor("#566573")  # outline of each chamber region
_SENSOR_PULSE  = QColor("#f1c40f")  # active sensor highlight (yellow)
_CHAMBER_PULSE = QColor("#3498db")  # touched chamber highlight (blue)

# Pulse decay for active sensors / chambers: a touch fades from full alpha to
# zero over this window so brief contacts still flash visibly.
_TOUCH_FADE_MS    = 400
_TOUCH_TICK_MS    = 40        # repaint cadence while a pulse is alive

# 4-connectivity offsets used when grouping cells into regions.
_NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1))


class SkinGridView(QWidget):
    """Spatial view of a skin's chambers + current pressure levels."""

    _magnet_msg = Signal(object)  # thread-safe bridge for on_magnet callbacks

    def __init__(self, skin: Skin, cell_px: int = 28,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._skin = skin
        self._cell_px = max(8, int(cell_px))
        self._shape = getattr(skin, "shape", "rect")

        self._chamber_cols, self._chamber_rows, self._chamber_grid = \
            self._read_chamber_layout(skin)
        self._sensor_cols, self._sensor_rows, self._sensor_grid = \
            self._read_sensor_layout(
                skin, self._chamber_cols, self._chamber_rows,
            )

        # Connected chamber regions (computed once; chamber_grid is static).
        self._regions: list[tuple[int, list[tuple[int, int]]]] = \
            self._find_regions(self._chamber_grid,
                               self._chamber_rows, self._chamber_cols)
        self._chamber_cells: dict[int, list[tuple[int, int]]] = \
            self._cells_by_value(self._chamber_grid,
                                 self._chamber_rows, self._chamber_cols)
        self._sensor_cells: dict[int, list[tuple[int, int]]] = \
            self._cells_by_value(self._sensor_grid,
                                 self._sensor_rows, self._sensor_cols)

        self._active_sensors:  dict[int, int] = {}
        self._active_chambers: dict[int, int] = {}
        self._held_sensors:    set[int] = set()
        self._tick = QTimer(self)
        self._tick.setInterval(_TOUCH_TICK_MS)
        self._tick.timeout.connect(self._decay_pulses)

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        ctrl = getattr(skin, "touch_controller", None)
        if ctrl is not None and hasattr(ctrl, "on_magnet"):
            # Force QueuedConnection: gateway uses a Python threading.Thread (not
            # QThread), so AutoConnection delivers the signal synchronously in the
            # wrong thread.  QueuedConnection always routes through the event loop.
            self._magnet_msg.connect(
                self._on_magnet_msg, Qt.ConnectionType.QueuedConnection
            )
            ctrl.on_magnet(self._magnet_msg.emit)

        self._sensor_buttons: dict[int, QPushButton] = {}
        # T-buttons only make sense in simulation — on real hardware the
        # magnet sensor board fires the touches, so an in-app simulate button would
        # be misleading. Detect sim mode via the controller class.
        if self._is_simulation():
            self._build_sensor_buttons()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pulse_sensor(self, sensor_idx: int) -> None:
        """Flash the yellow sensor highlight (real hardware touch or sim)."""
        if sensor_idx not in self._sensor_cells:
            return
        self._active_sensors[sensor_idx] = 255
        if not self._tick.isActive():
            self._tick.start()
        self.update()

    def refresh(self) -> None:
        self.update()

    def pulse_chamber(self, chamber_idx: int) -> None:
        if chamber_idx not in self._chamber_cells:
            return
        self._active_chambers[chamber_idx] = 255
        if not self._tick.isActive():
            self._tick.start()
        self.update()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        # Pixel size accommodates the LARGER of the two layers on each axis
        # so neither gets squashed.
        w_cols = max(self._chamber_cols, self._sensor_cols)
        w_rows = max(self._chamber_rows, self._sensor_rows)
        return QSize(w_cols * self._cell_px, w_rows * self._cell_px)

    def resizeEvent(self, ev):
        """Sensor T buttons are positioned in pixel coords that depend on
        the widget's actual size — reposition them whenever Qt lays us out
        (or resizes us). Without this they stay at the (0,0)-ish positions
        computed during ``__init__`` when the widget had no real size yet."""
        super().resizeEvent(ev)
        self._reposition_sensor_buttons()

    def paintEvent(self, _: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Round skins clip every layer (background, regions, pulses) to the
        # inscribed ellipse. Cells that straddle the border are cut by the
        # circle instead of being individually blanked.
        if self._shape == "round":
            clip = QPainterPath()
            clip.addEllipse(QRectF(0, 0, self.width(), self.height()))
            p.setClipPath(clip)

        self._paint_empty_grid(p)
        for ch_idx, cells in self._regions:
            self._paint_region(p, ch_idx, cells)
        if self._active_sensors or self._active_chambers:
            self._paint_pulse_overlay(p)

        # Always draw the circle outline last (unclipped) so the boundary is
        # visible regardless of which cells fall inside it.
        if self._shape == "round":
            p.setClipping(False)
            p.setPen(QPen(_REGION_PEN, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(0, 0, self.width(), self.height()))

    # ------------------------------------------------------------------
    # Painting helpers
    # ------------------------------------------------------------------

    def _paint_empty_grid(self, p: QPainter) -> None:
        """Background + thin borders for empty CHAMBER cells (the primary
        layer in activities). Clipping (round skins) is set up in
        ``paintEvent``; this method just paints the full rectangular grid
        and lets Qt cut whatever falls outside the circle."""
        p.fillRect(self.rect(), _EMPTY_BG)
        p.setPen(_CELL_PEN)
        for r in range(self._chamber_rows):
            for c in range(self._chamber_cols):
                if self._chamber_grid[r][c] < 0:
                    p.drawRect(self._cell_rect(r, c,
                                               self._chamber_cols,
                                               self._chamber_rows))

    def _paint_region(self, p: QPainter, ch_idx: int,
                      cells: list[tuple[int, int]]) -> None:
        """Fill the region, draw its outline once, and place a single label."""
        pressure = self._pressure_for(ch_idx)
        fill = self._tint(ch_idx, pressure)

        for (r, c) in cells:
            p.fillRect(self._cell_rect(r, c,
                                       self._chamber_cols, self._chamber_rows),
                       fill)

        p.setPen(QPen(_REGION_PEN, 2))
        self._draw_region_outline(p, cells,
                                  self._chamber_cols, self._chamber_rows)

        # Label centred on the region's bounding box.
        ar, _ = self._centroid_cell(cells)
        min_r = min(r for r, _ in cells)
        max_r = max(r for r, _ in cells)
        min_c = min(c for _, c in cells)
        max_c = max(c for _, c in cells)
        rect_tl = self._cell_rect(min_r, min_c,
                                  self._chamber_cols, self._chamber_rows)
        rect_br = self._cell_rect(max_r, max_c,
                                  self._chamber_cols, self._chamber_rows)
        cw = rect_br.width()
        ch = rect_br.height()
        label_x = rect_tl.x()
        label_w = rect_br.x() + rect_br.width() - rect_tl.x()
        label_y = self._cell_rect(ar, min_c,
                                  self._chamber_cols, self._chamber_rows).y()
        label_h = ch
        if min_r <= ar - 1:
            label_y -= ch // 2
            label_h += ch // 2
        if max_r >= ar + 1:
            label_h += ch // 2
        label_rect = QRect(label_x, label_y, label_w, label_h)

        font = p.font()
        font.setPointSizeF(max(7.0, min(cw, ch) * 0.32))
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(_LABEL_PEN)
        p.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextDontClip,
            f"C{ch_idx}  {pressure}%",
        )

    # ------------------------------------------------------------------
    # Sensor T buttons (simulation aid)
    # ------------------------------------------------------------------

    # Small fixed-size simulate button — bigger sensor regions previously
    # made the T-button huge enough to swallow the skin.
    _SENSOR_BTN_SIZE = 24

    def _build_sensor_buttons(self) -> None:
        """Create one small 'T' button per sensor. Positions are set in
        ``_reposition_sensor_buttons`` (called from ``resizeEvent``) so
        they pick up the widget's real pixel size."""
        side = self._SENSOR_BTN_SIZE
        for sensor_idx in self._sensor_cells:
            btn = QPushButton(f"T{sensor_idx}", self)
            btn.setFixedSize(side, side)
            btn.setStyleSheet(
                "QPushButton { background: rgba(241, 196, 15, 220);"
                " border: 1px solid #b7950b; border-radius: 4px;"
                " font-size: 9px; font-weight: bold; color: #1c2833; }"
                "QPushButton:pressed { background: rgba(241, 196, 15, 255); }"
            )
            btn.setToolTip(
                f"Simulate sensor {sensor_idx} touch — same code path as a "
                "real magnet sensor `act` event."
            )
            btn.pressed.connect(lambda idx=sensor_idx: self._simulate_sensor_press(idx))
            btn.released.connect(lambda idx=sensor_idx: self._simulate_sensor_release(idx))
            btn.show()
            self._sensor_buttons[sensor_idx] = btn
        self._reposition_sensor_buttons()

    def _reposition_sensor_buttons(self) -> None:
        """Re-centre each T button on its sensor region using the widget's
        current pixel size. Safe to call before / during layout — if the
        widget has no size yet, positions get set to 0 and corrected on
        the next ``resizeEvent``."""
        side = self._SENSOR_BTN_SIZE
        for sensor_idx, btn in self._sensor_buttons.items():
            cells = self._sensor_cells.get(sensor_idx)
            if not cells:
                continue
            cx, cy = self._region_pixel_centre(cells,
                                               self._sensor_cols,
                                               self._sensor_rows)
            btn.move(cx - side // 2, cy - side // 2)

    def _region_pixel_centre(self, cells: list[tuple[int, int]],
                             cols: int, rows: int) -> tuple[int, int]:
        """Centre of the bounding box of a region, in widget pixels."""
        min_r = min(r for r, _ in cells)
        max_r = max(r for r, _ in cells)
        min_c = min(c for _, c in cells)
        max_c = max(c for _, c in cells)
        tl = self._cell_rect(min_r, min_c, cols, rows)
        br = self._cell_rect(max_r, max_c, cols, rows)
        return ((tl.x() + br.x() + br.width()) // 2,
                (tl.y() + br.y() + br.height()) // 2)

    def _simulate_sensor_press(self, sensor_idx: int) -> None:
        """T button pressed — light the sensor yellow and broadcast the new set
        of held sensors as an magnet sensor event. The running activity decides whether
        (and how) to drive a chamber, exactly as for a real hardware touch."""
        self._held_sensors.add(sensor_idx)
        self._active_sensors[sensor_idx] = 255
        if not self._tick.isActive():
            self._tick.start()
        self.update()
        self._fire_magnet_act()

    def _simulate_sensor_release(self, sensor_idx: int) -> None:
        """T button released — drop the sensor from the held set and broadcast
        the updated set. The activity sees the sensor leave ``act`` (the
        release) and starts its deflate countdown. Yellow starts fading."""
        self._held_sensors.discard(sensor_idx)
        self._fire_magnet_act()

    def _fire_magnet_act(self) -> None:
        """Broadcast the current held-sensor set as an ``magnet`` event on the
        skin's touch controller — a ``SimulatedMagnetSensor`` in simulation, a real magnet sensor
        ``ESP32Controller`` on hardware. The activity reacts to the same
        ``on_magnet`` event either way, so behaviour is identical when the real
        board is plugged in. Falls back to the local visual handler if there is
        no touch controller (visual-only skin)."""
        data: dict[str, Any] = {"act": sorted(self._held_sensors)}
        source = (self._skin.touch or {}).get("node_mac")
        if source:
            data["source"] = source
        ctrl = getattr(self._skin, "touch_controller", None)
        fire = getattr(ctrl, "fire_magnet", None) if ctrl is not None else None
        if fire is not None:
            fire(data)
        else:
            self._on_magnet_msg(data)

    def _is_simulation(self) -> bool:
        """True when the skin is backed by simulated hardware (the session was
        launched with ``simulation_mode`` on) — a SimulatedController for the
        chambers or a SimulatedMagnetSensor for touch. Gates the T-button input."""
        from src.hardware.simulated_controller import SimulatedController
        from src.hardware.simulated_magnet_sensor import SimulatedMagnetSensor
        sim_types = (SimulatedController, SimulatedMagnetSensor)
        return (isinstance(getattr(self._skin, "_ctrl", None), sim_types)
                or isinstance(getattr(self._skin, "touch_controller", None), sim_types))

    # ------------------------------------------------------------------
    # Touch overlay
    # ------------------------------------------------------------------

    def _paint_pulse_overlay(self, p: QPainter) -> None:
        for sensor_idx, alpha in self._active_sensors.items():
            cells = self._sensor_cells.get(sensor_idx)
            if cells:
                self._stroke_pulse(p, cells, _SENSOR_PULSE, alpha,
                                   self._sensor_cols, self._sensor_rows)
        for chamber_idx, alpha in self._active_chambers.items():
            cells = self._chamber_cells.get(chamber_idx)
            if cells:
                self._stroke_pulse(p, cells, _CHAMBER_PULSE, alpha,
                                   self._chamber_cols, self._chamber_rows)

    def _stroke_pulse(self, p: QPainter, cells: list[tuple[int, int]],
                      base: QColor, alpha: int,
                      layer_cols: int, layer_rows: int) -> None:
        colour = QColor(base)
        colour.setAlpha(alpha)
        p.setPen(QPen(colour, 4))
        self._draw_region_outline(p, cells, layer_cols, layer_rows, inset=2)

    def _draw_region_outline(self, p: QPainter,
                             cells: list[tuple[int, int]],
                             layer_cols: int, layer_rows: int,
                             inset: int = 0) -> None:
        """Stroke each external edge of the region once. ``inset`` shrinks
        the outline by that many pixels so pulse outlines sit inside the
        region outline rather than overlapping it."""
        cell_set = set(cells)
        for (r, c) in cells:
            rect = self._cell_rect(r, c, layer_cols, layer_rows)
            x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
            if (r - 1, c) not in cell_set:
                p.drawLine(x + inset, y + inset,
                           x + w - inset, y + inset)
            if (r + 1, c) not in cell_set:
                p.drawLine(x + inset, y + h - inset,
                           x + w - inset, y + h - inset)
            if (r, c - 1) not in cell_set:
                p.drawLine(x + inset, y + inset,
                           x + inset, y + h - inset)
            if (r, c + 1) not in cell_set:
                p.drawLine(x + w - inset, y + inset,
                           x + w - inset, y + h - inset)

    def _on_magnet_msg(self, data: dict[str, Any]) -> None:
        active = data.get("act") or []
        if not isinstance(active, list):
            return
        changed = False
        for raw in active:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            self._active_sensors[idx] = 255
            changed = True
        if changed:
            if not self._tick.isActive():
                self._tick.start()
            self.update()

    def _decay_pulses(self) -> None:
        step = max(1, int(255 * (_TOUCH_TICK_MS / _TOUCH_FADE_MS)))
        # Held sensors stay at full brightness; only released sensors decay.
        for idx in self._held_sensors:
            self._active_sensors[idx] = 255
        self._fade_map(self._active_sensors, step, skip=self._held_sensors)
        self._fade_map(self._active_chambers, step)
        if not self._active_sensors and not self._active_chambers:
            self._tick.stop()
        self.update()

    @staticmethod
    def _fade_map(pulses: dict[int, int], step: int,
                  skip: set[int] | None = None) -> None:
        for k in list(pulses.keys()):
            if skip and k in skip:
                continue
            if pulses[k] - step <= 0:
                del pulses[k]
            else:
                pulses[k] -= step

    # ------------------------------------------------------------------
    # Geometry / data helpers
    # ------------------------------------------------------------------

    def _cell_rect(self, r: int, c: int,
                   cols: int, rows: int) -> QRect:
        """Pixel rect for the (r, c) cell of a grid sized ``cols × rows``,
        proportionally filling the widget."""
        w, h = self.width(), self.height()
        cw = w / max(1, cols)
        ch = h / max(1, rows)
        x = int(c * cw)
        y = int(r * ch)
        # +1 to overlap neighbours by one pixel and avoid 1-pixel gaps from
        # integer truncation.
        return QRect(x, y,
                     int((c + 1) * cw) - x + 1,
                     int((r + 1) * ch) - y + 1)

    def _tint(self, ch_idx: int, pressure: int) -> QColor:
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
    def _cells_by_value(grid: list[list[int]], rows: int, cols: int
                        ) -> dict[int, list[tuple[int, int]]]:
        out: dict[int, list[tuple[int, int]]] = {}
        for r in range(rows):
            for c in range(cols):
                v = grid[r][c]
                if v >= 0:
                    out.setdefault(v, []).append((r, c))
        return out

    @staticmethod
    def _read_chamber_layout(skin: Skin
                             ) -> tuple[int, int, list[list[int]]]:
        grid_cfg = skin.grid or {}
        cols = max(1, int(grid_cfg.get("cols", 0)))
        rows = max(1, int(grid_cfg.get("rows", 0)))
        raw = skin.chamber_grid or []
        if raw:
            rows = max(rows, len(raw))
            cols = max(cols, max((len(row) for row in raw), default=0))
        return cols, rows, _normalise_grid(raw, rows, cols)

    @staticmethod
    def _read_sensor_layout(skin: Skin, fallback_cols: int, fallback_rows: int
                            ) -> tuple[int, int, list[list[int]]]:
        """Sensor dims come from ``skin.touch.grid``, falling back to the
        chamber dims for legacy skins that share a single grid."""
        touch = skin.touch or {}
        grid_cfg = touch.get("grid") or {}
        cols = max(1, int(grid_cfg.get("cols", fallback_cols)))
        rows = max(1, int(grid_cfg.get("rows", fallback_rows)))
        raw = touch.get("sensor_grid") or []
        if raw:
            rows = max(rows, len(raw))
            cols = max(cols, max((len(row) for row in raw), default=0))
        return cols, rows, _normalise_grid(raw, rows, cols)


def _normalise_grid(raw: list[list[int]] | None,
                    rows: int, cols: int) -> list[list[int]]:
    grid = [[-1] * cols for _ in range(rows)]
    if not raw:
        return grid
    for r in range(min(rows, len(raw))):
        row = raw[r]
        for c in range(min(cols, len(row))):
            grid[r][c] = int(row[c])
    return grid

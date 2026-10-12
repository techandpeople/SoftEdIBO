"""TimelineCanvas - a session drawn on a time axis.

A custom-painted widget, placed in ``session_timeline_view.ui`` as a promoted
widget. It paints the lanes of a :class:`src.data.timeline.Timeline`: spans as
bars (phases, touches) and instants as ticks (robot actions, inputs, observer
codes). Scroll the wheel to zoom around the cursor, drag to pan, hover an item
to read what it is.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from src.data import timeline as tl

_LABEL_WIDTH = 150
_AXIS_HEIGHT = 22
_LANE_HEIGHT = 26
_MIN_BAR_PX = 2.0
_HIT_PX = 4.0
_MIN_VISIBLE_MS = 500.0
# Preferred distance between two axis ticks, and the step values to pick from.
_TICK_PX = 90
_TICK_STEPS_MS = (100, 200, 500, 1000, 2000, 5000, 10_000, 30_000, 60_000,
                  120_000, 300_000, 600_000)

_KIND_COLORS = {
    tl.KIND_TOUCH: QColor("#2980b9"),
    tl.KIND_ROBOT: QColor("#8e44ad"),
    tl.KIND_RESPONSE: QColor("#b39ddb"),
    tl.KIND_INPUT: QColor("#d35400"),
    tl.KIND_OBSERVER: QColor("#16a085"),
    tl.KIND_MARKER: QColor("#c0392b"),
    tl.KIND_SESSION: QColor("#7f8c8d"),
}
# Phases alternate through these so neighbours are told apart.
_PHASE_COLORS = (QColor("#f9e79f"), QColor("#aed6f1"), QColor("#a9dfbf"),
                 QColor("#f5b7b1"), QColor("#d7bde2"), QColor("#fad7a0"))


class TimelineCanvas(QWidget):
    """Paints a :class:`Timeline` with zoom, pan and hover descriptions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timeline = tl.Timeline(0.0)
        self._view_start_ms = 0.0
        self._view_span_ms = 1000.0
        self._drag_from: QPoint | None = None
        self._phase_colors: dict[str, QColor] = {}
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        self._fit_height()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_timeline(self, timeline: tl.Timeline) -> None:
        """Show ``timeline``, zoomed out to its whole length."""
        self._timeline = timeline
        self._phase_colors = {}
        for lane in timeline.lanes:
            for item in lane.items:
                if item.kind == tl.KIND_PHASE and item.group not in self._phase_colors:
                    self._phase_colors[item.group] = _PHASE_COLORS[
                        len(self._phase_colors) % len(_PHASE_COLORS)]
        self._fit_height()
        self.fit()

    def fit(self) -> None:
        """Zoom out to show the whole session."""
        self._view_start_ms = 0.0
        self._view_span_ms = max(_MIN_VISIBLE_MS, self._timeline.duration_ms)
        self.update()

    def zoom(self, factor: float, around_px: float | None = None) -> None:
        """Zoom by ``factor`` (>1 = in), keeping the time under ``around_px``
        (default: the middle) where it is."""
        width = self._plot_width()
        if width <= 0:
            return
        pivot_px = width / 2.0 if around_px is None else around_px - _LABEL_WIDTH
        pivot_ms = self._view_start_ms + pivot_px / width * self._view_span_ms
        total = max(_MIN_VISIBLE_MS, self._timeline.duration_ms)
        span = min(total, max(_MIN_VISIBLE_MS, self._view_span_ms / factor))
        self._view_span_ms = span
        self._set_start(pivot_ms - pivot_px / width * span)

    def visible_range_ms(self) -> tuple[float, float]:
        """The time window currently shown: ``(start, end)``."""
        return self._view_start_ms, self._view_start_ms + self._view_span_ms

    def describe_at(self, pos: QPoint) -> str:
        """What is drawn under ``pos`` ("" when nothing)."""
        lane_index = (pos.y() - _AXIS_HEIGHT) // _LANE_HEIGHT
        if pos.y() < _AXIS_HEIGHT or not 0 <= lane_index < len(self._timeline.lanes):
            return ""
        hits = [item for item in self._timeline.lanes[lane_index].items
                if self._x(item.start_ms) - _HIT_PX <= pos.x()
                <= max(self._x(item.end_ms), self._x(item.start_ms) + _MIN_BAR_PX)
                + _HIT_PX]
        return "\n".join(
            f"{item.start_ms / 1000.0:.2f} s  {item.label}" for item in hits[:6])

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _fit_height(self) -> None:
        lanes = max(1, len(self._timeline.lanes))
        self.setMinimumHeight(_AXIS_HEIGHT + lanes * _LANE_HEIGHT + 4)

    def _plot_width(self) -> float:
        return float(max(1, self.width() - _LABEL_WIDTH))

    def _x(self, t_ms: float) -> float:
        return _LABEL_WIDTH + ((t_ms - self._view_start_ms)
                               / self._view_span_ms * self._plot_width())

    def _set_start(self, start_ms: float) -> None:
        total = max(_MIN_VISIBLE_MS, self._timeline.duration_ms)
        self._view_start_ms = min(max(0.0, start_ms),
                                  max(0.0, total - self._view_span_ms))
        self.update()

    def _tick_step_ms(self) -> float:
        ms_per_px = self._view_span_ms / self._plot_width()
        wanted = ms_per_px * _TICK_PX
        return next((step for step in _TICK_STEPS_MS if step >= wanted),
                    _TICK_STEPS_MS[-1])

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.base())
        if not self._timeline.lanes:
            painter.setPen(palette.placeholderText().color())
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Select a session to see its timeline.")
            return
        self._paint_axis(painter)
        for index, lane in enumerate(self._timeline.lanes):
            self._paint_lane(painter, index, lane)

    def _paint_axis(self, painter: QPainter) -> None:
        palette = self.palette()
        step = self._tick_step_ms()
        tick = (self._view_start_ms // step) * step
        end = self._view_start_ms + self._view_span_ms
        grid = QPen(palette.mid().color(), 1, Qt.PenStyle.DotLine)
        while tick <= end:
            x = self._x(tick)
            if x >= _LABEL_WIDTH:
                painter.setPen(grid)
                painter.drawLine(int(x), _AXIS_HEIGHT, int(x), self.height())
                painter.setPen(palette.text().color())
                seconds = tick / 1000.0
                text = f"{seconds:.1f} s" if step < 1000 else f"{seconds:.0f} s"
                painter.drawText(int(x) + 3, _AXIS_HEIGHT - 6, text)
            tick += step

    def _paint_lane(self, painter: QPainter, index: int,
                    lane: tl.TimelineLane) -> None:
        palette = self.palette()
        top = _AXIS_HEIGHT + index * _LANE_HEIGHT
        if index % 2:
            painter.fillRect(0, top, self.width(), _LANE_HEIGHT,
                             palette.alternateBase())
        painter.save()
        painter.setClipRect(_LABEL_WIDTH, top, self.width() - _LABEL_WIDTH,
                            _LANE_HEIGHT)
        for item in lane.items:
            self._paint_item(painter, top, item)
        painter.restore()
        painter.setPen(palette.text().color())
        painter.drawText(
            QRectF(6, top, _LABEL_WIDTH - 10, _LANE_HEIGHT),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            painter.fontMetrics().elidedText(
                lane.label, Qt.TextElideMode.ElideMiddle, _LABEL_WIDTH - 12))

    def _paint_item(self, painter: QPainter, top: int,
                    item: tl.TimelineItem) -> None:
        x0 = self._x(item.start_ms)
        if item.kind == tl.KIND_PHASE:
            rect = QRectF(x0, top + 3, self._x(item.end_ms) - x0, _LANE_HEIGHT - 6)
            painter.fillRect(rect, self._phase_colors.get(item.group,
                                                          _PHASE_COLORS[0]))
            painter.setPen(QColor("#2c3e50"))
            painter.drawText(rect.adjusted(4, 0, -2, 0),
                             Qt.AlignmentFlag.AlignVCenter, item.label)
            return
        color = _KIND_COLORS.get(item.kind, QColor("#7f8c8d"))
        if item.is_point:
            painter.setPen(QPen(color, 2))
            painter.drawLine(int(x0), top + 5, int(x0), top + _LANE_HEIGHT - 5)
            return
        width = max(_MIN_BAR_PX, self._x(item.end_ms) - x0)
        painter.fillRect(QRectF(x0, top + 6, width, _LANE_HEIGHT - 12), color)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.angleDelta().y() == 0:
            return
        self.zoom(1.25 if event.angleDelta().y() > 0 else 0.8,
                  event.position().x())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position().toPoint()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        self._drag_from = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag_from is not None:
            moved_px = pos.x() - self._drag_from.x()
            self._drag_from = pos
            self._set_start(self._view_start_ms
                            - moved_px / self._plot_width() * self._view_span_ms)
            return
        text = self.describe_at(pos)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()

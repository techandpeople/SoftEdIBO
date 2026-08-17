"""LED ring tester - a circle of N clickable LEDs for testing a WS2812 ring.

Basic colours with pattern selection (solid, blink, pulse, comet) and a segment
count (whole, halves, quarters). Each action calls back into the host (the Test
Actuators dialog), which forwards a ``set_led`` / ``set_led_halves`` command to the
node over the gateway. Every change cross-fades; the "Smooth transition" box sets
how long. The on-screen ring mirrors the firmware so the preview matches hardware.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QMouseEvent, QPainter, QPaintEvent,
                           QPen, QPolygonF)
from PySide6.QtWidgets import QGroupBox, QPushButton, QSizePolicy, QWidget

from src.gui.led_animation import (AnimationPattern, comet_pixels, lerp_color,
                                   pattern_scale)
from src.gui.ui_led_ring_tester import Ui_LedRingTester

_OFF_COLOR = QColor("#202020")

# Basic LED colors. Purple matches the behaviour editor's default (#8e44ad) so
# the same colour is selectable from the test panel and the activity editor.
_COLORS = {
    "Red":    "#ff0000",
    "Green":  "#00ff00",
    "Blue":   "#0000ff",
    "White":  "#ffffff",
    "Yellow": "#ffff00",
    "Cyan":   "#00ffff",
    "Purple": "#8e44ad",
}

_PATTERNS = ["solid", "blink", "pulse", "comet"]

# Segment presets: label -> arc count. Whole = one colour; halves/quarters split
# the ring into 2/4 equal arcs (and, with the comet pattern, give 2/4 comets).
_SEGMENTS = {"Whole": 1, "Halves": 2, "Quarters": 4}

# Fixed animation periods for the test panel (it has no period control). Plenty
# slow to read the motion while verifying wiring.
_PULSE_PERIOD_MS = 1000
_COMET_PERIOD_MS = 2000

# Repaint cadence for the on-screen preview (~30 fps) while animating/fading.
_PREVIEW_MS = 33

# On-screen ring size: shrink the drawn ring to ~75% of the widget so the preview
# stays compact next to the controls.
_RING_SCALE = 0.75


def _scaled(c: QColor, scale: float) -> QColor:
    """A colour at ``scale`` brightness (0..1)."""
    return QColor(round(c.red() * scale), round(c.green() * scale),
                  round(c.blue() * scale))


class LedRingWidget(QWidget):
    """Draws N LEDs on a circle as a live preview, with a draggable angle handle.

    Renders the firmware's looks faithfully: solid/blink/pulse scale brightness,
    comet sweeps one rotating head per segment colour, split arcs are rotated by
    an angle, and every change cross-fades. The look's split/comet angle is set by
    dragging the handle around the ring (an arc slider) - ``angleChanged`` fires
    live while dragging, ``angleReleased`` once when the drag ends.
    """

    angleChanged = Signal(float)    # live during a drag (degrees 0..360)
    angleReleased = Signal(float)   # once when the drag ends

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = max(1, count)
        self._seg_colors = [QColor(_OFF_COLOR)]
        self._angle_deg = 0.0
        self._base = self._compute_base()
        self._pattern = AnimationPattern.SOLID
        self._period_ms = _PULSE_PERIOD_MS
        self._fade_from = list(self._base)
        self._fade_ms = 0
        self._anim_t0 = time.monotonic()
        self._fade_t0 = time.monotonic()
        self._dragging = False
        self.setMinimumSize(190, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

    @property
    def count(self) -> int:
        return self._count

    @property
    def angle(self) -> float:
        return self._angle_deg

    # -- public API -----------------------------------------------------------

    def set_look(self, seg_colors: list[QColor], pattern: AnimationPattern,
                 period_ms: int, fade_ms: int, angle_deg: float) -> None:
        """Show a whole-ring look, cross-fading from what is on screen now.

        ``seg_colors`` are the arc colours (1 = whole ring, 2 = halves, 4 =
        quarters); ``angle_deg`` rotates the split/comet around the ring."""
        now = time.monotonic()
        self._fade_from = self._displayed(now)
        self._seg_colors = [QColor(c) for c in seg_colors] or [QColor(_OFF_COLOR)]
        self._angle_deg = angle_deg % 360.0
        self._base = self._compute_base()
        self._pattern = pattern
        self._period_ms = max(1, period_ms)
        self._fade_ms = max(0, fade_ms)
        self._anim_t0 = now
        self._fade_t0 = now
        self._kick()

    def set_angle(self, angle_deg: float) -> None:
        """Set the split/comet angle without emitting (e.g. to the saved value)."""
        self._angle_deg = float(angle_deg) % 360.0
        self._base = self._compute_base()
        self.update()

    # -- rendering ------------------------------------------------------------

    def _compute_base(self) -> list[QColor]:
        """Per-pixel target colours for the static patterns: the arc colours laid
        out as equal contiguous arcs, rotated by the angle (mirrors the firmware
        ``setSegments``)."""
        n, k = self._count, len(self._seg_colors)
        off = round(self._angle_deg / 360.0 * n) % n
        base = []
        for i in range(n):
            idx = (i - off) % n
            seg = min(idx * k // n, k - 1)
            base.append(QColor(self._seg_colors[seg]))
        return base

    def _animated(self, now: float) -> list[QColor]:
        """Per-pixel colour for the current pattern at time ``now`` (pre-fade)."""
        if self._pattern == AnimationPattern.COMET:
            head = ((now - self._anim_t0) * 1000.0 / self._period_ms
                    + self._angle_deg / 360.0)
            return comet_pixels(self._seg_colors, self._count, head)
        frac = ((now - self._anim_t0) * 1000.0 / self._period_ms)
        scale = pattern_scale(self._pattern, frac)
        return [_scaled(c, scale) for c in self._base]

    def _displayed(self, now: float) -> list[QColor]:
        """Per-pixel colour actually on screen (pattern + cross-fade)."""
        anim = self._animated(now)
        if self._fade_ms <= 0:
            return anim
        ft = min(1.0, (now - self._fade_t0) * 1000.0 / self._fade_ms)
        n = min(len(self._fade_from), len(anim))
        return [lerp_color(self._fade_from[i], anim[i], ft) for i in range(n)]

    def _animating(self, now: float) -> bool:
        fading = self._fade_ms > 0 and (now - self._fade_t0) * 1000.0 < self._fade_ms
        return fading or self._pattern != AnimationPattern.SOLID

    def _kick(self) -> None:
        """Repaint now and keep ticking while there is motion to show."""
        self.update()
        if not self._timer.isActive():
            self._timer.start(_PREVIEW_MS)

    def _on_tick(self) -> None:
        self.update()
        if not self._animating(time.monotonic()):
            self._timer.stop()

    def _center_radius(self) -> tuple[float, float, float, float]:
        """Ring centre (cx, cy), LED-circle radius and per-LED radius."""
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        ring_r = (min(w, h) / 2.0 - 18.0) * _RING_SCALE
        led_r = max(4.0, min(14.0, math.pi * ring_r / self._count / 1.4))
        return cx, cy, ring_r, led_r

    def _geometry(self) -> tuple[list[QPointF], float]:
        cx, cy, ring_r, led_r = self._center_radius()
        pts = []
        for i in range(self._count):
            a = 2.0 * math.pi * i / self._count - math.pi / 2.0
            pts.append(QPointF(cx + ring_r * math.cos(a), cy + ring_r * math.sin(a)))
        return pts, led_r

    def _handle_center(self) -> QPointF:
        """Where the angle handle sits (just outside the ring, at the angle)."""
        cx, cy, ring_r, led_r = self._center_radius()
        a = 2.0 * math.pi * (self._angle_deg / 360.0) - math.pi / 2.0
        r = ring_r + led_r + 7.0
        return QPointF(cx + r * math.cos(a), cy + r * math.sin(a))

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts, led_r = self._geometry()
        colors = self._displayed(time.monotonic())
        for i in range(self._count):
            painter.setBrush(colors[i] if i < len(colors) else _OFF_COLOR)
            painter.setPen(QPen(QColor("#7f8c8d"), 1))
            painter.drawEllipse(pts[i], led_r, led_r)
        self._draw_handle(painter)

    def _draw_handle(self, painter: QPainter) -> None:
        """A triangular grip pointing inwards at the current angle - drag it to
        rotate the split/comet around the ring."""
        c = self._handle_center()
        cx, cy, _ring_r, _led_r = self._center_radius()
        ang = math.atan2(c.y() - cy, c.x() - cx)          # outward direction
        size = 7.0
        tip = QPointF(c.x() - size * math.cos(ang), c.y() - size * math.sin(ang))
        left = QPointF(c.x() + size * math.cos(ang + 2.2),
                       c.y() + size * math.sin(ang + 2.2))
        right = QPointF(c.x() + size * math.cos(ang - 2.2),
                        c.y() + size * math.sin(ang - 2.2))
        painter.setBrush(QBrush(QColor("#ecf0f1" if self._dragging else "#bdc3c7")))
        painter.setPen(QPen(QColor("#2c3e50"), 1))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    # -- angle drag -----------------------------------------------------------

    def _angle_at(self, pos: QPointF) -> float:
        """Angle (deg, 0 at top, clockwise) of a point relative to the centre."""
        cx, cy, _ring_r, _led_r = self._center_radius()
        deg = math.degrees(math.atan2(pos.y() - cy, pos.x() - cx))
        return (deg + 90.0) % 360.0

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._dragging = True
        self._angle_deg = self._angle_at(event.position())
        self._base = self._compute_base()
        self.update()
        self.angleChanged.emit(self._angle_deg)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            return
        self._angle_deg = self._angle_at(event.position())
        self._base = self._compute_base()
        self.update()
        self.angleChanged.emit(self._angle_deg)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.update()
        self.angleReleased.emit(self._angle_deg)


class LedRingTester(QGroupBox, Ui_LedRingTester):
    """LED ring test: a preview ring + preset colour/pattern/segment buttons.

    Every colour/pattern/segment change applies live, so ``send_cb`` fires
    immediately (no separate "apply" step):

    ``send_cb(index, colors, pattern, fade_ms, angle)``:
      - index:   always None here (whole ring); kept for the host signature
      - colors:  list of "#RRGGBB" arc colours (len 1/2/4), or None to turn off
      - pattern: "solid" | "blink" | "pulse" | "comet"
      - fade_ms: cross-fade time for this change (from the panel's spin box)
      - angle:   0-360 deg rotation of the split/comet (from the ring's drag handle)
    """

    def __init__(self, count: int,
                 send_cb: Callable[[int | None, list[str] | None, str, int, float], None],
                 initial_angle: float = 0.0,
                 on_save_angle: Callable[[float], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._send = send_cb
        self._on_save_angle = on_save_angle
        # Clicked-colour queue: the last N clicks (N = segment count) are the arc
        # colours, oldest -> newest. One colour for the whole ring, two for halves,
        # four for quarters - the user builds the split by clicking colours in a row.
        self._color_queue: list[str] = ["#ff0000"]
        self._pattern = "solid"
        self._segments = 1
        self._angle = float(initial_angle)
        self._pattern_buttons: dict[str, QPushButton] = {}
        self._segment_buttons: dict[str, QPushButton] = {}

        # Colour buttons (data-driven) -> into the .ui's color_row. Each button is
        # tinted its own colour so the row reads as a palette.
        for name, hex_color in _COLORS.items():
            btn = QPushButton(name)
            btn.setStyleSheet(self._swatch_style(hex_color))
            btn.clicked.connect(lambda checked=False, c=hex_color: self._set_color(c))
            self.color_row.addWidget(btn)
        self.color_row.addStretch()

        # Pattern buttons (data-driven) -> into the .ui's pattern_row.
        for pattern in _PATTERNS:
            btn = QPushButton(pattern.capitalize())
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, p=pattern: self._set_pattern(p))
            self._pattern_buttons[pattern] = btn
            self.pattern_row.addWidget(btn)
        self._pattern_buttons["solid"].setChecked(True)
        self.pattern_row.addStretch()

        # Segment buttons (data-driven) -> into the .ui's segment_row.
        for name, n in _SEGMENTS.items():
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, k=n: self._set_segments(k))
            self._segment_buttons[name] = btn
            self.segment_row.addWidget(btn)
        self._segment_buttons["Whole"].setChecked(True)
        self.segment_row.addStretch()

        # Static control buttons live in the .ui.
        self.off_btn.clicked.connect(self._on_off)
        # "Save angle" persists the ring's mounting angle via the host callback;
        # hidden when the host doesn't support saving (e.g. no skin context).
        if self._on_save_angle is not None:
            self.save_angle_btn.clicked.connect(self._on_save_angle_clicked)
        else:
            self.save_angle_btn.setVisible(False)

        # Right side: LED ring - a live preview of the chosen look, with a
        # draggable handle to set the split/comet angle (count is a runtime arg so
        # it can't live in the .ui).
        self._ring = LedRingWidget(count)
        self._ring.set_angle(self._angle)                        # start at the saved angle
        self._ring.angleChanged.connect(self._on_angle_live)     # live preview
        self._ring.angleReleased.connect(self._on_angle_release)  # send on drop
        self.main_layout.addWidget(self._ring, 1)
        self.main_layout.setStretch(0, 1)
        self._update_hint()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _swatch_style(hex_color: str) -> str:
        """Button style that tints it its own colour, with readable text."""
        c = QColor(hex_color)
        fg = "#000000" if c.value() > 140 else "#ffffff"
        return (f"QPushButton {{ background-color: {hex_color}; color: {fg}; "
                f"padding: 3px 8px; border: 1px solid #555; border-radius: 3px; }}")

    def _fade_ms(self) -> int:
        return int(self.fade_spin.value())

    def _period_ms(self) -> int:
        if self._pattern == "comet":
            return _COMET_PERIOD_MS
        return _PULSE_PERIOD_MS

    def _arc_colors(self) -> list[str]:
        """The ``segments`` arc colours = the last ``segments`` clicked colours
        (oldest -> newest). Fewer clicks than needed cycle to fill the ring."""
        n = self._segments
        q = self._color_queue or ["#ff0000"]
        if len(q) >= n:
            return list(q[-n:])
        return [q[i % len(q)] for i in range(n)]

    def _update_hint(self) -> None:
        """Tell the user how many colours the current split needs."""
        if self._segments == 1:
            self.select_label.setText("Select color:")
        else:
            picked = " ".join(self._arc_colors())
            self.select_label.setText(
                f"Click {self._segments} colors in a row (arcs: {picked}):")

    def _pattern_enum(self) -> AnimationPattern:
        return AnimationPattern(self._pattern)

    def _apply(self) -> None:
        """Update the preview ring AND push the look to the node - every colour /
        pattern / segment change applies live, so there is no separate apply step."""
        arcs = [QColor(c) for c in self._arc_colors()]
        self._ring.set_look(arcs, self._pattern_enum(),
                            self._period_ms(), self._fade_ms(), self._angle)
        self._send(None, self._arc_colors(), self._pattern, self._fade_ms(),
                   self._angle)

    # -- slots ----------------------------------------------------------------

    def _set_color(self, hex_color: str) -> None:
        # Push onto the queue, keeping only the last `segments` clicks.
        self._color_queue.append(hex_color)
        if len(self._color_queue) > self._segments:
            self._color_queue = self._color_queue[-self._segments:]
        self._update_hint()
        self._apply()

    def _set_pattern(self, pattern: str) -> None:
        self._pattern = pattern
        for name, btn in self._pattern_buttons.items():
            btn.setChecked(name == pattern)
        self._apply()

    def _set_segments(self, n: int) -> None:
        self._segments = n
        if len(self._color_queue) > n:
            self._color_queue = self._color_queue[-n:]
        for name, count in _SEGMENTS.items():
            self._segment_buttons[name].setChecked(count == n)
        self._update_hint()
        self._apply()

    def _on_off(self) -> None:
        self._ring.set_look([QColor(_OFF_COLOR)], AnimationPattern.SOLID,
                            _PULSE_PERIOD_MS, self._fade_ms(), self._angle)
        self._send(None, None, "off", self._fade_ms(), self._angle)

    def _on_angle_live(self, angle: float) -> None:
        # The ring repaints itself while dragging; just track the value so the
        # next colour/pattern change (and the drop) use it. No send yet.
        self._angle = angle

    def _on_angle_release(self, angle: float) -> None:
        # Send once when the drag ends, so dragging doesn't flood the radio.
        self._angle = angle
        self._send(None, self._arc_colors(), self._pattern, self._fade_ms(),
                   self._angle)

    def _on_save_angle_clicked(self) -> None:
        """Persist the current angle as this ring's mounting orientation."""
        if self._on_save_angle is not None:
            self._on_save_angle(self._angle)

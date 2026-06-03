"""LED ring tester — a circle of N clickable LEDs for testing a WS2812 ring.

Basic colors (red, green, blue, white, yellow, cyan) with pattern selection
(solid, blink, pulse). Each action calls back into the host (the Test Actuators
dialog), which forwards a ``set_led`` command to the node over the gateway.
"""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from src.gui.led_animation import AnimationPattern, LedAnimator, apply_animation

_OFF_COLOR = QColor("#202020")

# Basic LED colors
_COLORS = {
    "Red":    "#ff0000",
    "Green":  "#00ff00",
    "Blue":   "#0000ff",
    "White":  "#ffffff",
    "Yellow": "#ffff00",
    "Cyan":   "#00ffff",
}

_PATTERNS = ["solid", "blink", "pulse"]


class LedRingWidget(QWidget):
    """Paints N LEDs evenly on a circle and reports clicks on them."""

    ledClicked = Signal(int)

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = max(1, count)
        self._colors = [QColor(_OFF_COLOR) for _ in range(self._count)]
        self._pattern = AnimationPattern.SOLID
        self._anim_step = 0
        self.setMinimumSize(280, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_color(self, i: int, color: QColor) -> None:
        if 0 <= i < self._count:
            self._colors[i] = QColor(color)
            self.update()

    def set_all(self, color: QColor) -> None:
        self._colors = [QColor(color) for _ in range(self._count)]
        self.update()

    def set_pattern(self, pattern: AnimationPattern) -> None:
        self._pattern = pattern

    def set_anim_step(self, step: int) -> None:
        self._anim_step = step
        self.update()

    def _geometry(self) -> tuple[list[QPointF], float]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        ring_r = min(w, h) / 2.0 - 18.0
        led_r = max(5.0, min(16.0, math.pi * ring_r / self._count / 1.4))
        pts = []
        for i in range(self._count):
            a = 2.0 * math.pi * i / self._count - math.pi / 2.0
            pts.append(QPointF(cx + ring_r * math.cos(a), cy + ring_r * math.sin(a)))
        return pts, led_r

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts, led_r = self._geometry()
        for i, color in enumerate(self._colors):
            display_color = apply_animation(color, self._pattern, self._anim_step, max_steps=10)
            painter.setBrush(display_color)
            painter.setPen(QPen(QColor("#7f8c8d"), 1))
            painter.drawEllipse(pts[i], led_r, led_r)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pts, led_r = self._geometry()
        pos = event.position()
        hit_r2 = (led_r + 3.0) ** 2
        for i, pt in enumerate(pts):
            if (pt.x() - pos.x()) ** 2 + (pt.y() - pos.y()) ** 2 <= hit_r2:
                self.ledClicked.emit(i)
                return


class LedRingTester(QGroupBox):
    """LED ring test: clickable ring + preset color/pattern buttons.

    ``send_cb(index, color_hex, pattern)`` is called on every change:
      - index: LED number, or None for whole ring
      - color_hex: "#RRGGBB" or None to turn off
      - pattern: "solid", "blink", "pulse"
    """

    def __init__(self, count: int,
                 send_cb: Callable[[int | None, str | None, str], None],
                 parent: QWidget | None = None) -> None:
        super().__init__("LED ring test", parent)
        self._send = send_cb
        self._color = "#ff0000"
        self._pattern = AnimationPattern.SOLID
        self._pattern_buttons = {}
        self._animator = LedAnimator(interval_ms=100, max_steps=10, parent=self)
        self._animator.stepped.connect(self._on_animation_step)

        main_layout = QHBoxLayout(self)

        # Left side: controls
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Select color & pattern:"))

        # Color buttons
        color_row = QHBoxLayout()
        for name, hex_color in _COLORS.items():
            btn = QPushButton(name)
            btn.clicked.connect(lambda checked=False, c=hex_color, n=name: self._set_color(c, n))
            color_row.addWidget(btn)
        color_row.addStretch()
        left_layout.addLayout(color_row)

        # Pattern buttons
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Pattern:"))
        for pattern in _PATTERNS:
            btn = QPushButton(pattern.capitalize())
            btn.clicked.connect(lambda checked=False, p=pattern: self._set_pattern(p))
            self._pattern_buttons[pattern] = btn
            pattern_row.addWidget(btn)
        pattern_row.addStretch()
        left_layout.addLayout(pattern_row)

        # Control buttons
        control_row = QHBoxLayout()
        all_btn = QPushButton("Apply to All")
        off_btn = QPushButton("Off")
        all_btn.clicked.connect(self._on_apply_all)
        off_btn.clicked.connect(self._on_off)
        control_row.addWidget(all_btn)
        control_row.addWidget(off_btn)
        control_row.addStretch()
        left_layout.addLayout(control_row)

        left_layout.addWidget(QLabel("Click LED to set individually:"))
        left_layout.addStretch()

        # Right side: LED ring
        self._ring = LedRingWidget(count)
        self._ring.ledClicked.connect(self._on_led_clicked)

        main_layout.addLayout(left_layout, 1)
        main_layout.addWidget(self._ring, 1)

    def _on_animation_step(self, step: int) -> None:
        """Callback when animation step changes."""
        self._ring.set_anim_step(step)

    def _set_color(self, hex_color: str, name: str) -> None:
        self._color = hex_color

    def _set_pattern(self, pattern: str) -> None:
        pattern_enum = AnimationPattern(pattern)
        self._pattern = pattern_enum
        self._ring.set_pattern(pattern_enum)
        if pattern_enum == AnimationPattern.SOLID:
            self._animator.stop()
        else:
            self._animator.start()

    def _on_led_clicked(self, i: int) -> None:
        self._ring.set_color(i, QColor(self._color))
        self._send(i, self._color, self._pattern.value)

    def _on_apply_all(self) -> None:
        self._ring.set_all(QColor(self._color))
        self._send(None, self._color, self._pattern.value)

    def _on_off(self) -> None:
        self._ring.set_all(_OFF_COLOR)
        self._send(None, None, "off")

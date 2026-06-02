"""LED ring tester — a circle of N clickable LEDs for testing a WS2812 ring.

Click a single LED to pick its colour, or use "Set all…" to colour the whole
ring. Each action calls back into the host (the Test Actuators dialog), which
forwards a ``set_led`` command to the node over the gateway.
"""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QColorDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

_OFF_COLOR = QColor("#202020")   # how an "off" LED is drawn in the UI


class LedRingWidget(QWidget):
    """Paints N LEDs evenly on a circle and reports clicks on them."""

    ledClicked = Signal(int)

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = max(1, count)
        self._colors = [QColor(_OFF_COLOR) for _ in range(self._count)]
        self.setMinimumSize(240, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_color(self, i: int, color: QColor) -> None:
        if 0 <= i < self._count:
            self._colors[i] = QColor(color)
            self.update()

    def set_all(self, color: QColor) -> None:
        self._colors = [QColor(color) for _ in range(self._count)]
        self.update()

    def _geometry(self) -> tuple[list[QPointF], float]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        ring_r = min(w, h) / 2.0 - 18.0
        # Dot radius scales with spacing so dense rings don't overlap.
        led_r = max(5.0, min(16.0, math.pi * ring_r / self._count / 1.4))
        pts = []
        for i in range(self._count):
            a = 2.0 * math.pi * i / self._count - math.pi / 2.0   # LED 0 at top
            pts.append(QPointF(cx + ring_r * math.cos(a), cy + ring_r * math.sin(a)))
        return pts, led_r

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts, led_r = self._geometry()
        for i, color in enumerate(self._colors):
            painter.setBrush(color)
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
    """LED ring test group: the ring + "set all" / "all off" controls.

    ``send_cb(index, color_hex)`` is called on every change:
      - index is the LED number, or None for the whole ring;
      - color_hex is "#RRGGBB", or None to turn off.
    """

    def __init__(self, count: int,
                 send_cb: Callable[[int | None, str | None], None],
                 parent: QWidget | None = None) -> None:
        super().__init__("LED ring test", parent)
        self._send = send_cb
        self._last = "#ff0000"

        layout = QVBoxLayout(self)
        self._ring = LedRingWidget(count)
        self._ring.ledClicked.connect(self._on_led_clicked)
        layout.addWidget(self._ring)
        layout.addWidget(QLabel("Click a LED to set its colour, or:"))

        row = QHBoxLayout()
        all_btn = QPushButton("Set all…")
        all_btn.clicked.connect(self._on_set_all)
        off_btn = QPushButton("All off")
        off_btn.clicked.connect(self._on_off)
        row.addWidget(all_btn)
        row.addWidget(off_btn)
        row.addStretch()
        layout.addLayout(row)

    def _pick(self) -> QColor | None:
        color = QColorDialog.getColor(QColor(self._last), self, "Pick LED colour")
        if not color.isValid():
            return None
        self._last = color.name()
        return color

    def _on_led_clicked(self, i: int) -> None:
        color = self._pick()
        if color is None:
            return
        self._ring.set_color(i, color)
        self._send(i, color.name())

    def _on_set_all(self) -> None:
        color = self._pick()
        if color is None:
            return
        self._ring.set_all(color)
        self._send(None, color.name())

    def _on_off(self) -> None:
        self._ring.set_all(_OFF_COLOR)
        self._send(None, None)

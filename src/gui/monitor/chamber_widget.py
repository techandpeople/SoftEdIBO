"""ChamberWidget — visualises a single AirChamber as a vertical pressure bar.

Reads pressure and state directly from the AirChamber object.
Inflate/deflate commands go through the parent Skin (hardware logic lives there).

Layout (top to bottom):
  [Touch]     press => skin.touch_press; release => skin.touch_release
  ┌──────┐
  │ bar  │    Custom painted widget:
  │      │      filled bar   = current pressure  (blue-green)
  │ ─────│      red line  ── = target pressure
  └──────┘
  Slot N
  75/90
  INFLATING
  [-]  [+]    deflate left, inflate right
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.hardware.air_chamber import AirChamber
from src.hardware.skin import Skin


# ---------------------------------------------------------------------------
# Custom bar widget
# ---------------------------------------------------------------------------

class _PressureBar(QWidget):
    """Vertical bar showing current pressure (fill) and target pressure (line)."""

    _BAR_COLOR    = QColor("#3daee9")   # current fill
    _TARGET_COLOR = QColor("#da4453")   # target line
    _BG_COLOR     = QColor("#e0e0e0")   # empty portion
    _LIMIT_COLOR  = QColor(200, 200, 200, 80)  # disabled zone (above max)

    def __init__(self, max_pressure: int = 100) -> None:
        super().__init__()
        self._current: int = 0
        self._target:  int = 0
        self._max_p = max_pressure
        self.setFixedWidth(18)
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_values(self, current: int, target: int) -> None:
        if self._current != current or self._target != target:
            self._current = current
            self._target  = target
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, self._BG_COLOR)

        # Shade zone above max pressure limit
        if self._max_p < 100:
            top_h = int(h * (100 - self._max_p) / 100)
            p.fillRect(0, 0, w, top_h, self._LIMIT_COLOR)
            pen = QPen(QColor("#888888"), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(0, top_h, w, top_h)

        # Current pressure fill (from bottom)
        if self._current > 0:
            fill_h = int(h * self._current / 100)
            p.fillRect(0, h - fill_h, w, fill_h, self._BAR_COLOR)

        # Target pressure line (2 px, full width)
        if self._target > 0:
            target_y = h - int(h * self._target / 100)
            target_y = max(1, min(h - 2, target_y))
            pen = QPen(self._TARGET_COLOR, 2)
            p.setPen(pen)
            p.drawLine(0, target_y, w, target_y)

        p.end()


# ---------------------------------------------------------------------------
# ChamberWidget
# ---------------------------------------------------------------------------

class ChamberWidget(QWidget):
    """Widget for a single AirChamber."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action: "press"|"release")

    def __init__(self, chamber: AirChamber, skin: Skin) -> None:
        super().__init__()
        self._chamber = chamber
        self._skin = skin
        self._press_time: float = 0.0
        self.setFixedWidth(50)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        # Touch — delegates inflate/deflate timing entirely to the controller
        self._touch_btn = QPushButton("T")
        self._touch_btn.setFixedWidth(33)
        self._touch_btn.setFixedHeight(20)

        def _on_press() -> None:
            self._press_time = time.monotonic()
            skin.touch_press(chamber.chamber_id)
            skin.fire_touch(chamber.chamber_id, 1023)
            self.touch_event.emit(skin.skin_id, chamber.chamber_id, "press")

        def _on_release() -> None:
            hold_ms = max(50, int((time.monotonic() - self._press_time) * 1000))
            skin.touch_release(chamber.chamber_id, hold_ms)
            skin.fire_touch(chamber.chamber_id, 0)
            self.touch_event.emit(skin.skin_id, chamber.chamber_id, "release")

        self._touch_btn.pressed.connect(_on_press)
        self._touch_btn.released.connect(_on_release)
        layout.addWidget(self._touch_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Pressure bar (current fill + target line + max limit zone)
        self._bar = _PressureBar(chamber.max_pressure)
        layout.addWidget(self._bar, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Info labels — small font to save space
        _SMALL = "font-size: 9px;"
        slot_lbl = QLabel(f"#{chamber.chamber_id}")
        slot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        slot_lbl.setStyleSheet(_SMALL)
        layout.addWidget(slot_lbl)

        self._pressure_lbl = QLabel("0/0")
        self._pressure_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pressure_lbl.setStyleSheet(_SMALL)
        layout.addWidget(self._pressure_lbl)

        self._state_lbl = QLabel("IDLE")
        self._state_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_lbl.setStyleSheet(_SMALL)
        layout.addWidget(self._state_lbl)

        # Inflate / deflate buttons — each click is a relative 10% step
        _STEP = 10
        btn_row = QHBoxLayout()
        btn_row.setSpacing(1)
        btn_row.setContentsMargins(0, 0, 0, 0)
        self._deflate_btn = QPushButton("-")
        self._deflate_btn.setFixedWidth(16)
        self._deflate_btn.setFixedHeight(18)
        self._inflate_btn = QPushButton("+")
        self._inflate_btn.setFixedWidth(16)
        self._inflate_btn.setFixedHeight(18)

        def _on_inflate() -> None:
            skin.inflate(chamber.chamber_id, _STEP)

        def _on_deflate() -> None:
            skin.deflate(chamber.chamber_id, _STEP)

        self._deflate_btn.clicked.connect(_on_deflate)
        self._inflate_btn.clicked.connect(_on_inflate)
        btn_row.addWidget(self._deflate_btn)
        btn_row.addWidget(self._inflate_btn)
        layout.addLayout(btn_row)

    def set_paused(self, paused: bool) -> None:
        """Enable or disable all interactive buttons."""
        self._touch_btn.setEnabled(not paused)
        self._inflate_btn.setEnabled(not paused)
        self._deflate_btn.setEnabled(not paused)

    def refresh(self) -> None:
        """Read state directly from the AirChamber object."""
        current = self._chamber.pressure
        target  = self._chamber.target_pressure
        self._bar.set_values(current, target)
        self._pressure_lbl.setText(f"{current}/{target}")
        if not self._skin.is_connected:
            self._state_lbl.setText("OFF")
        else:
            self._state_lbl.setText(self._chamber.state.value.upper())

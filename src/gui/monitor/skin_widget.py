"""SkinWidget — visualises a Skin as a labelled group of ChamberWidgets.

A Skin is a group of AirChambers sharing an ESP32 node.
One ChamberWidget is created per AirChamber in skin.chambers.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QSizePolicy

from src.gui.monitor.chamber_widget import ChamberWidget
from src.hardware.skin import Skin


class SkinWidget(QGroupBox):
    """Widget for a single Skin — one ChamberWidget per AirChamber."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, skin: Skin) -> None:
        super().__init__(skin.name)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._chamber_widgets: list[ChamberWidget] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        for chamber in sorted(skin.chambers.values(), key=lambda c: c.chamber_id):
            cw = ChamberWidget(chamber, skin)
            cw.touch_event.connect(self.touch_event)
            self._chamber_widgets.append(cw)
            layout.addWidget(cw)

    def set_paused(self, paused: bool) -> None:
        for cw in self._chamber_widgets:
            cw.set_paused(paused)

    def refresh(self) -> None:
        for cw in self._chamber_widgets:
            cw.refresh()

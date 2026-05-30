"""SkinWidget — visualises a Skin as a labelled group of ChamberWidgets.

A Skin is a group of AirChambers sharing an ESP32 node.
One ChamberWidget is created per AirChamber in skin.chambers.

When the skin has a configured spatial layout (``skin.chamber_grid``), a
read-only ``SkinGridView`` is shown above the chamber columns so the user
sees where each chamber sits on the physical surface and how full it is.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QSizePolicy, QVBoxLayout

from src.gui.monitor.chamber_widget import ChamberWidget
from src.gui.monitor.skin_grid_view import SkinGridView
from src.hardware.skin import Skin


class SkinWidget(QGroupBox):
    """Widget for a single Skin — one ChamberWidget per AirChamber."""

    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, skin: Skin) -> None:
        super().__init__(skin.name)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._chamber_widgets: list[ChamberWidget] = []
        self._grid_view: SkinGridView | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)

        # Spatial view (only when the skin has a configured chamber_grid).
        if skin.chamber_grid:
            self._grid_view = SkinGridView(skin)
            outer.addWidget(self._grid_view)

        # Chamber controls row (one ChamberWidget per AirChamber).
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(1)
        for chamber in sorted(skin.chambers.values(), key=lambda c: c.chamber_id):
            cw = ChamberWidget(chamber, skin)
            cw.touch_event.connect(self.touch_event)
            self._chamber_widgets.append(cw)
            cols.addWidget(cw)
        outer.addLayout(cols)

    def set_paused(self, paused: bool) -> None:
        for cw in self._chamber_widgets:
            cw.set_paused(paused)

    def refresh(self) -> None:
        for cw in self._chamber_widgets:
            cw.refresh()
        if self._grid_view is not None:
            self._grid_view.refresh()

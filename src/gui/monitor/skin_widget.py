"""SkinWidget — visualises a Skin as a labelled group of ChamberWidgets.

A Skin is a group of AirChambers sharing an ESP32 node.
One ChamberWidget is created per AirChamber in skin.chambers.

When the skin has a configured spatial layout (``skin.chamber_grid``), a
read-only ``SkinGridView`` is shown above the chamber columns so the user
sees where each chamber sits on the physical surface and how full it is.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QSizePolicy, QVBoxLayout

from src.gui.monitor.chamber_widget import ChamberWidget
from src.gui.monitor.skin_grid_view import SkinGridView
from src.gui.monitor.touch_tuning_panel import TouchTuningPanel
from src.hardware.skin import Skin


class SkinWidget(QGroupBox):
    """Widget for a single Skin — one ChamberWidget per AirChamber."""

    touch_event   = Signal(str, int, str)  # (skin_id, chamber_id, action)
    _sensor_pulse = Signal(int)            # thread-safe bridge → pulse_sensor

    def __init__(self, skin: Skin) -> None:
        super().__init__(skin.name)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._chamber_widgets: list[ChamberWidget] = []
        self._grid_view: SkinGridView | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)

        # Spatial view (only when the skin has a configured chamber_grid).
        if skin.chamber_grid:
            self._grid_view = SkinGridView(skin)
            outer.addWidget(self._grid_view, alignment=Qt.AlignmentFlag.AlignCenter)
            # Mirror real-hardware touch events as yellow pulses on the grid.
            # Use a Signal bridge so the gateway thread never calls Qt directly.
            touch_ctrl = getattr(skin, "touch_controller", None)
            if touch_ctrl is not None:
                on_touch = getattr(touch_ctrl, "on_touch", None)
                if on_touch is not None:
                    # Force QueuedConnection for the same reason as _magnet_msg:
                    # gateway thread is a Python thread, not QThread.
                    self._sensor_pulse.connect(
                        self._grid_view.pulse_sensor,
                        Qt.ConnectionType.QueuedConnection,
                    )
                    on_touch(lambda sensor_id, _raw: self._sensor_pulse.emit(sensor_id))

        # Live tuning panel for skins with 4-sensor quadrant touch detection.
        if skin.has_touch_tracking:
            outer.addWidget(TouchTuningPanel(skin))

        # Chamber controls row (one ChamberWidget per AirChamber).
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(1)
        for chamber in sorted(skin.chambers.values(), key=lambda c: c.chamber_id):
            cw = ChamberWidget(chamber, skin)
            cw.touch_event.connect(self.touch_event)
            cw.touch_event.connect(self._relay_to_grid)
            self._chamber_widgets.append(cw)
            cols.addWidget(cw)
        outer.addLayout(cols)

    def _relay_to_grid(self, _skin_id: str, chamber_id: int, action: str) -> None:
        """Mirror chamber touches onto the SkinGridView as a blue pulse on
        the chamber's cells (simulation T-button, or hardware tap)."""
        if action == "press" and self._grid_view is not None:
            self._grid_view.pulse_chamber(chamber_id)

    def set_paused(self, paused: bool) -> None:
        for cw in self._chamber_widgets:
            cw.set_paused(paused)

    def refresh(self) -> None:
        for cw in self._chamber_widgets:
            cw.refresh()
        if self._grid_view is not None:
            self._grid_view.refresh()

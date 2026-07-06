"""SkinWidget — visualises a Skin as a labelled group of ChamberWidgets.

A Skin is a group of AirChambers sharing an ESP32 node.
One ChamberWidget is created per AirChamber in skin.chambers.

When the skin has a configured spatial layout (``skin.chamber_grid``), a
read-only ``SkinGridView`` is shown above the chamber columns so the user
sees where each chamber sits on the physical surface and how full it is.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QSizePolicy,
                               QVBoxLayout)

from src.gui.monitor.chamber_widget import ChamberWidget
from src.gui.monitor.organ_panel import OrganPanel
from src.gui.monitor.skin_grid_view import SkinGridView
from src.gui.monitor.touch_tuning_panel import TouchTuningPanel
from src.hardware.skin import Skin
from src.hardware.touch_source import CompensatedMagnetSource


class SkinWidget(QGroupBox):
    """Widget for a single Skin — one ChamberWidget per AirChamber."""

    touch_event   = Signal(str, int, str)  # (skin_id, chamber_id, action)
    _sensor_pulse = Signal(int)            # thread-safe bridge → pulse_sensor
    _touch_log    = Signal(int, str)       # thread-safe bridge → touch_event log

    def __init__(self, skin: Skin) -> None:
        super().__init__(skin.skin_id)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._skin_id = skin.skin_id
        self._has_organs = bool(getattr(skin, "organs", None))
        self._chamber_widgets: list[ChamberWidget] = []
        self._grid_view: SkinGridView | None = None
        self._organ_panel: OrganPanel | None = None

        # Bubble skin sensor touches up as touch_event so the session panel logs
        # them. The Skin's TouchEventRouter fires on the gateway thread, so hop
        # through a QueuedConnection bridge before re-emitting on the GUI thread.
        self._touch_log.connect(self._emit_touch_log, Qt.ConnectionType.QueuedConnection)
        skin.on_touch_event(lambda chamber_id, action: self._touch_log.emit(chamber_id, action))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)

        # Top row: spatial grid view (chambers) and, beside it, the organ
        # status panel (numbered dots + LED light) when the skin has organs.
        top = QHBoxLayout()
        top.setSpacing(6)
        if skin.chamber_grid:
            self._grid_view = SkinGridView(skin)
            top.addWidget(self._grid_view, alignment=Qt.AlignmentFlag.AlignCenter)
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
        if self._has_organs:
            self._organ_panel = OrganPanel(skin)
            top.addWidget(self._organ_panel, alignment=Qt.AlignmentFlag.AlignTop)
        if self._grid_view is not None or self._organ_panel is not None:
            outer.addLayout(top)

        # Raw↔compensated toggle for the sensor highlight, shown only when this
        # skin has a calibrated + enabled coupling (so ``touch_source`` is the
        # compensating source). Default compensated: the grid then lights the same
        # touches the activities react to, instead of false ones from actuation.
        if (self._grid_view is not None
                and isinstance(getattr(skin, "touch_source", None),
                               CompensatedMagnetSource)):
            compensate_cb = QCheckBox("Compensate actuation coupling")
            compensate_cb.setChecked(True)
            help_text = (
                "Subtract the calibrated actuation coupling from the sensor "
                "highlight, so an inflating chamber no longer flashes a false "
                "touch on the grid — matching what the activities detect. "
                "Uncheck to see the raw sensor field for diagnosis.")
            compensate_cb.setToolTip(help_text)
            compensate_cb.setWhatsThis(help_text)
            compensate_cb.toggled.connect(self._grid_view.set_compensated)
            outer.addWidget(compensate_cb, alignment=Qt.AlignmentFlag.AlignCenter)

        # Live tuning panel for skins with 4-sensor quadrant touch detection.
        # Keep it compact: it must NOT stretch to the (now wider) grid view's
        # width — only the SkinGridView shape should scale up.
        if skin.has_touch_tracking:
            tuning = TouchTuningPanel(skin)
            tuning.setSizePolicy(QSizePolicy.Policy.Maximum,
                                 QSizePolicy.Policy.Fixed)
            outer.addWidget(tuning, alignment=Qt.AlignmentFlag.AlignCenter)

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

    def _emit_touch_log(self, chamber_id: int, action: str) -> None:
        """Re-emit a router touch event as the widget's ``touch_event`` (on the
        GUI thread) so it bubbles up to the session panel for DB logging."""
        self.touch_event.emit(self._skin_id, chamber_id, action)

    def _relay_to_grid(self, _skin_id: str, chamber_id: int, action: str) -> None:
        """Mirror chamber touches onto the SkinGridView as a blue pulse on
        the chamber's cells (simulation T-button, or hardware tap)."""
        if action == "press" and self._grid_view is not None:
            self._grid_view.pulse_chamber(chamber_id)

    @property
    def skin_id(self) -> str:
        return self._skin_id

    @property
    def organ_view(self) -> OrganPanel | None:
        """The organ status panel (numbered dots + LED) for this skin, or None
        when the skin has no organs."""
        return self._organ_panel

    def set_paused(self, paused: bool) -> None:
        for cw in self._chamber_widgets:
            cw.set_paused(paused)

    def refresh(self) -> None:
        for cw in self._chamber_widgets:
            cw.refresh()
        if self._grid_view is not None:
            self._grid_view.refresh()

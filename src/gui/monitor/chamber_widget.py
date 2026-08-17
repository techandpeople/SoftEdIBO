"""ChamberWidget - visualises a single AirChamber as a vertical pressure bar.

Reads pressure and state directly from the AirChamber object.
Inflate/deflate commands go through the parent Skin (hardware logic lives there).

Layout (top to bottom):
  +------+
  | bar  |    Custom painted PressureBar (promoted widget):
  |      |      filled bar   = current pressure  (blue-green)
  | -----|      red line  -- = target pressure
  +------+
  Slot N
  75/90
  INFLATING
  [-]  [+]    deflate left, inflate right

The layout lives in ``src/gui/ui/chamber_widget.ui`` (edit in Qt Designer,
recompile with ``scripts/compile_ui.sh``); the pressure bar is the promoted
``PressureBar`` widget. This module keeps only the data binding + wiring.

Touch simulation lives on the SkinGridView (one T button per sensor, laid
out on the grid). The ``touch_event`` signal is kept so future hardware
chamber-touch sources can still drive the grid-view pulse without touching
this widget.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from src.gui.ui_chamber_widget import Ui_ChamberWidget
from src.hardware.air_chamber import AirChamber
from src.hardware.skin import Skin

_STEP = 10  # each inflate/deflate click is a relative 10% step


class ChamberWidget(QWidget, Ui_ChamberWidget):
    """Widget for a single AirChamber."""

    # Kept for future hardware chamber-touch sources so the SkinGridView
    # blue-pulse path stays wired up. No longer emitted from this widget -
    # sensor T-buttons on the grid simulate touches now.
    touch_event = Signal(str, int, str)  # (skin_id, chamber_id, action)

    def __init__(self, chamber: AirChamber, skin: Skin) -> None:
        super().__init__()
        self.setupUi(self)
        self._chamber = chamber
        self._skin = skin
        self.setFixedWidth(50)

        self.slot_lbl.setText(f"#{chamber.chamber_id}")

        self.deflate_btn.clicked.connect(
            lambda: skin.deflate(chamber.chamber_id, _STEP))
        self.inflate_btn.clicked.connect(
            lambda: skin.inflate(chamber.chamber_id, _STEP))

    def set_paused(self, paused: bool) -> None:
        """Enable or disable all interactive buttons."""
        self.inflate_btn.setEnabled(not paused)
        self.deflate_btn.setEnabled(not paused)

    def refresh(self) -> None:
        """Read state directly from the AirChamber object."""
        current = self._chamber.pressure
        target  = self._chamber.target_pressure
        self.bar.set_values(current, target)
        self.pressure_lbl.setText(f"{current}/{target}")
        # Absolute kPa when the firmware reports it (matches the Test Actuators
        # readout); blank for the simulator / pre-kPa firmware (NaN).
        kpa = self._chamber.kpa
        self.kpa_lbl.setText(f"{kpa:.1f} kPa" if kpa == kpa else "")
        if not self._skin.is_connected:
            self.state_lbl.setText("OFF")
        else:
            self.state_lbl.setText(self._chamber.state.value.upper())

"""TankWidget — visualises a reservoir tank (pressure or vacuum) on a robot.

One TankWidget per AirReservoir. The tank pressure is read from
``reservoir.pressure`` (already a 0-100 percent reading reported by the firmware).

The layout lives in ``src/gui/ui/tank_widget.ui`` (edit in Qt Designer, recompile
with ``scripts/compile_ui.sh``); the level bar is the promoted ``TankBar`` widget.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox

from src.gui.ui_tank_widget import Ui_TankWidget
from src.hardware.air_reservoir import AirReservoir


class TankWidget(QGroupBox, Ui_TankWidget):
    """Widget for a single reservoir tank (pressure or vacuum)."""

    def __init__(self, reservoir: AirReservoir) -> None:
        super().__init__()
        self.setupUi(self)
        self._reservoir = reservoir
        self.setFixedWidth(60)
        self.setTitle("Pressure" if reservoir.kind == "pressure" else "Vacuum")
        self.bar.set_kind(reservoir.kind)

    def refresh(self) -> None:
        current = self._reservoir.pressure
        self.bar.set_value(current)
        self.pressure_lbl.setText(f"{current}%")
        if not self._reservoir.is_connected:
            self.state_lbl.setText("OFF")
        else:
            self.state_lbl.setText("ON")

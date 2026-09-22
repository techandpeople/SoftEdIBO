"""Sensor-position editor: which skin quadrant each touch sensor sits in.

A small group box for the Skin dialog's touch section. One row per sensor
with a quadrant combo (Q1 top-left .. Q4 bottom-right); the rows are built
data-driven from the sensor count, into the ``.ui``'s form. Only a
four-sensor board has corner quadrants (the thesis quadrant detector's
layout), so the box hides itself for any other count, where the sensors are
placed evenly instead (see :func:`src.core.touch_zones.quadrant_placements`).

The value round-trips through the skin's ``touch.sensor_quadrants`` map
(sensor index -> quadrant name), kept terse: only sensors moved off their
default ``Q{i+1}`` are stored.
"""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtWidgets import QComboBox, QGroupBox, QLabel, QWidget

from src.core.touch_zones import (QUADRANT_LABELS, QUADRANT_NAMES,
                                  normalise_sensor_quadrants)
from src.gui.ui_sensor_zones_widget import Ui_SensorZonesWidget


class SensorZonesWidget(QGroupBox, Ui_SensorZonesWidget):
    """Per-sensor quadrant combos, shown only for a four-sensor layout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._combos: list[QComboBox] = []
        self._count = 0
        self.set_sensor_count(len(QUADRANT_NAMES))

    # -- API ------------------------------------------------------------------

    def set_sensor_count(self, count: int) -> None:
        """Rebuild the rows for ``count`` sensors (hidden unless it is four)."""
        count = max(0, int(count))
        if count == self._count and self._combos:
            return
        current = self.assignments()
        self._clear_rows()
        self._count = count
        quadrant = count == len(QUADRANT_NAMES)
        self.setVisible(quadrant)
        if not quadrant:
            return
        for idx in range(count):
            combo = QComboBox()
            for name in QUADRANT_NAMES:
                combo.addItem(f"{name} ({QUADRANT_LABELS[name]})", userData=name)
            combo.setWhatsThis(
                f"The corner sensor {idx} is mounted in. A touch on this sensor "
                "lights the LED-strip arc above that corner in activities that "
                "use zone fills.")
            combo.setCurrentIndex(idx)
            self._combos.append(combo)
            self.rows_form.addRow(QLabel(f"Sensor {idx}:"), combo)
        self.set_assignments(current)

    def set_assignments(self, sensor_quadrants: Mapping[Any, Any] | None) -> None:
        """Apply a saved ``sensor_quadrants`` map (missing = default)."""
        cleaned = normalise_sensor_quadrants(sensor_quadrants, self._count)
        for idx, combo in enumerate(self._combos):
            name = cleaned.get(str(idx), QUADRANT_NAMES[idx])
            pos = combo.findData(name)
            combo.setCurrentIndex(pos if pos >= 0 else idx)

    def assignments(self) -> dict[str, str]:
        """The map to save: only sensors moved off their default quadrant.
        Empty when the layout is not four sensors (or nothing was moved)."""
        raw = {str(idx): str(combo.currentData())
               for idx, combo in enumerate(self._combos)}
        return normalise_sensor_quadrants(raw, self._count)

    # -- internals ------------------------------------------------------------

    def _clear_rows(self) -> None:
        while self.rows_form.rowCount():
            self.rows_form.removeRow(0)
        self._combos.clear()

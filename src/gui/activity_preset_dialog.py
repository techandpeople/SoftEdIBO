"""Activity Presets manager dialog (Tools => Activity Presets…).

Lets the operator browse, create, edit and delete the named parameter
presets stored in the ``activity_presets`` table. The form is generated
on the fly from each activity's declared ``PARAMS`` (and the inherited
``SIM_PARAMS``) so adding a new tunable knob to an activity is a one-line
change with no UI work.

See ``docs/ACTIVITIES.md`` for the broader behaviour-framework plan.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# How many sensor/chamber slots the `sensor_map` editor exposes by default.
_SENSOR_MAP_MAX_INDEX = 15

from src.activities import ACTIVITIES
from src.activities.base_activity import BaseActivity, Param
from src.data.database import Database
from src.data.models import ActivityPreset
from src.gui.ui_activity_preset_dialog import Ui_ActivityPresetDialog


_NEW_LABEL = "(unsaved preset)"


class _ColorButton(QPushButton):
    """Button that previews a colour and opens QColorDialog on click."""

    def __init__(self, initial: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value: str = initial or "#000000"
        self.setFixedHeight(24)
        self._refresh()
        self.clicked.connect(self._pick)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value or "#000000"
        self._refresh()

    def _refresh(self) -> None:
        self.setText(self._value)
        self.setStyleSheet(
            f"QPushButton {{ background: {self._value}; color: "
            f"{'#000' if self._readable_on_light() else '#fff'}; }}"
        )

    def _readable_on_light(self) -> bool:
        c = QColor(self._value)
        # YIQ luma — pick light text on dark backgrounds.
        return (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000 > 128

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._value), self, "Pick colour")
        if c.isValid():
            self.set_value(c.name())


class _SensorMap(QWidget):
    """Compact editor for ``{sensor_idx: chamber_idx}`` routings.

    The user gets one row per mapping with two int spin-boxes ("Sensor",
    "Chamber") instead of having to type JSON by hand. Rows can be added
    or removed with the ``+`` / ``×`` buttons.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QSpinBox, QSpinBox, QPushButton]] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        outer.addLayout(self._rows_layout)
        add_btn = QPushButton("+ Add mapping")
        add_btn.clicked.connect(lambda: self._add_row())
        outer.addWidget(add_btn)

    # ------------------------------------------------------------------
    # Public API (used by ActivityPresetDialog._set/_get_widget_value)
    # ------------------------------------------------------------------

    def value(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for sensor_spin, chamber_spin, _ in self._rows:
            out[str(int(sensor_spin.value()))] = int(chamber_spin.value())
        return out

    def set_value(self, mapping: dict[str, int] | None) -> None:
        # Drop existing rows then rebuild from the dict (keys may be ints
        # or stringified ints, depending on how the preset was authored).
        for sensor_spin, chamber_spin, btn in self._rows[:]:
            self._remove_row(sensor_spin, chamber_spin, btn)
        for raw_key, raw_val in (mapping or {}).items():
            try:
                self._add_row(int(raw_key), int(raw_val))
            except (TypeError, ValueError):
                continue

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _add_row(self, sensor: int = 0, chamber: int = 0) -> None:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        sensor_spin = QSpinBox()
        sensor_spin.setRange(0, _SENSOR_MAP_MAX_INDEX)
        sensor_spin.setPrefix("S")
        sensor_spin.setValue(sensor)
        chamber_spin = QSpinBox()
        chamber_spin.setRange(0, _SENSOR_MAP_MAX_INDEX)
        chamber_spin.setPrefix("C")
        chamber_spin.setValue(chamber)
        rm_btn = QPushButton("×")
        rm_btn.setFixedWidth(28)
        row.addWidget(sensor_spin)
        row.addWidget(QLabel("→"))
        row.addWidget(chamber_spin)
        row.addStretch()
        row.addWidget(rm_btn)
        self._rows_layout.addWidget(row_widget)
        entry = (sensor_spin, chamber_spin, rm_btn)
        self._rows.append(entry)
        rm_btn.clicked.connect(lambda: self._remove_row(*entry))

    def _remove_row(self, sensor_spin: QSpinBox,
                    chamber_spin: QSpinBox, btn: QPushButton) -> None:
        target = (sensor_spin, chamber_spin, btn)
        if target not in self._rows:
            return
        self._rows.remove(target)
        # Walk up to the row widget (the QHBoxLayout's parent) and remove it.
        row_widget = sensor_spin.parentWidget()
        if row_widget is not None:
            row_widget.setParent(None)
            row_widget.deleteLater()


class ActivityPresetDialog(QDialog, Ui_ActivityPresetDialog):
    """Browse / edit / delete activity presets, stored in the DB."""

    def __init__(self, db: Database, parent: QWidget | None = None,
                 *, initial_activity: BaseActivity | None = None,
                 apply_on_close: bool = False) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._db = db
        self._current_preset_id: str | None = None
        self._widgets: dict[str, QWidget] = {}
        self._initial_activity = initial_activity
        self._apply_on_close = apply_on_close

        # The static frame lives in the .ui; the params form is rebuilt per
        # activity into ``params_layout``.
        for activity in ACTIVITIES:
            self.activity_combo.addItem(activity.name, userData=activity)
        self.activity_combo.currentIndexChanged.connect(self._on_activity_changed)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.new_btn.clicked.connect(self._on_new)
        self.delete_btn.clicked.connect(self._on_delete)
        self.save_btn.clicked.connect(self._on_save)
        self.close_btn.clicked.connect(self._on_close)

        # Bootstrap selection — prefer the activity the caller asked for
        # (e.g. the one already picked in SessionSetupDialog) so the user
        # lands on the right form without having to reselect.
        if ACTIVITIES:
            start_idx = 0
            if self._initial_activity is not None:
                pos = self.activity_combo.findText(self._initial_activity.name)
                if pos >= 0:
                    start_idx = pos
            self.activity_combo.setCurrentIndex(start_idx)
            if start_idx == 0:
                # setCurrentIndex(0) when it was already 0 doesn't fire the
                # signal — kick the rebuild manually.
                self._on_activity_changed(0)

    # ------------------------------------------------------------------
    # Selection handlers
    # ------------------------------------------------------------------

    def selected_preset(self) -> ActivityPreset | None:
        """Return the currently selected preset (for external apply-on-close)."""
        if self._current_preset_id is None:
            return None
        return self._db.get_activity_preset(self._current_preset_id)

    def _current_activity(self) -> BaseActivity | None:
        return self.activity_combo.currentData()

    def _on_activity_changed(self, _index: int) -> None:
        """Rebuild the form and reload the preset list for the new activity."""
        activity = self._current_activity()
        if activity is None:
            return
        self._rebuild_form(activity)
        self._reload_preset_list()
        # Select the first preset (if any) or fall back to defaults.
        if self.preset_combo.count() > 1:
            self.preset_combo.setCurrentIndex(1)  # 0 is the "(new)" sentinel
        else:
            self._on_preset_changed(0)

    def _on_preset_changed(self, _index: int) -> None:
        """Load the selected preset's values into the form. Sentinel index 0
        means 'unsaved' — show defaults."""
        activity = self._current_activity()
        if activity is None:
            return
        preset_id = self.preset_combo.currentData()
        self._current_preset_id = preset_id
        if preset_id is None:
            self.name_edit.setText("")
            self.desc_edit.setText("")
            self._fill_form({p.name: p.default for p in activity.all_params()})
            self.delete_btn.setEnabled(False)
            return
        preset = self._db.get_activity_preset(preset_id)
        if preset is None:
            return
        self.name_edit.setText(preset.name)
        self.desc_edit.setText(preset.description)
        self._fill_form(preset.params)
        self.delete_btn.setEnabled(True)

    def _on_new(self) -> None:
        """Start an unsaved preset with the activity's defaults."""
        self.preset_combo.setCurrentIndex(0)
        self.name_edit.setFocus()

    def _on_save(self) -> None:
        activity = self._current_activity()
        if activity is None:
            return
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name",
                                "Give the preset a name before saving.")
            return
        values = self._collect_form()
        if values is None:
            return  # validation already complained
        now = datetime.now()
        preset_id = self._current_preset_id or self._db.next_activity_preset_id()
        preset = ActivityPreset(
            preset_id=preset_id,
            activity_name=activity.name,
            name=name,
            description=self.desc_edit.text().strip(),
            params=values,
            created_at=now,
            updated_at=now,
        )
        self._db.save_activity_preset(preset)
        self._reload_preset_list()
        # Re-select the just-saved preset.
        for i in range(self.preset_combo.count()):
            if self.preset_combo.itemData(i) == preset_id:
                self.preset_combo.setCurrentIndex(i)
                break

    def _on_close(self) -> None:
        """Close dialog, applying preset if opened from manage."""
        self.accept()

    def _on_delete(self) -> None:
        if self._current_preset_id is None:
            return
        reply = QMessageBox.question(
            self, "Delete preset",
            f"Delete preset {self._current_preset_id}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._db.delete_activity_preset(self._current_preset_id)
        self._current_preset_id = None
        self._reload_preset_list()
        if self.preset_combo.count() > 1:
            self.preset_combo.setCurrentIndex(1)
        else:
            self._on_preset_changed(0)

    # ------------------------------------------------------------------
    # Form builders / collectors
    # ------------------------------------------------------------------

    def _reload_preset_list(self) -> None:
        activity = self._current_activity()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(_NEW_LABEL, userData=None)
        if activity is not None:
            for p in self._db.get_activity_presets(activity.name):
                self.preset_combo.addItem(f"{p.name} [{p.preset_id}]",
                                           userData=p.preset_id)
        self.preset_combo.blockSignals(False)

    def _rebuild_form(self, activity: BaseActivity) -> None:
        """Drop and rebuild the param rows for ``activity``."""
        while self.params_layout.rowCount():
            self.params_layout.removeRow(0)
        self._widgets.clear()
        for param in activity.all_params():
            widget = self._build_widget(param)
            widget.setToolTip(param.description or param.name)
            self.params_layout.addRow(param.display_label() + ":", widget)
            self._widgets[param.name] = widget

    def _fill_form(self, values: dict[str, Any]) -> None:
        for name, widget in self._widgets.items():
            if name not in values:
                continue
            self._set_widget_value(widget, values[name])

    def _collect_form(self) -> dict[str, Any] | None:
        """Read every widget into a dict. Returns ``None`` if validation fails
        (e.g. invalid JSON in a json-typed param)."""
        out: dict[str, Any] = {}
        for name, widget in self._widgets.items():
            try:
                out[name] = self._get_widget_value(widget)
            except ValueError as exc:
                QMessageBox.warning(
                    self, f"Invalid value for '{name}'",
                    str(exc),
                )
                return None
        return out

    # ------------------------------------------------------------------
    # Per-type widget plumbing
    # ------------------------------------------------------------------

    def _build_widget(self, param: Param) -> QWidget:
        match param.type:
            case "int":
                w = QSpinBox()
                lo = -(10**9) if param.min is None else int(param.min)
                hi =  (10**9) if param.max is None else int(param.max)
                w.setRange(lo, hi)
                w.setValue(int(param.default))
                return w
            case "float":
                w = QDoubleSpinBox()
                lo = -1e12 if param.min is None else float(param.min)
                hi =  1e12 if param.max is None else float(param.max)
                w.setRange(lo, hi)
                w.setDecimals(3)
                w.setValue(float(param.default))
                return w
            case "bool":
                w = QCheckBox()
                w.setChecked(bool(param.default))
                return w
            case "color":
                return _ColorButton(str(param.default))
            case "enum":
                w = QComboBox()
                for choice in param.choices:
                    w.addItem(str(choice), userData=choice)
                pos = w.findData(param.default)
                if pos >= 0:
                    w.setCurrentIndex(pos)
                return w
            case "json":
                w = QPlainTextEdit()
                w.setPlaceholderText("Valid JSON, e.g. {\"key\": value}")
                w.setPlainText(json.dumps(param.default, indent=2))
                w.setFixedHeight(120)
                return w
            case "sensor_map":
                # Tabular editor for sensor → chamber dicts so the user
                # picks spin-box values instead of typing JSON.
                m = _SensorMap()
                m.set_value(param.default if isinstance(param.default, dict)
                            else {})
                return m
            case _:
                w = QLineEdit()
                w.setText(str(param.default))
                return w

    @staticmethod
    def _set_widget_value(widget: QWidget, value: Any) -> None:
        if isinstance(widget, _SensorMap):
            widget.set_value(value if isinstance(value, dict) else {})
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value))
        elif isinstance(widget, QDoubleSpinBox):
            widget.setValue(float(value))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, _ColorButton):
            widget.set_value(str(value))
        elif isinstance(widget, QComboBox):
            pos = widget.findData(value)
            if pos >= 0:
                widget.setCurrentIndex(pos)
        elif isinstance(widget, QPlainTextEdit):
            widget.setPlainText(json.dumps(value, indent=2))
        elif isinstance(widget, QLineEdit):
            widget.setText(str(value))

    @staticmethod
    def _get_widget_value(widget: QWidget) -> Any:
        if isinstance(widget, _SensorMap):
            return widget.value()
        if isinstance(widget, QSpinBox):
            return int(widget.value())
        if isinstance(widget, QDoubleSpinBox):
            return float(widget.value())
        if isinstance(widget, QCheckBox):
            return bool(widget.isChecked())
        if isinstance(widget, _ColorButton):
            return widget.value()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QPlainTextEdit):
            raw = widget.toPlainText().strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Not valid JSON: {exc}") from exc
        if isinstance(widget, QLineEdit):
            return widget.text()
        return None

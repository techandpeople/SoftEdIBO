"""Skin configuration dialog.

Adds or edits a single skin entry under a robot.
A skin groups 1-3 chambers, each referencing a (node MAC, node slot) pair
from the robot's configured nodes.

Layout:
    Skin ID / Name fields
    ┌─────────────────────────────────────┐
    │ Chamber 1: [MAC ▾] Slot [0] Max [8.0 kPa] [✕] │
    │ Chamber 2: [MAC ▾] Slot [1] Max [8.0 kPa] [✕] │
    │ [+ Add Chamber]                               │
    └─────────────────────────────────────┘
    [Test] [Delete] [Cancel] [Save]
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.data.models import SkinTemplate
from src.gui.skin_grid_editor import SkinGridEditor
from src.hardware.espnow_gateway import ESPNowGateway

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}
_DEFAULT_MAX_KPA   = 8.0
_MAX_ALLOWED_KPA   = 12.0
_CONFIRM_DELTA     = 2.0
_MAX_CHAMBERS      = 3
_NONE_LABEL        = "(none)"
_MISSING_FIELD_TITLE = "Missing Field"


class _ChamberRow(QWidget):
    """A single chamber row: MAC dropdown + slot spinbox + max pressure spinbox + remove."""

    def __init__(
        self,
        node_macs: list[str],
        node_max_slots: dict[str, int],
        mac: str = "",
        slot: int = 0,
        max_pressure: float = _DEFAULT_MAX_KPA,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._node_macs      = node_macs
        self._node_max_slots = node_max_slots

        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(4)

        self._mac_combo = QComboBox()
        self._mac_combo.setMinimumWidth(160)
        for m in node_macs:
            self._mac_combo.addItem(m)
        idx = self._mac_combo.findText(mac)
        if idx >= 0:
            self._mac_combo.setCurrentIndex(idx)
        self._mac_combo.currentTextChanged.connect(self._on_mac_changed)

        self._slot_spin = QSpinBox()
        self._slot_spin.setPrefix("Slot ")
        self._slot_spin.setMinimum(0)
        self._slot_spin.setValue(slot)
        self._update_slot_max()

        self._max_spin = QDoubleSpinBox()
        self._max_spin.setDecimals(1)
        self._max_spin.setSingleStep(0.1)
        self._max_spin.setRange(0.1, _MAX_ALLOWED_KPA)
        self._max_spin.setValue(max_pressure)
        self._max_spin.setSuffix(" kPa")
        self._max_spin.setFixedWidth(75)

        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedWidth(24)
        self._remove_btn.setFixedHeight(24)

        hbox.addWidget(self._mac_combo, stretch=1)
        hbox.addWidget(self._slot_spin)
        hbox.addWidget(QLabel("Max:"))
        hbox.addWidget(self._max_spin)
        hbox.addWidget(self._remove_btn)

    @property
    def remove_btn(self) -> QPushButton:
        return self._remove_btn

    def get_values(self) -> tuple[str, int, float]:
        return (
            self._mac_combo.currentText(),
            self._slot_spin.value(),
            round(self._max_spin.value(), 1),
        )

    def _on_mac_changed(self, _mac: str) -> None:
        self._update_slot_max()

    def _update_slot_max(self) -> None:
        mac      = self._mac_combo.currentText()
        max_slot = max(0, self._node_max_slots.get(mac, 3) - 1)
        self._slot_spin.setMaximum(max_slot)
        if self._slot_spin.value() > max_slot:
            self._slot_spin.setValue(0)


class SkinConfigDialog(QDialog):
    """Dialog for adding or editing a single skin entry.

    Args:
        robot_type:  One of ``"turtle"``, ``"tree"``, or ``"thymio"``.
        robot_index: Index of the parent robot in the settings list.
        skin_index:  Index of this skin in the robot's ``skins`` list,
                     or ``-1`` to add a new skin.
        settings:    Application settings instance.
        gateway:     Shared ESP-NOW gateway (used by the test dialog).
        parent:      Optional parent widget.
    """

    def __init__(
        self,
        robot_type: str,
        robot_index: int,
        skin_index: int,
        settings: Settings,
        gateway: ESPNowGateway,
        db=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._robot_type  = robot_type
        self._robot_index = robot_index
        self._skin_index  = skin_index
        self._settings    = settings
        self._gateway     = gateway
        self._db          = db
        self._rows: list[_ChamberRow] = []
        # Track if user manually edited the skin_id — if so, stop auto-filling
        # it on template changes so we don't clobber their typing.
        self._skin_id_user_edited = False

        is_new = skin_index < 0
        self.setWindowTitle("Add Skin" if is_new else "Configure Skin")
        # Two-column layout keeps the dialog reasonable on smaller screens
        # without an outer scrollbar — left = metadata/chambers/touch,
        # right = layout/grid editor (the tallest section).
        self.setMinimumWidth(820)
        self.resize(900, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- Two-column body ----
        columns = QHBoxLayout()
        columns.setSpacing(8)

        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)

        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 0, 0)

        columns.addWidget(left_w, stretch=2)
        columns.addWidget(right_w, stretch=3)
        root.addLayout(columns, stretch=1)

        # ---- LEFT COLUMN ----
        # Template row — pick a saved layout to auto-fill, or save the current
        # configuration as a new template for reuse on other skins.
        if self._db is not None:
            left.addLayout(self._build_template_row())

        # Skin ID / Name
        form = QFormLayout()
        self._skin_id_edit = QLineEdit()
        self._skin_id_edit.setPlaceholderText("e.g. belly")
        self._skin_id_edit.textEdited.connect(self._on_skin_id_edited)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Display name (e.g. Belly)")
        form.addRow("Skin ID:", self._skin_id_edit)
        form.addRow("Name:",    self._name_edit)
        left.addLayout(form)

        # Chamber rows inside a scroll area
        chambers_scroll = QScrollArea()
        chambers_scroll.setWidgetResizable(True)
        chambers_scroll.setMaximumHeight(160)
        inner_w = QWidget()
        self._rows_layout = QVBoxLayout(inner_w)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(3)
        chambers_scroll.setWidget(inner_w)
        left.addWidget(QLabel("Chambers:"))
        left.addWidget(chambers_scroll)

        # Add chamber button
        self._add_chamber_btn = QPushButton("+ Add Chamber")
        self._add_chamber_btn.clicked.connect(self._on_add_chamber)
        left.addWidget(self._add_chamber_btn)

        # Touch group (node, sensor count, sensor→chamber mapping)
        left.addWidget(self._build_touch_group())
        left.addStretch()

        # ---- RIGHT COLUMN ----
        # Layout / grid editor — tallest piece, gets its own column.
        right.addWidget(self._build_layout_group(), stretch=1)

        # ---- Action buttons (full width, pinned at the bottom) ----
        btn_row = QHBoxLayout()
        self._test_btn   = QPushButton("Test Actuators")
        self._delete_btn = QPushButton("Delete Skin")
        self._cancel_btn = QPushButton("Cancel")
        self._save_btn   = QPushButton("Save")
        self._test_btn.setEnabled(gateway.is_connected)
        self._delete_btn.setVisible(not is_new)
        btn_row.addWidget(self._test_btn)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        # Pre-populate from saved skin config.
        self._populate_from_cfg(self._load_skin_cfg())

        self._test_btn.clicked.connect(self._on_test)
        self._delete_btn.clicked.connect(self._on_delete)
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn.clicked.connect(self._on_save)

    # ------------------------------------------------------------------
    # Pre-population (load saved skin into widgets)
    # ------------------------------------------------------------------

    def _populate_from_cfg(self, skin_cfg: dict) -> None:
        """Drive every widget from a saved skin entry. Split into focused
        helpers so ``__init__`` stays simple."""
        self._skin_id_edit.setText(skin_cfg.get("skin_id", ""))
        self._name_edit.setText(skin_cfg.get("name", ""))
        self._populate_chambers(skin_cfg)
        touch_cfg = skin_cfg.get("touch") or {}
        self._populate_touch_header(touch_cfg)
        self._populate_shape(skin_cfg.get("shape", "rect"))
        self._populate_dims(skin_cfg.get("grid") or {}, touch_cfg.get("grid"))
        self._grid.set_chamber_grid(skin_cfg.get("chamber_grid"))
        self._grid.set_sensor_grid(touch_cfg.get("sensor_grid"))
        self._rebuild_palette()

    def _populate_chambers(self, skin_cfg: dict) -> None:
        for ch in skin_cfg.get("chambers", []):
            self._add_row(
                mac=ch.get("mac", ""),
                slot=int(ch.get("slot", 0)),
                max_pressure=float(ch.get("max_pressure", _DEFAULT_MAX_KPA)),
            )
        if not self._rows:
            self._add_row()  # start with one empty row

    def _populate_touch_header(self, touch_cfg: dict) -> None:
        touch_mac = touch_cfg.get("node_mac", "")
        idx = self._touch_mac_combo.findData(touch_mac)
        if idx >= 0:
            self._touch_mac_combo.setCurrentIndex(idx)
        self._sensor_count_spin.setValue(int(touch_cfg.get("sensor_count", 4)))

    def _populate_shape(self, shape: str) -> None:
        if shape == "round":
            self._shape_round_btn.setChecked(True)
        else:
            self._shape_rect_btn.setChecked(True)
        self._grid.set_shape(shape)

    def _populate_dims(self, grid_cfg: dict,
                       sensor_grid_cfg: dict | None) -> None:
        """Apply per-layer dimensions from YAML. Chamber dims come from
        ``grid``; sensor dims from ``touch.grid`` (falling back to chamber
        dims for legacy skins without a separate sensor grid)."""
        ch_cols = int(grid_cfg.get("cols", 8))
        ch_rows = int(grid_cfg.get("rows", 4))
        sn = sensor_grid_cfg or grid_cfg
        sn_cols = int(sn.get("cols", ch_cols))
        sn_rows = int(sn.get("rows", ch_rows))
        self._grid.set_dimensions(ch_cols, ch_rows, layer="chamber")
        self._grid.set_dimensions(sn_cols, sn_rows, layer="sensor")
        # Show the chamber dims first (mode starts on chamber).
        self._cols_spin.blockSignals(True)
        self._rows_spin.blockSignals(True)
        self._cols_spin.setValue(ch_cols)
        self._rows_spin.setValue(ch_rows)
        self._cols_spin.blockSignals(False)
        self._rows_spin.blockSignals(False)

    # ------------------------------------------------------------------
    # Template handling
    # ------------------------------------------------------------------

    def _build_template_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Template:"))
        self._template_combo = QComboBox()
        self._template_combo.addItem(_NONE_LABEL, userData=None)
        for tpl in self._db.get_all_skin_templates():
            self._template_combo.addItem(f"{tpl.name} [{tpl.template_id}]",
                                         userData=tpl.template_id)
        self._template_combo.setMinimumWidth(220)
        row.addWidget(self._template_combo, stretch=1)

        self._apply_tpl_btn = QPushButton("Apply")
        self._apply_tpl_btn.clicked.connect(self._on_apply_template)
        row.addWidget(self._apply_tpl_btn)

        self._save_tpl_btn = QPushButton("Save as template…")
        self._save_tpl_btn.clicked.connect(self._on_save_template)
        row.addWidget(self._save_tpl_btn)
        return row

    def _on_skin_id_edited(self, _text: str) -> None:
        self._skin_id_user_edited = True

    def _on_apply_template(self) -> None:
        template_id = self._template_combo.currentData()
        if not template_id:
            QMessageBox.information(
                self, "Apply Template",
                "Pick a template from the dropdown first.",
            )
            return
        tpl = self._db.get_skin_template(template_id)
        if tpl is None:
            return
        self._apply_template(tpl)

    def _apply_template(self, tpl: SkinTemplate) -> None:
        """Fill the dialog widgets from a template.

        Chamber rows are recreated empty (no MAC) with the template's pressure
        defaults — the user still has to pick MACs for the chambers since those
        are per-instance. Skin ID auto-fills as ``{template_name}-{N}`` (e.g.
        ``belly-3``) unless the user has already typed something.
        """
        # Reset chamber rows to match template's chamber_count. Copy with a
        # slice before iterating because _remove_row mutates self._rows.
        for row in self._rows[:]:
            self._remove_row(row)
        for _ in range(max(1, tpl.chamber_count)):
            self._add_row(max_pressure=tpl.default_max_pressure)

        # Grid + chamber_grid.
        cols = int(tpl.grid.get("cols", self._cols_spin.value()))
        rows = int(tpl.grid.get("rows", self._rows_spin.value()))
        self._cols_spin.setValue(cols)
        self._rows_spin.setValue(rows)
        self._grid.set_dimensions(cols, rows)
        if tpl.chamber_grid:
            self._grid.set_chamber_grid(tpl.chamber_grid)
        # Touch defaults.
        self._sensor_count_spin.setValue(max(0, tpl.sensor_count))
        if tpl.sensor_grid:
            self._grid.set_sensor_grid(tpl.sensor_grid)

        self._rebuild_palette()

        # Auto-fill skin_id only if the user hasn't typed one yet AND we're
        # creating a new skin (not editing an existing one).
        if self._skin_index < 0 and not self._skin_id_user_edited:
            self._skin_id_edit.setText(self._next_skin_id_for(tpl))

    def _next_skin_id_for(self, tpl: SkinTemplate) -> str:
        """Return ``{tpl.name}-{N}`` with N = (existing skins matching prefix) + 1."""
        prefix = (tpl.name or tpl.template_id).strip().lower().replace(" ", "_") or "skin"
        siblings = self._sibling_skins() + ([] if self._skin_index < 0
                                            else [self._load_skin_cfg()])
        used_nums: list[int] = []
        for sk in siblings:
            sid = str(sk.get("skin_id", "")).lower()
            if sid.startswith(prefix + "-"):
                try:
                    used_nums.append(int(sid.rsplit("-", 1)[1]))
                except ValueError:
                    pass
        n = (max(used_nums) + 1) if used_nums else 1
        return f"{prefix}-{n}"

    def _on_save_template(self) -> None:
        if self._db is None:
            return
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Save Template",
            "Template name (e.g. 'Belly 4-chamber'):",
        )
        if not ok or not name.strip():
            return
        # Snapshot the current dialog state into a template.
        max_p = 0.0
        min_p = 0.0
        if self._rows:
            _, _, max_p = self._rows[0].get_values()
            # min_pressure is per-chamber on the row but we collect default
            # from the first row (typical use is symmetric pressure caps).
            min_p = 0.0  # spinbox not exposed yet on _ChamberRow
        template = SkinTemplate(
            template_id=self._db.next_skin_template_id(),
            name=name.strip(),
            chamber_count=len(self._rows),
            default_max_pressure=float(max_p) if max_p else 8.0,
            default_min_pressure=float(min_p),
            grid={"cols": self._grid.cols(), "rows": self._grid.rows()},
            chamber_grid=self._grid.chamber_grid(),
            sensor_count=int(self._sensor_count_spin.value()),
            sensor_grid=self._grid.sensor_grid(),
        )
        self._db.save_skin_template(template)
        # Refresh the dropdown and select the new template.
        self._template_combo.addItem(f"{template.name} [{template.template_id}]",
                                     userData=template.template_id)
        self._template_combo.setCurrentIndex(self._template_combo.count() - 1)
        QMessageBox.information(
            self, "Template Saved",
            f"Template '{template.name}' saved as {template.template_id}.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _robot_nodes(self) -> list[dict]:
        robots = self._settings.data.get("robots", {})
        robots_list = robots.get(_YAML_KEY[self._robot_type], [])
        if 0 <= self._robot_index < len(robots_list):
            return robots_list[self._robot_index].get("nodes", [])
        return []

    def _node_macs(self) -> list[str]:
        return [n["mac"] for n in self._robot_nodes() if n.get("mac")]

    def _node_max_slots(self) -> dict[str, int]:
        return {n["mac"]: int(n.get("max_slots", 3)) for n in self._robot_nodes() if n.get("mac")}

    def _load_skin_cfg(self) -> dict:
        if self._skin_index < 0:
            return {}
        robots = self._settings.data.get("robots", {})
        robots_list = robots.get(_YAML_KEY[self._robot_type], [])
        if 0 <= self._robot_index < len(robots_list):
            skins = robots_list[self._robot_index].get("skins", [])
            if 0 <= self._skin_index < len(skins):
                return skins[self._skin_index]
        return {}

    def _sibling_skins(self) -> list[dict]:
        robots = self._settings.data.get("robots", {})
        robots_list = robots.get(_YAML_KEY[self._robot_type], [])
        if not (0 <= self._robot_index < len(robots_list)):
            return []
        all_skins = robots_list[self._robot_index].get("skins", [])
        return [sc for i, sc in enumerate(all_skins) if i != self._skin_index]

    # ------------------------------------------------------------------
    # Chamber rows
    # ------------------------------------------------------------------

    def _add_row(
        self,
        mac: str = "",
        slot: int = 0,
        max_pressure: float = _DEFAULT_MAX_KPA,
    ) -> None:
        if len(self._rows) >= _MAX_CHAMBERS:
            return
        macs      = self._node_macs()
        max_slots = self._node_max_slots()
        row = _ChamberRow(macs, max_slots, mac=mac, slot=slot, max_pressure=max_pressure)
        row.remove_btn.clicked.connect(lambda: self._remove_row(row))
        self._rows.append(row)
        self._rows_layout.addWidget(row)
        self._add_chamber_btn.setEnabled(len(self._rows) < _MAX_CHAMBERS)

    def _remove_row(self, row: _ChamberRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._add_chamber_btn.setEnabled(len(self._rows) < _MAX_CHAMBERS)

    def _on_add_chamber(self) -> None:
        self._add_row()
        self._rebuild_palette()

    # ------------------------------------------------------------------
    # Touch + grid widgets
    # ------------------------------------------------------------------

    def _magnet_macs(self) -> list[str]:
        return [n["mac"] for n in self._robot_nodes()
                if n.get("node_type") == "node_magnet_sensor" and n.get("mac")]

    def _build_touch_group(self) -> QGroupBox:
        group = QGroupBox("Touch sensors (optional)")
        outer = QVBoxLayout(group)

        form = QFormLayout()
        outer.addLayout(form)

        self._touch_mac_combo = QComboBox()
        self._touch_mac_combo.addItem(_NONE_LABEL, userData="")
        for mac in self._magnet_macs():
            self._touch_mac_combo.addItem(mac, userData=mac)
        self._touch_mac_combo.currentTextChanged.connect(
            lambda _t: self._rebuild_palette()
        )
        form.addRow("Touch node:", self._touch_mac_combo)

        self._sensor_count_spin = QSpinBox()
        self._sensor_count_spin.setRange(1, 16)
        self._sensor_count_spin.setValue(4)
        self._sensor_count_spin.valueChanged.connect(self._on_sensor_count_changed)
        form.addRow("Sensors:", self._sensor_count_spin)

        # Note: Sensor → Chamber routing is now configured per activity,
        # not per skin. Activities provide a sensor_to_chamber param that
        # overrides the skin's default, allowing the same physical skin to be
        # reused with different routings in different activities.
        outer.addWidget(QLabel("Note: Sensor → Chamber routing is configured in activity presets"))

        return group

    def _on_sensor_count_changed(self, _value: int) -> None:
        self._rebuild_palette()

    def _build_layout_group(self) -> QGroupBox:
        group = QGroupBox("Skin layout (chambers + zones)")
        v = QVBoxLayout(group)

        # Shape selector — applies to BOTH layers; the grid editor masks
        # off-circle cells when ``round`` is picked. Put into its own
        # QButtonGroup so it stays independent from the Mode radios below
        # (Qt's default exclusivity would otherwise lump them together).
        shape_row = QHBoxLayout()
        shape_row.addWidget(QLabel("Shape:"))
        self._shape_rect_btn  = QRadioButton("Rectangle")
        self._shape_round_btn = QRadioButton("Round")
        self._shape_rect_btn.setChecked(True)
        self._shape_group = QButtonGroup(self)
        self._shape_group.setExclusive(True)
        self._shape_group.addButton(self._shape_rect_btn)
        self._shape_group.addButton(self._shape_round_btn)
        shape_row.addWidget(self._shape_rect_btn)
        shape_row.addWidget(self._shape_round_btn)
        shape_row.addStretch()
        v.addLayout(shape_row)

        # Per-layer dimensions — the Mode buttons come FIRST so the user
        # picks what they're editing, then sees the dims for that layer.
        # Switching mode swaps the spinboxes in and out so chamber and
        # sensor grids can have different resolutions on the same skin
        # (e.g. 4×2 chambers, 8×4 sensors). Separate QButtonGroup so it
        # doesn't fight the Shape radios.
        size_row = QHBoxLayout()

        size_row.addWidget(QLabel("Mode:"))
        self._mode_chamber = QRadioButton("Chambers")
        self._mode_sensor  = QRadioButton("Touch zones")
        self._mode_chamber.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self._mode_chamber)
        self._mode_group.addButton(self._mode_sensor)
        size_row.addWidget(self._mode_chamber)
        size_row.addWidget(self._mode_sensor)
        size_row.addSpacing(12)

        self._layer_dims_label = QLabel("Chamber grid:")
        size_row.addWidget(self._layer_dims_label)
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(1, 32)
        self._cols_spin.setValue(8)
        self._cols_spin.setSuffix(" cols")
        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(1, 16)
        self._rows_spin.setValue(4)
        self._rows_spin.setSuffix(" rows")
        size_row.addWidget(self._cols_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self._rows_spin)
        size_row.addStretch()
        v.addLayout(size_row)

        self._palette_row = QHBoxLayout()
        self._palette_row.addWidget(QLabel("Paint:"))
        self._palette_group = QButtonGroup(self)
        self._palette_group.setExclusive(True)
        v.addLayout(self._palette_row)

        hint = QLabel(
            "<i>Left-click (or drag) to paint with the selected colour. "
            "Right-click (or drag) to erase.</i>"
        )
        hint.setStyleSheet("color: #566573; font-size: 10px;")
        v.addWidget(hint)

        self._grid = SkinGridEditor(cols=8, rows=4)
        v.addWidget(self._grid, stretch=1)

        # Hook up — spinboxes always edit the ACTIVE layer's dims.
        self._cols_spin.valueChanged.connect(self._on_dims_changed)
        self._rows_spin.valueChanged.connect(self._on_dims_changed)
        self._mode_chamber.toggled.connect(self._on_mode_changed)
        self._mode_sensor.toggled.connect(self._on_mode_changed)
        self._shape_rect_btn.toggled.connect(self._on_shape_changed)
        self._shape_round_btn.toggled.connect(self._on_shape_changed)
        return group

    def _on_dims_changed(self, _value: int) -> None:
        """Spinbox edited → resize the layer currently being edited."""
        layer = "chamber" if self._mode_chamber.isChecked() else "sensor"
        self._grid.set_dimensions(self._cols_spin.value(),
                                  self._rows_spin.value(),
                                  layer=layer)

    def _on_mode_changed(self, _checked: bool) -> None:
        layer = "chamber" if self._mode_chamber.isChecked() else "sensor"
        self._grid.set_layer(layer)
        # Swap spinboxes to show the new layer's dims without re-firing
        # ``set_dimensions`` on every load.
        self._cols_spin.blockSignals(True)
        self._rows_spin.blockSignals(True)
        if layer == "chamber":
            self._cols_spin.setValue(self._grid.chamber_cols())
            self._rows_spin.setValue(self._grid.chamber_rows())
            self._layer_dims_label.setText("Chamber grid:")
        else:
            self._cols_spin.setValue(self._grid.sensor_cols())
            self._rows_spin.setValue(self._grid.sensor_rows())
            self._layer_dims_label.setText("Sensor grid:")
        self._cols_spin.blockSignals(False)
        self._rows_spin.blockSignals(False)
        self._rebuild_palette()

    def _on_shape_changed(self, _checked: bool) -> None:
        shape = "round" if self._shape_round_btn.isChecked() else "rect"
        self._grid.set_shape(shape)

    def _apply_layout_and_touch(self, skin_entry: dict) -> None:
        """Persist grid + touch fields onto ``skin_entry`` (only when used)."""
        chamber_grid = self._grid.chamber_grid()
        sensor_grid  = self._grid.sensor_grid()
        chambers_painted = any(v >= 0 for row in chamber_grid for v in row)
        sensors_painted  = any(v >= 0 for row in sensor_grid for v in row)

        # Shape is persisted whenever it differs from the default ("rect").
        if self._grid.shape() == "round":
            skin_entry["shape"] = "round"
        else:
            skin_entry.pop("shape", None)

        # Chamber grid dims always saved when anything is painted (chambers
        # or sensors) so the renderer knows the layout to use.
        if chambers_painted or sensors_painted:
            skin_entry["grid"] = {"cols": self._grid.chamber_cols(),
                                  "rows": self._grid.chamber_rows()}
        if chambers_painted:
            skin_entry["chamber_grid"] = chamber_grid

        touch_mac = self._touch_mac_combo.currentData() or ""
        # Persist whatever the user has filled in. The touch block is written
        # even without a node_mac so painted zones survive across saves while
        # the user is still wiring things up. Activities just won't act on
        # touch events until ``node_mac`` is set.
        if not (touch_mac or sensors_painted):
            return
        touch_entry: dict = {
            "sensor_count": int(self._sensor_count_spin.value()),
        }
        if touch_mac:
            touch_entry["node_mac"] = touch_mac
        # Only persist a separate sensor grid dimension when it actually
        # differs from the chamber grid — keeps existing YAMLs untouched if
        # the user never changed the sensor resolution.
        sn_cols, sn_rows = self._grid.sensor_cols(), self._grid.sensor_rows()
        if (sn_cols, sn_rows) != (self._grid.chamber_cols(),
                                  self._grid.chamber_rows()):
            touch_entry["grid"] = {"cols": sn_cols, "rows": sn_rows}
        if sensors_painted:
            touch_entry["sensor_grid"] = sensor_grid
        skin_entry["touch"] = touch_entry

    def _rebuild_palette(self) -> None:
        """Refresh the paint-target buttons for the active layer."""
        # Remove all palette buttons from both the layout and the button group.
        # Widgets must be hidden immediately after takeAt — deleteLater() defers
        # actual destruction, so an orphaned (still-visible) old button would
        # overlap the newly-added one with the same label.
        while self._palette_row.count() > 1:
            item = self._palette_row.takeAt(1)
            w = item.widget()
            if w is not None:
                self._palette_group.removeButton(w)
                w.hide()
                w.deleteLater()

        def add_btn(label: str, value: int) -> None:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedSize(60, 24)
            self._palette_group.addButton(btn, id=value)
            btn.clicked.connect(lambda _c, v=value: self._grid.set_paint_target(v))
            self._palette_row.addWidget(btn)

        prefix = "C" if self._mode_chamber.isChecked() else "S"
        count = (len(self._rows) if self._mode_chamber.isChecked()
                 else self._sensor_count_spin.value())
        for idx in range(count):
            add_btn(f"{prefix}{idx}", idx)
        self._palette_row.addStretch()

        # Auto-select the first palette button so a click on the grid always
        # paints something. Use setChecked + set_paint_target directly — calling
        # btn.click() on a checkable button toggles its state and would undo
        # the setChecked.
        first_btn = self._palette_group.button(0)
        if first_btn is not None:
            first_btn.setChecked(True)
            self._grid.set_paint_target(0)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_test(self) -> None:
        macs = list({row.get_values()[0] for row in self._rows if row.get_values()[0]})
        if not macs:
            QMessageBox.warning(self, "Test Actuators", "Configure at least one chamber first.")
            return
        from src.gui.test_actuators_dialog import TestActuatorsDialog
        skin_id   = self._skin_id_edit.text().strip() or "preview"
        chambers  = [{"mac": m, "slot": s} for m, s, _ in [r.get_values() for r in self._rows]]
        # TestActuatorsDialog expects the old skin_cfgs format; build a compatible dict
        skin_cfgs = [{"skin_id": skin_id, "slots": [c["slot"] for c in chambers]}]
        dlg = TestActuatorsDialog(
            mac=macs[0],
            skin_cfgs=skin_cfgs,
            gateway=self._gateway,
            parent=self,
        )
        dlg.exec()

    # ------------------------------------------------------------------
    # Save validation helpers
    # ------------------------------------------------------------------

    def _collect_chambers(self) -> list[dict] | None:
        """Read each chamber row into a list of dicts. Returns None and
        warns if any row is missing its MAC."""
        chambers: list[dict] = []
        for row in self._rows:
            mac, slot, max_p = row.get_values()
            if not mac:
                QMessageBox.warning(
                    self, _MISSING_FIELD_TITLE,
                    "Each chamber must have a node MAC selected.",
                )
                return None
            chambers.append({"mac": mac, "slot": slot, "max_pressure": max_p})
        return chambers

    def _validate_no_duplicate_chambers(self, chambers: list[dict]) -> bool:
        seen: set[tuple[str, int]] = set()
        for ch in chambers:
            key = (ch["mac"], ch["slot"])
            if key in seen:
                QMessageBox.warning(
                    self, "Duplicate Chamber",
                    f"Slot {ch['slot']} on {ch['mac']} is used more than "
                    "once in this skin.",
                )
                return False
            seen.add(key)
        return True

    def _validate_no_sibling_conflicts(self, chambers: list[dict]) -> bool:
        used_by_siblings = {
            (ch.get("mac"), ch.get("slot"))
            for sk in self._sibling_skins()
            for ch in sk.get("chambers", [])
        }
        conflicts = [ch for ch in chambers
                     if (ch["mac"], ch["slot"]) in used_by_siblings]
        if not conflicts:
            return True
        parts = [f"{c['mac']} #{c['slot']}" for c in conflicts]
        QMessageBox.warning(
            self, "Chamber Conflict",
            "These chambers are already used by another skin:\n"
            + "\n".join(parts),
        )
        return False

    def _confirm_large_pressure_change(self, chambers: list[dict]) -> bool:
        """Warn the user when any chamber's max_pressure shifted by more
        than ``_CONFIRM_DELTA``. Returns False if they cancel."""
        prev_cfg = self._load_skin_cfg()
        prev_chs = {(c.get("mac"), c.get("slot")): c
                    for c in prev_cfg.get("chambers", [])}
        big_changes: list[str] = []
        for ch in chambers:
            key = (ch["mac"], ch["slot"])
            old_max = float(prev_chs.get(key, {}).get(
                "max_pressure", _DEFAULT_MAX_KPA))
            if abs(ch["max_pressure"] - old_max) >= _CONFIRM_DELTA:
                big_changes.append(
                    f"{ch['mac']} #{ch['slot']}: "
                    f"{old_max:.1f} → {ch['max_pressure']:.1f} kPa"
                )
        if not big_changes:
            return True
        reply = QMessageBox.question(
            self, "Confirm Large Pressure Change",
            "Large max-pressure change detected:\n"
            + "\n".join(big_changes)
            + "\n\nKeep these changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _on_save(self) -> None:
        skin_id = self._skin_id_edit.text().strip()
        name    = self._name_edit.text().strip() or skin_id

        if not skin_id:
            QMessageBox.warning(self, _MISSING_FIELD_TITLE,
                                "Skin ID cannot be empty.")
            return
        if not self._rows:
            QMessageBox.warning(self, _MISSING_FIELD_TITLE,
                                "Add at least one chamber.")
            return

        chambers = self._collect_chambers()
        if chambers is None:
            return
        if not self._validate_no_duplicate_chambers(chambers):
            return
        if not self._validate_no_sibling_conflicts(chambers):
            return
        if not self._confirm_large_pressure_change(chambers):
            return

        # Only store max_pressure when it differs from the default
        for ch in chambers:
            if abs(ch["max_pressure"] - _DEFAULT_MAX_KPA) < 1e-9:
                del ch["max_pressure"]

        skin_entry: dict = {"skin_id": skin_id, "name": name, "chambers": chambers}
        self._apply_layout_and_touch(skin_entry)

        data = self._settings.data
        robots_list = (
            data.setdefault("robots", {})
            .setdefault(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            skins = robots_list[self._robot_index].setdefault("skins", [])
            if self._skin_index < 0:
                skins.append(skin_entry)
            else:
                skins[self._skin_index] = skin_entry

        self._settings.save()
        self.accept()

    def _on_delete(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm Delete", "Delete this skin? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        robots_list = (
            self._settings.data.get("robots", {})
            .get(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            skins = robots_list[self._robot_index].get("skins", [])
            if 0 <= self._skin_index < len(skins):
                skins.pop(self._skin_index)
        self._settings.save()
        self.accept()

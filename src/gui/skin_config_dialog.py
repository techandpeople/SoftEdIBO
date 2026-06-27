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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.core import skin_config as skincfg
from src.data.models import SkinTemplate
from src.gui.skin_grid_editor import SkinGridEditor
from src.gui.ui_skin_config_dialog import Ui_SkinConfigDialog
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.skin_geometry import max_organs_for

# Domain rules (node lookup, validation, persistence) live in src.core.skin_config;
# these aliases keep the dialog terse while single-sourcing the values there.
_DEFAULT_MAX_KPA   = skincfg.DEFAULT_MAX_KPA
_MAX_ALLOWED_KPA   = skincfg.MAX_ALLOWED_KPA
_DEFAULT_MIN_KPA   = skincfg.DEFAULT_MIN_KPA
_MIN_ALLOWED_KPA   = skincfg.MIN_ALLOWED_KPA
_MAX_CHAMBERS      = skincfg.MAX_CHAMBERS
_NONE_LABEL        = "(none)"
_MISSING_FIELD_TITLE = "Missing Field"
# Silicone variant that carries pluggable organs (organ editor gated on it).
_ORGAN_VARIANT     = "organ"


class _ChamberRow(QWidget):
    """A single chamber row: MAC dropdown + slot spinbox + min/max pressure spinboxes + remove."""

    def __init__(
        self,
        node_macs: list[str],
        node_max_slots: dict[str, int],
        mac: str = "",
        slot: int = 0,
        max_pressure: float = _DEFAULT_MAX_KPA,
        min_pressure: float = _DEFAULT_MIN_KPA,
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

        self._min_spin = QDoubleSpinBox()
        self._min_spin.setDecimals(1)
        self._min_spin.setSingleStep(0.1)
        # Negative allowed for vacuum-fed chambers (0% maps to this lower cap).
        self._min_spin.setRange(_MIN_ALLOWED_KPA, _MAX_ALLOWED_KPA - 0.1)
        self._min_spin.setValue(min_pressure)
        self._min_spin.setSuffix(" kPa")
        self._min_spin.setFixedWidth(75)
        self._min_spin.setWhatsThis(
            "Lowest pressure for this chamber (0% maps here). Leave at 0 for a "
            "normal chamber; set negative for one fed from a vacuum reservoir.")

        self._max_spin = QDoubleSpinBox()
        self._max_spin.setDecimals(1)
        self._max_spin.setSingleStep(0.1)
        self._max_spin.setRange(0.1, _MAX_ALLOWED_KPA)
        self._max_spin.setValue(max_pressure)
        self._max_spin.setSuffix(" kPa")
        self._max_spin.setFixedWidth(75)
        self._max_spin.setWhatsThis(
            "Highest pressure this chamber inflates to (100% maps here). The "
            "firmware also enforces its own hard safety ceiling.")

        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedWidth(24)
        self._remove_btn.setFixedHeight(24)

        hbox.addWidget(self._mac_combo, stretch=1)
        hbox.addWidget(self._slot_spin)
        hbox.addWidget(QLabel("Min:"))
        hbox.addWidget(self._min_spin)
        hbox.addWidget(QLabel("Max:"))
        hbox.addWidget(self._max_spin)
        hbox.addWidget(self._remove_btn)

    @property
    def remove_btn(self) -> QPushButton:
        return self._remove_btn

    def get_values(self) -> tuple[str, int, float, float]:
        return (
            self._mac_combo.currentText(),
            self._slot_spin.value(),
            round(self._max_spin.value(), 1),
            round(self._min_spin.value(), 1),
        )

    def _on_mac_changed(self, _mac: str) -> None:
        self._update_slot_max()

    def _update_slot_max(self) -> None:
        mac      = self._mac_combo.currentText()
        max_slot = max(0, self._node_max_slots.get(mac, 3) - 1)
        self._slot_spin.setMaximum(max_slot)
        if self._slot_spin.value() > max_slot:
            self._slot_spin.setValue(0)


class _OrganRow(QWidget):
    """One organ row: a number label + good/bad resistance spinboxes + remove."""

    removed = Signal(object)   # emits self

    def __init__(self, number: int, good_ohm: float, bad_ohm: float,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(0, 0, 0, 0)
        self._num_label = QLabel()
        hbox.addWidget(self._num_label)
        hbox.addWidget(QLabel("Good Ω:"))
        self._good_spin = self._ohm_spin(
            good_ohm, "Resistance when the GOOD variant of this organ is on.")
        hbox.addWidget(self._good_spin)
        hbox.addWidget(QLabel("Bad Ω:"))
        self._bad_spin = self._ohm_spin(
            bad_ohm, "Resistance when the BAD variant of this organ is on.")
        hbox.addWidget(self._bad_spin)
        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedSize(24, 24)
        self._remove_btn.clicked.connect(lambda: self.removed.emit(self))
        hbox.addWidget(self._remove_btn)
        self.set_number(number)

    @staticmethod
    def _ohm_spin(value: float, whats_this: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1_000_000.0)
        spin.setDecimals(0)
        spin.setSingleStep(100.0)
        spin.setValue(value)
        spin.setWhatsThis(whats_this)
        return spin

    def set_number(self, number: int) -> None:
        self._num_label.setText(f"Organ {number}:")

    def values(self) -> tuple[float, float]:
        return self._good_spin.value(), self._bad_spin.value()


class SkinConfigDialog(QDialog, Ui_SkinConfigDialog):
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
        self.setupUi(self)
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
        # Left = metadata/chambers/touch, right = layout/grid editor (tallest).
        self.columns.setStretch(0, 2)
        self.columns.setStretch(1, 3)

        # The static two-column frame (metadata form, chambers list, action
        # buttons) lives in the .ui; the dynamic groups (template / touch /
        # layout-grid editor) are built here and inserted into the columns.
        self.skin_id_edit.textEdited.connect(self._on_skin_id_edited)

        # Skin type — indexes the hardcoded geometry registry (shape + sensor
        # coordinates) and the per-type touch-gesture model. Only the types of
        # THIS robot kind are offered. When the robot has exactly one type
        # (Tree, Thymio) there is nothing to choose — preselect it, hide the row.
        from src.hardware.skin_geometry import known_skin_variants, skin_types_for
        types = skin_types_for(self._robot_type)
        single_type = len(types) == 1
        if not single_type:
            self.skin_type_combo.addItem("(none)", "")
        for st in types:
            self.skin_type_combo.addItem(st, st)
        self.skin_type_combo.currentIndexChanged.connect(self._on_skin_type_changed)
        # Silicone variant — orthogonal to the shape (same shape, different
        # silicone format / chamber sizes). Fed to the touch ML as a feature.
        self.skin_variant_combo.addItem("(none)", "")
        for sv in known_skin_variants():
            self.skin_variant_combo.addItem(sv, sv)
        # Organs only exist on the "organ" silicone variant — gate the organ
        # editor on it (the other variants have no pluggable organ slots).
        self.skin_variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        if single_type:
            try:
                self.form.setRowVisible(self.skin_type_combo, False)
            except (AttributeError, TypeError):
                self.skin_type_combo.setVisible(False)

        # ---- LEFT COLUMN dynamic groups ----
        # Template row — pick a saved layout to auto-fill, or save the current
        # configuration as a new template. Inserted above the metadata form.
        if self._db is not None:
            self.left_layout.insertLayout(0, self._build_template_row())
        # Touch group + trailing stretch, below the chambers.
        self.left_layout.addWidget(self._build_touch_group())
        self.left_layout.addStretch()

        # ---- RIGHT COLUMN ----
        # Layout / grid editor — tallest piece, gets its own column.
        self.right_layout.addWidget(self._build_layout_group(), stretch=1)
        self._organ_group = self._build_organ_group()
        self.right_layout.addWidget(self._organ_group)

        # ---- Action buttons (defined in the .ui) ----
        self.test_btn.setEnabled(gateway.is_connected)
        self.calib_btn.setEnabled(gateway.is_connected)
        self.delete_btn.setVisible(not is_new)
        self.add_chamber_btn.clicked.connect(self._on_add_chamber)

        # Pre-populate from saved skin config.
        self._populate_from_cfg(self._load_skin_cfg())

        self.test_btn.clicked.connect(self._on_test)
        self.calib_btn.clicked.connect(self._on_calibrate)
        self.delete_btn.clicked.connect(self._on_delete)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn.clicked.connect(self._on_save)

    # ------------------------------------------------------------------
    # Pre-population (load saved skin into widgets)
    # ------------------------------------------------------------------

    def _populate_from_cfg(self, skin_cfg: dict) -> None:
        """Drive every widget from a saved skin entry. Split into focused
        helpers so ``__init__`` stays simple."""
        self.skin_id_edit.setText(skin_cfg.get("skin_id", ""))
        # Select the saved skin_type by data; its registry geometry then drives
        # the shape (via _on_skin_type_changed). Fall back to the stored shape
        # only when the skin has no (registered) type.
        st_idx = self.skin_type_combo.findData(skin_cfg.get("skin_type", ""))
        self.skin_type_combo.setCurrentIndex(st_idx if st_idx >= 0 else 0)
        sv_idx = self.skin_variant_combo.findData(skin_cfg.get("skin_variant", ""))
        self.skin_variant_combo.setCurrentIndex(sv_idx if sv_idx >= 0 else 0)
        self._populate_chambers(skin_cfg)
        touch_cfg = skin_cfg.get("touch") or {}
        self._populate_touch_header(touch_cfg)
        if not self.skin_type_combo.currentData():
            self._populate_shape(skin_cfg.get("shape", "rect"))
        else:
            self._on_skin_type_changed()
        self._populate_dims(skin_cfg.get("grid") or {}, touch_cfg.get("grid"))
        self._grid.set_chamber_grid(skin_cfg.get("chamber_grid"))
        self._grid.set_sensor_grid(touch_cfg.get("sensor_grid"))
        self._load_organs(skin_cfg.get("organs"))
        self._sync_organ_geometry()
        self._on_variant_changed()
        self._rebuild_palette()

    def _populate_chambers(self, skin_cfg: dict) -> None:
        for ch in skin_cfg.get("chambers", []):
            self._add_row(
                mac=ch.get("mac", ""),
                slot=int(ch.get("slot", 0)),
                max_pressure=float(ch.get("max_pressure", _DEFAULT_MAX_KPA)),
                min_pressure=float(ch.get("min_pressure", _DEFAULT_MIN_KPA)),
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
        dims for legacy skins without a separate sensor grid). A brand-new skin
        (no saved grid) starts at 3×3."""
        d_cols, d_rows = (3, 3) if self._skin_index < 0 else (8, 4)
        ch_cols = int(grid_cfg.get("cols", d_cols))
        ch_rows = int(grid_cfg.get("rows", d_rows))
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
            self._add_row(max_pressure=tpl.default_max_pressure,
                          min_pressure=tpl.default_min_pressure)

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
            self.skin_id_edit.setText(self._next_skin_id_for(tpl))

    def _next_skin_id_for(self, tpl: SkinTemplate) -> str:
        """Return ``{tpl.name}-{N}`` with N one past the highest existing match."""
        siblings = self._sibling_skins() + ([] if self._skin_index < 0
                                            else [self._load_skin_cfg()])
        ids = [sk.get("skin_id", "") for sk in siblings]
        return skincfg.next_skin_id(tpl.name or tpl.template_id, ids)

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
            # Collect the template defaults from the first row (typical use is
            # uniform pressure caps across a skin's chambers).
            _, _, max_p, min_p = self._rows[0].get_values()
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
        return skincfg.robot_nodes(
            self._settings.data, self._robot_type, self._robot_index)

    def _node_macs(self) -> list[str]:
        # Chambers live only on actuator boards. Sensor-only boards
        # (node_magnet_sensor) have nothing to inflate, so they must not
        # be selectable as a chamber's node.
        return skincfg.actuator_macs(
            self._settings.data, self._robot_type, self._robot_index)

    def _node_max_slots(self) -> dict[str, int]:
        return skincfg.node_max_slots(
            self._settings.data, self._robot_type, self._robot_index)

    def _load_skin_cfg(self) -> dict:
        return skincfg.load_skin_cfg(
            self._settings.data, self._robot_type, self._robot_index,
            self._skin_index)

    def _sibling_skins(self) -> list[dict]:
        return skincfg.sibling_skins(
            self._settings.data, self._robot_type, self._robot_index,
            self._skin_index)

    # ------------------------------------------------------------------
    # Chamber rows
    # ------------------------------------------------------------------

    def _add_row(
        self,
        mac: str = "",
        slot: int = 0,
        max_pressure: float = _DEFAULT_MAX_KPA,
        min_pressure: float = _DEFAULT_MIN_KPA,
    ) -> None:
        if len(self._rows) >= _MAX_CHAMBERS:
            return
        macs      = self._node_macs()
        max_slots = self._node_max_slots()
        row = _ChamberRow(macs, max_slots, mac=mac, slot=slot,
                          max_pressure=max_pressure, min_pressure=min_pressure)
        row.remove_btn.clicked.connect(lambda: self._remove_row(row))
        self._rows.append(row)
        self.rows_layout.addWidget(row)
        self.add_chamber_btn.setEnabled(len(self._rows) < _MAX_CHAMBERS)

    def _remove_row(self, row: _ChamberRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self.add_chamber_btn.setEnabled(len(self._rows) < _MAX_CHAMBERS)

    def _on_add_chamber(self) -> None:
        self._add_row()
        self._rebuild_palette()

    # ------------------------------------------------------------------
    # Touch + grid widgets
    # ------------------------------------------------------------------

    def _magnet_macs(self) -> list[str]:
        return skincfg.magnet_macs(
            self._settings.data, self._robot_type, self._robot_index)

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

        # Shape is now DERIVED from the chosen skin_type (geometry registry),
        # not freely selectable — so this robot only ever shows its own shapes.
        # The radios are kept (other code reads/writes them + the grid mask)
        # but hidden; ``_on_skin_type_changed`` drives them from the registry.
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
        self._shape_row_widget = QWidget()
        self._shape_row_widget.setLayout(shape_row)
        self._shape_row_widget.setVisible(False)
        v.addWidget(self._shape_row_widget)

        # Per-layer dimensions — the Mode buttons come FIRST so the user
        # picks what they're editing, then sees the dims for that layer.
        # Switching mode swaps the spinboxes in and out so chamber and
        # sensor grids can have different resolutions on the same skin
        # (e.g. 4×2 chambers, 8×4 sensors). Separate QButtonGroup so it
        # doesn't fight the Shape radios.
        size_row = QHBoxLayout()

        # Sensors are hardcoded per skin_type in the geometry registry — they
        # are NOT painted here. The grid editor is chamber-only, so the
        # layer-switch ("Mode") is built (other code reads the radios) but
        # hidden; the chamber layer is always active.
        self._mode_label = QLabel("Mode:")
        size_row.addWidget(self._mode_label)
        self._mode_chamber = QRadioButton("Chambers")
        self._mode_sensor  = QRadioButton("Touch zones")
        self._mode_chamber.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self._mode_chamber)
        self._mode_group.addButton(self._mode_sensor)
        size_row.addWidget(self._mode_chamber)
        size_row.addWidget(self._mode_sensor)
        for _w in (self._mode_label, self._mode_chamber, self._mode_sensor):
            _w.setVisible(False)
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

    def _build_organ_group(self) -> QGroupBox:
        """Simple list of the pluggable organs on this skin (hospital study):
        one row per organ with its good/bad resistance. The monitor shows each
        organ as a numbered dot (green=good, red=bad, empty=absent) beside the
        skin grid. Only the organ silicone variant has organs."""
        group = QGroupBox("Organs (optional)")
        group.setWhatsThis(
            "List the pluggable organs on this skin — one row per organ with the "
            "resistance of its good and bad variants. In the monitor each organ "
            "shows as a numbered dot (green = good, red = bad, empty = absent), "
            "inferred from the skin's organ-circuit resistance. Choose good/bad "
            "resistances well separated so different organ combinations don't "
            "overlap. Available only on the 'organ' skin variant.")
        v = QVBoxLayout(group)
        v.addWidget(QLabel(
            "<i>Each organ is a numbered dot in the monitor: "
            "green = good, red = bad, empty outline = absent.</i>"))

        self._organ_rows: list[_OrganRow] = []
        self._organ_rows_layout = QVBoxLayout()
        v.addLayout(self._organ_rows_layout)

        self._add_organ_btn = QPushButton("+ Add organ")
        self._add_organ_btn.setWhatsThis("Add another organ to this skin.")
        self._add_organ_btn.clicked.connect(self._on_add_organ)
        v.addWidget(self._add_organ_btn)
        return group

    def _on_add_organ(self) -> None:
        cap = max_organs_for(self.skin_type_combo.currentData())
        if len(self._organ_rows) >= cap:
            QMessageBox.information(
                self, "Organ limit",
                f"This skin type allows at most {cap} organ(s).")
            return
        self._add_organ_row()

    def _add_organ_row(self, good_ohm: float = 1500.0,
                       bad_ohm: float = 4700.0) -> None:
        row = _OrganRow(len(self._organ_rows) + 1, good_ohm, bad_ohm)
        row.removed.connect(self._remove_organ_row)
        self._organ_rows.append(row)
        self._organ_rows_layout.addWidget(row)
        self._refresh_organ_cap()

    def _remove_organ_row(self, row: "_OrganRow") -> None:
        if row not in self._organ_rows:
            return
        self._organ_rows.remove(row)
        self._organ_rows_layout.removeWidget(row)
        row.deleteLater()
        for i, r in enumerate(self._organ_rows):
            r.set_number(i + 1)
        self._refresh_organ_cap()

    def _load_organs(self, organs: list[dict] | None) -> None:
        """Replace the organ rows from a saved skin entry."""
        for row in list(self._organ_rows):
            self._remove_organ_row(row)
        for o in organs or []:
            self._add_organ_row(float(o.get("good_ohm", 0) or 0),
                                float(o.get("bad_ohm", 0) or 0))

    def _organs_from_rows(self) -> list[dict]:
        """Collect the organ rows as ``{id, good_ohm, bad_ohm}`` (id = order)."""
        out: list[dict] = []
        for i, row in enumerate(self._organ_rows):
            good, bad = row.values()
            out.append({"id": f"organ-{i + 1}",
                        "good_ohm": good, "bad_ohm": bad})
        return out

    def _on_variant_changed(self, _index: int = 0) -> None:
        """Organs only exist on the ``organ`` silicone variant — enable the
        organ list only then."""
        is_organ = self.skin_variant_combo.currentData() == _ORGAN_VARIANT
        self._organ_group.setEnabled(is_organ)
        self._organ_group.setToolTip(
            "" if is_organ
            else "Organs are only available on the 'organ' skin variant.")

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
        self._sync_organ_geometry()

    def _on_skin_type_changed(self, _index: int = 0) -> None:
        """Drive the grid's outline + aspect ratio from the chosen skin type's
        registry geometry (square vs rectangle vs round/triangle/'D')."""
        from src.hardware.skin_geometry import geometry_for
        geo = geometry_for(self.skin_type_combo.currentData())
        if geo is None:
            self._grid.set_geometry("rect", None)
            self._shape_rect_btn.setChecked(True)
            self._sensor_count_spin.setEnabled(True)   # legacy skin: editable
            self._sensor_count_spin.setToolTip("")
            self._sync_organ_geometry()
            return
        self._grid.set_geometry(geo.shape, geo.size_mm)
        # Sensor count is a registry constant for this type — show it, read-only.
        self._sensor_count_spin.setValue(geo.sensor_count)
        self._sensor_count_spin.setEnabled(False)
        self._sensor_count_spin.setToolTip(
            "Fixed by the skin type (geometry registry).")
        # Keep the (hidden) radios consistent for the legacy ``shape`` save.
        if geo.shape == "round":
            self._shape_round_btn.setChecked(True)
        else:
            self._shape_rect_btn.setChecked(True)
        self._sync_organ_geometry()

    def _sync_organ_geometry(self) -> None:
        """Refresh the add-button against the skin type's organ cap (the cap
        depends on the skin type, which may change after the rows are built)."""
        if hasattr(self, "_add_organ_btn"):
            self._refresh_organ_cap()

    def _refresh_organ_cap(self) -> None:
        """Enable 'Add organ' only while under the skin type's organ cap."""
        cap = max_organs_for(self.skin_type_combo.currentData())
        self._add_organ_btn.setEnabled(len(self._organ_rows) < cap)

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
        # Touch wiring only — which magnet node feeds this skin. Sensor LAYOUT
        # (count + positions) comes from the skin_type geometry registry, not a
        # drawn sensor_grid. We persist node_mac (+ sensor_count for skins
        # without a type); the sensor_grid is no longer written.
        if not touch_mac:
            return
        touch_entry: dict = {"node_mac": touch_mac,
                             "sensor_count": int(self._sensor_count_spin.value())}
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
        skin_id   = self.skin_id_edit.text().strip() or "preview"
        chambers  = [{"mac": m, "slot": s} for m, s, *_ in [r.get_values() for r in self._rows]]
        # TestActuatorsDialog expects the old skin_cfgs format; build a compatible dict
        skin_cfgs = [{"skin_id": skin_id, "slots": [c["slot"] for c in chambers]}]
        dlg = TestActuatorsDialog(
            mac=macs[0],
            skin_cfgs=skin_cfgs,
            gateway=self._gateway,
            parent=self,
        )
        dlg.exec()

    def _on_calibrate(self) -> None:
        """Calibrate fill times for just this skin's chambers.

        Builds the chamber list from the current (possibly unsaved) rows so the
        user can calibrate before committing the skin. Only actuator-typed nodes
        are included — magnet/sensor nodes have nothing to inflate."""
        node_types = {n.get("mac"): n.get("node_type")
                      for n in self._robot_nodes()}
        skin_id = self.skin_id_edit.text().strip() or "(unsaved)"
        chambers: list[dict] = []
        for row in self._rows:
            mac, slot, _max_p, _min_p = row.get_values()
            if not mac:
                continue
            if node_types.get(mac) not in ("node_direct", "node_multiplexed"):
                continue
            chambers.append({
                "robot_id": self._robot_type,
                "skin_id": skin_id,
                "mac": mac,
                "slot": int(slot),
                "node_type": node_types.get(mac),
                "fill_time_ms": None,
            })
        if not chambers:
            QMessageBox.warning(
                self, "Calibrate Fill",
                "Add at least one chamber on an actuator node first.")
            return
        from src.gui.fill_calibration_dialog import FillCalibrationDialog
        FillCalibrationDialog(self._settings, self._gateway,
                              parent=self, chambers=chambers).exec()

    # ------------------------------------------------------------------
    # Save validation helpers
    # ------------------------------------------------------------------

    def _collect_chambers(self) -> list[dict] | None:
        """Read each chamber row into a list of dicts. Returns None and
        warns if any row is missing its MAC."""
        chambers = [{"mac": m, "slot": s, "max_pressure": p, "min_pressure": mn}
                    for m, s, p, mn in (row.get_values() for row in self._rows)]
        if skincfg.find_missing_mac(chambers):
            QMessageBox.warning(
                self, _MISSING_FIELD_TITLE,
                "Each chamber must have a node MAC selected.",
            )
            return None
        return chambers

    def _validate_no_duplicate_chambers(self, chambers: list[dict]) -> bool:
        dup = skincfg.find_duplicate(chambers)
        if dup is not None:
            QMessageBox.warning(
                self, "Duplicate Chamber",
                f"Slot {dup['slot']} on {dup['mac']} is used more than "
                "once in this skin.",
            )
            return False
        return True

    def _validate_no_sibling_conflicts(self, chambers: list[dict]) -> bool:
        conflicts = skincfg.sibling_conflicts(chambers, self._sibling_skins())
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
        than the confirm delta. Returns False if they cancel."""
        prev_chambers = self._load_skin_cfg().get("chambers", [])
        changes = skincfg.large_pressure_changes(chambers, prev_chambers)
        if not changes:
            return True
        big_changes = [f"{c.mac} #{c.slot}: {c.old_kpa:.1f} → {c.new_kpa:.1f} kPa"
                       for c in changes]
        reply = QMessageBox.question(
            self, "Confirm Large Pressure Change",
            "Large max-pressure change detected:\n"
            + "\n".join(big_changes)
            + "\n\nKeep these changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _on_save(self) -> None:
        skin_id = self.skin_id_edit.text().strip()

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

        skin_type = self.skin_type_combo.currentData() or ""
        skin_variant = self.skin_variant_combo.currentData() or ""
        skin_entry = skincfg.build_skin_entry(
            skin_id, chambers, skin_type, skin_variant)
        self._apply_layout_and_touch(skin_entry)
        # Organs are only meaningful on the organ variant; drop them otherwise.
        organs = self._organs_from_rows() if skin_variant == _ORGAN_VARIANT else []
        skincfg.apply_organs(skin_entry, organs)

        skincfg.save_skin_entry(
            self._settings.data, self._robot_type, self._robot_index,
            self._skin_index, skin_entry)
        self._settings.save()
        self.accept()

    def _on_delete(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm Delete", "Delete this skin? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        skincfg.delete_skin(
            self._settings.data, self._robot_type, self._robot_index,
            self._skin_index)
        self._settings.save()
        self.accept()

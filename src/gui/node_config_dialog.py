"""Node configuration dialog.

Adds or edits a single ESP32 node entry under a robot.
A node has three attributes: MAC address, node_type, and max_slots.

Node types and their default slot counts:
    node_direct       — 3   (fixed: 3 chambers, GPIO valves, onboard pumps)
    node_multiplexed  — 12  (default; up to 16 chambers, optional shared
                              pressure/vacuum tanks via has_reservoirs)
"""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}

NODE_TYPES: dict[str, int] = {
    "node_direct": 3,
    "node_multiplexed": 12,
    "node_magnet_sensor": 4,
}


class NodeConfigDialog(QDialog):
    """Dialog for adding or editing a single node entry.

    Args:
        robot_type:  One of ``"turtle"``, ``"tree"``, or ``"thymio"``.
        robot_index: Index of the parent robot in the settings list.
        node_index:  Index of this node in the robot's ``nodes`` list,
                     or ``-1`` to add a new node.
        settings:    Application settings instance.
        parent:      Optional parent widget.
        prefill_mac: MAC address to pre-fill when adding a new node.
    """

    def __init__(
        self,
        robot_type: str,
        robot_index: int,
        node_index: int,
        settings: Settings,
        parent: QWidget | None = None,
        prefill_mac: str = "",
    ):
        super().__init__(parent)
        self._robot_type  = robot_type
        self._robot_index = robot_index
        self._node_index  = node_index
        self._settings    = settings

        is_new = node_index < 0
        self.setWindowTitle("Add Node" if is_new else "Configure Node")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        form   = QFormLayout()
        layout.addLayout(form)

        # MAC address
        self._mac_edit = QLineEdit()
        self._mac_edit.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        form.addRow("Node MAC:", self._mac_edit)

        # Node type dropdown
        self._type_combo = QComboBox()
        for nt in NODE_TYPES:
            self._type_combo.addItem(nt)
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        form.addRow("Node type:", self._type_combo)

        # Max slots spinbox
        self._slots_spin = QSpinBox()
        self._slots_spin.setRange(0, 64)
        self._slots_spin.setSuffix(" slots")
        self._slots_label = QLabel("Max slots:")
        form.addRow(self._slots_label, self._slots_spin)

        # has_reservoirs checkbox (only meaningful for node_multiplexed)
        self._reservoirs_chk = QCheckBox("Has shared pressure / vacuum reservoirs")
        self._reservoirs_label = QLabel("Reservoirs:")
        form.addRow(self._reservoirs_label, self._reservoirs_chk)
        self._reservoirs_chk.toggled.connect(self._update_tank_visibility)

        # Tank group — visible only when node_multiplexed + has_reservoirs
        self._tank_group = self._build_tank_group()
        layout.addWidget(self._tank_group)

        # Note label
        self._note_lbl = QLabel()
        self._note_lbl.setWordWrap(True)
        self._note_lbl.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self._note_lbl)

        # Buttons
        btn_layout = QVBoxLayout()
        self._save_btn   = QPushButton("Save")
        self._cancel_btn = QPushButton("Cancel")
        self._delete_btn = QPushButton("Delete Node")
        self._delete_btn.setVisible(not is_new)
        btn_layout.addWidget(self._save_btn)
        btn_layout.addWidget(self._cancel_btn)
        btn_layout.addWidget(self._delete_btn)
        layout.addLayout(btn_layout)

        # Populate from existing config
        node_cfg = self._load_node_cfg()
        self._mac_edit.setText(node_cfg.get("mac", "") or prefill_mac)
        stored_type = node_cfg.get("node_type", "node_direct")
        idx = self._type_combo.findText(stored_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        stored_slots = node_cfg.get("max_slots", NODE_TYPES.get(stored_type, 3))
        self._reservoirs_chk.setChecked(bool(node_cfg.get("has_reservoirs", False)))
        for key, spin in self._tank_spins.items():
            if key in node_cfg:
                spin.setValue(float(node_cfg[key]))
        self._on_type_changed(self._type_combo.currentText())
        if self._slots_spin.isEnabled():
            self._slots_spin.setValue(int(stored_slots))
        self._update_note()

        self._save_btn.clicked.connect(self._on_save)
        self._cancel_btn.clicked.connect(self.reject)
        self._delete_btn.clicked.connect(self._on_delete)

    # ------------------------------------------------------------------
    # Tank limit / target widgets
    # ------------------------------------------------------------------

    # (name, attribute, default, kPa range)
    # Hard caps mirror firmware's config::HARD_TANK_{MIN,MAX}_KPA (±80 kPa).
    _TANK_FIELDS = (
        ("Pressure tank min (kPa)",    "tank_pressure_min_kpa",      0.0, (-80.0, 80.0)),
        ("Pressure tank max (kPa)",    "tank_pressure_max_kpa",     50.0, (-80.0, 80.0)),
        ("Pressure tank target (kPa)", "tank_pressure_target_kpa",  25.0, (-80.0, 80.0)),
        ("Vacuum tank min (kPa)",      "tank_vacuum_min_kpa",      -50.0, (-80.0, 80.0)),
        ("Vacuum tank max (kPa)",      "tank_vacuum_max_kpa",        0.0, (-80.0, 80.0)),
        ("Vacuum tank target (kPa)",   "tank_vacuum_target_kpa",   -25.0, (-80.0, 80.0)),
    )

    def _build_tank_group(self) -> QGroupBox:
        group = QGroupBox("Reservoir limits (kPa)")
        form = QFormLayout(group)
        self._tank_spins: dict[str, QDoubleSpinBox] = {}
        for label, key, default, (lo, hi) in self._TANK_FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(" kPa")
            spin.setValue(default)
            form.addRow(label + ":", spin)
            self._tank_spins[key] = spin
        return group

    def _update_tank_visibility(self) -> None:
        is_multiplexed = self._type_combo.currentText() == "node_multiplexed"
        has_reservoirs = self._reservoirs_chk.isChecked()
        self._tank_group.setVisible(is_multiplexed and has_reservoirs)

    def _apply_tank_fields(self, node_entry: dict, node_type: str) -> bool:
        """Validate + write the tank fields into ``node_entry``.

        Returns False if validation fails (caller should abort the save).
        """
        if node_type != "node_multiplexed":
            node_entry.pop("has_reservoirs", None)
            for key in self._tank_spins:
                node_entry.pop(key, None)
            return True

        has_reservoirs = bool(self._reservoirs_chk.isChecked())
        node_entry["has_reservoirs"] = has_reservoirs

        if not has_reservoirs:
            for key in self._tank_spins:
                node_entry.pop(key, None)
            return True

        p_min = self._tank_spins["tank_pressure_min_kpa"].value()
        p_max = self._tank_spins["tank_pressure_max_kpa"].value()
        v_min = self._tank_spins["tank_vacuum_min_kpa"].value()
        v_max = self._tank_spins["tank_vacuum_max_kpa"].value()
        if p_min >= p_max:
            QMessageBox.warning(
                self, "Invalid pressure tank range",
                "Pressure tank min must be less than max.",
            )
            return False
        if v_min >= v_max:
            QMessageBox.warning(
                self, "Invalid vacuum tank range",
                "Vacuum tank min must be less than max.",
            )
            return False

        for key, spin in self._tank_spins.items():
            node_entry[key] = float(spin.value())
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_node_cfg(self) -> dict:
        if self._node_index < 0:
            return {}
        robots = self._settings.data.get("robots", {})
        robots_list = robots.get(_YAML_KEY[self._robot_type], [])
        if 0 <= self._robot_index < len(robots_list):
            nodes = robots_list[self._robot_index].get("nodes", [])
            if 0 <= self._node_index < len(nodes):
                return nodes[self._node_index]
        return {}

    def _on_type_changed(self, node_type: str) -> None:
        if node_type == "node_direct":
            self._slots_spin.setRange(3, 3)
            self._slots_spin.setValue(3)
            self._slots_spin.setEnabled(False)
            self._reservoirs_chk.setChecked(False)
            self._reservoirs_chk.setVisible(False)
            self._reservoirs_label.setVisible(False)
        elif node_type == "node_magnet_sensor":
            # magnet sensor node: 4 fixed sensors, no chambers/reservoirs.
            self._slots_spin.setRange(4, 4)
            self._slots_spin.setValue(4)
            self._slots_spin.setEnabled(False)
            self._reservoirs_chk.setChecked(False)
            self._reservoirs_chk.setVisible(False)
            self._reservoirs_label.setVisible(False)
        else:
            self._slots_spin.setRange(1, 16)
            self._slots_spin.setEnabled(True)
            self._slots_spin.setValue(NODE_TYPES.get(node_type, 12))
            self._reservoirs_chk.setVisible(True)
            self._reservoirs_label.setVisible(True)
        self._update_tank_visibility()
        self._update_note()

    def _update_note(self) -> None:
        nt = self._type_combo.currentText()
        notes = {
            "node_direct": "3 chambers, direct ADC sensors, onboard pumps.",
            "node_multiplexed": (
                "Up to 16 chambers (default 12). Multiplexed valves/sensors. "
                "Optional shared pressure/vacuum tanks — enable 'Reservoirs' "
                "and set tank limits in settings.yaml."
            ),
            "node_magnet_sensor": (
                "4-sensor magnet sensor. Sends raw / magnitudes / baseline-adjusted / "
                "active-quadrant data via the standard `magnet` message. No "
                "chambers, no pumps."
            ),
        }
        self._note_lbl.setText(notes.get(nt, ""))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        mac       = self._mac_edit.text().strip()
        node_type = self._type_combo.currentText()
        max_slots = 3 if node_type == "node_direct" else self._slots_spin.value()

        if not mac:
            QMessageBox.warning(self, "Missing Field", "Node MAC cannot be empty.")
            return

        # Check MAC not already used by another node in this robot
        robots_list = (
            self._settings.data.get("robots", {})
            .get(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            nodes = robots_list[self._robot_index].get("nodes", [])
            for i, n in enumerate(nodes):
                if i != self._node_index and n.get("mac") == mac:
                    QMessageBox.warning(
                        self, "Duplicate MAC",
                        f"Node {mac} is already configured for this robot.",
                    )
                    return

        # Preserve any extra fields (tank kpa, pump counts, ...) from the
        # existing entry so YAML-only edits aren't lost when saving from the UI.
        node_entry: dict = dict(self._load_node_cfg())
        node_entry.update(
            {"mac": mac, "node_type": node_type, "max_slots": max_slots}
        )
        if not self._apply_tank_fields(node_entry, node_type):
            return

        data = self._settings.data
        robots_list = (
            data.setdefault("robots", {})
            .setdefault(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            nodes = robots_list[self._robot_index].setdefault("nodes", [])
            if self._node_index < 0:
                nodes.append(node_entry)
            else:
                nodes[self._node_index] = node_entry

        self._settings.save()
        self.accept()

    def _on_delete(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm Delete",
            "Delete this node? Skins referencing its chambers will lose those chambers.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        robots_list = (
            self._settings.data.get("robots", {})
            .get(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            nodes = robots_list[self._robot_index].get("nodes", [])
            if 0 <= self._node_index < len(nodes):
                nodes.pop(self._node_index)
        self._settings.save()
        self.accept()

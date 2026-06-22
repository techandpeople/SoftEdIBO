"""Node configuration dialog.

Adds or edits a single ESP32 node entry under a robot.
A node has three attributes: MAC address, node_type, and max_slots.

Node types and their default slot counts:
    node_direct       — 3   (fixed: 3 chambers, GPIO valves, onboard pumps)
    node_multiplexed  — 12  (default; up to 16 chambers, optional shared
                              pressure/vacuum tanks via has_reservoirs)
"""

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.config.settings import Settings
from src.gui.ui_node_config_dialog import Ui_NodeConfigDialog

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}

NODE_TYPES: dict[str, int] = {
    "node_direct": 3,
    "node_multiplexed": 12,
    "node_magnet_sensor": 4,
}


class NodeConfigDialog(QDialog, Ui_NodeConfigDialog):
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
        self.setupUi(self)
        self._robot_type  = robot_type
        self._robot_index = robot_index
        self._node_index  = node_index
        self._settings    = settings

        is_new = node_index < 0
        self.setWindowTitle("Add Node" if is_new else "Configure Node")

        # Node type dropdown
        for nt in NODE_TYPES:
            self.type_combo.addItem(nt)
        self.type_combo.currentTextChanged.connect(self._on_type_changed)

        # Tank limit/target spinboxes live in the .ui (object name == config key).
        self._tank_spins = {key: getattr(self, key) for key in self._TANK_KEYS}

        self.reservoirs_chk.toggled.connect(self._update_tank_visibility)
        self.delete_btn.setVisible(not is_new)

        # Populate from existing config
        node_cfg = self._load_node_cfg()
        self.mac_edit.setText(node_cfg.get("mac", "") or prefill_mac)
        stored_type = node_cfg.get("node_type", "node_direct")
        idx = self.type_combo.findText(stored_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)
        stored_slots = node_cfg.get("max_slots", NODE_TYPES.get(stored_type, 3))
        self.reservoirs_chk.setChecked(bool(node_cfg.get("has_reservoirs", False)))
        for key, spin in self._tank_spins.items():
            if key in node_cfg:
                spin.setValue(float(node_cfg[key]))
        self._on_type_changed(self.type_combo.currentText())
        if self.slots_spin.isEnabled():
            self.slots_spin.setValue(int(stored_slots))
        self._update_note()

        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)
        self.delete_btn.clicked.connect(self._on_delete)

    # ------------------------------------------------------------------
    # Tank limit / target widgets
    # ------------------------------------------------------------------

    # Config keys for the reservoir limit/target spinboxes; each matches the
    # object name of a QDoubleSpinBox defined in the .ui (ranges/defaults there).
    # Hard caps mirror firmware's config::HARD_TANK_{MIN,MAX}_KPA (±80 kPa).
    _TANK_KEYS = (
        "tank_pressure_min_kpa",
        "tank_pressure_max_kpa",
        "tank_pressure_target_kpa",
        "tank_vacuum_min_kpa",
        "tank_vacuum_max_kpa",
        "tank_vacuum_target_kpa",
    )

    def _update_tank_visibility(self) -> None:
        is_multiplexed = self.type_combo.currentText() == "node_multiplexed"
        has_reservoirs = self.reservoirs_chk.isChecked()
        self.tank_group.setVisible(is_multiplexed and has_reservoirs)

    def _apply_tank_fields(self, node_entry: dict, node_type: str) -> bool:
        """Validate + write the tank fields into ``node_entry``.

        Returns False if validation fails (caller should abort the save).
        """
        if node_type != "node_multiplexed":
            node_entry.pop("has_reservoirs", None)
            for key in self._tank_spins:
                node_entry.pop(key, None)
            return True

        has_reservoirs = bool(self.reservoirs_chk.isChecked())
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
            self.slots_spin.setRange(3, 3)
            self.slots_spin.setValue(3)
            self.slots_spin.setEnabled(False)
            self.reservoirs_chk.setChecked(False)
            self.reservoirs_chk.setVisible(False)
            self.reservoirs_label.setVisible(False)
        elif node_type == "node_magnet_sensor":
            # magnet sensor node: 4 fixed sensors, no chambers/reservoirs.
            self.slots_spin.setRange(4, 4)
            self.slots_spin.setValue(4)
            self.slots_spin.setEnabled(False)
            self.reservoirs_chk.setChecked(False)
            self.reservoirs_chk.setVisible(False)
            self.reservoirs_label.setVisible(False)
        else:
            self.slots_spin.setRange(1, 16)
            self.slots_spin.setEnabled(True)
            self.slots_spin.setValue(NODE_TYPES.get(node_type, 12))
            self.reservoirs_chk.setVisible(True)
            self.reservoirs_label.setVisible(True)
        self._update_tank_visibility()
        self._update_note()

    def _update_note(self) -> None:
        nt = self.type_combo.currentText()
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
        self.note_lbl.setText(notes.get(nt, ""))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        mac       = self.mac_edit.text().strip()
        node_type = self.type_combo.currentText()
        max_slots = 3 if node_type == "node_direct" else self.slots_spin.value()

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

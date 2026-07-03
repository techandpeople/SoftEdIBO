"""Node configuration dialog.

Adds or edits a single ESP32 node entry under a robot.
A node has three attributes: MAC address, node_type, and max_slots.

Node types and their default slot counts:
    node_direct       — 3   (fixed: 3 chambers, GPIO valves, onboard pumps)
    node_multiplexed  — 12  (default; up to 16 chambers)
"""

from PySide6.QtWidgets import QMessageBox, QWidget

from src.config.settings import Settings
from src.gui.base_dialog import BaseDialog
from src.gui.ui_node_config_dialog import Ui_NodeConfigDialog

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}

NODE_TYPES: dict[str, int] = {
    "node_direct": 3,
    "node_multiplexed": 12,
    "node_magnet_sensor": 4,
}


class NodeConfigDialog(BaseDialog, Ui_NodeConfigDialog):
    """Dialog for adding or editing a single node entry.

    Args:
        robot_type:  One of ``"turtle"``, ``"tree"`` or ``"thymio"``.
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

        self.delete_btn.setVisible(not is_new)

        # Populate from existing config
        node_cfg = self._load_node_cfg()
        self.mac_edit.setText(node_cfg.get("mac", "") or prefill_mac)
        stored_type = node_cfg.get("node_type", "node_direct")
        idx = self.type_combo.findText(stored_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)
        stored_slots = node_cfg.get("max_slots", NODE_TYPES.get(stored_type, 3))
        self._on_type_changed(self.type_combo.currentText())
        if self.slots_spin.isEnabled():
            self.slots_spin.setValue(int(stored_slots))
        self._update_note()

        self.save_btn.clicked.connect(self._on_save)
        self.apply_btn.clicked.connect(self._on_apply)
        self.cancel_btn.clicked.connect(self.reject)
        self.delete_btn.clicked.connect(self._on_delete)

    # Legacy reservoir/tank keys dropped on save (the feature was removed).
    _LEGACY_RESERVOIR_KEYS = (
        "has_reservoirs",
        "pump_inflate_count",
        "pump_deflate_count",
        "tank_pressure_min_kpa",
        "tank_pressure_max_kpa",
        "tank_pressure_target_kpa",
        "tank_vacuum_min_kpa",
        "tank_vacuum_max_kpa",
        "tank_vacuum_target_kpa",
    )

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
        elif node_type == "node_magnet_sensor":
            # magnet sensor node: 4 fixed sensors, no chambers.
            self.slots_spin.setRange(4, 4)
            self.slots_spin.setValue(4)
            self.slots_spin.setEnabled(False)
        else:
            self.slots_spin.setRange(1, 16)
            self.slots_spin.setEnabled(True)
            self.slots_spin.setValue(NODE_TYPES.get(node_type, 12))
        self._update_note()

    def _update_note(self) -> None:
        nt = self.type_combo.currentText()
        notes = {
            "node_direct": "3 chambers, direct ADC sensors, onboard pumps.",
            "node_multiplexed": (
                "Up to 16 chambers (default 12). Multiplexed valves/sensors."
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

    def _commit(self) -> bool:
        """Validate and persist the node to settings, without closing. Returns
        True on success. Shared by Save (which then closes) and Apply (which
        stays open so the user can keep editing)."""
        mac       = self.mac_edit.text().strip()
        node_type = self.type_combo.currentText()
        max_slots = 3 if node_type == "node_direct" else self.slots_spin.value()

        if not mac:
            QMessageBox.warning(self, "Missing Field", "Node MAC cannot be empty.")
            return False

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
                    return False

        # Preserve any extra fields from the existing entry so YAML-only edits
        # aren't lost when saving from the UI, but drop the removed reservoir/
        # tank keys so old configs migrate cleanly.
        node_entry: dict = dict(self._load_node_cfg())
        for key in self._LEGACY_RESERVOIR_KEYS:
            node_entry.pop(key, None)
        node_entry.update(
            {"mac": mac, "node_type": node_type, "max_slots": max_slots}
        )

        data = self._settings.data
        robots_list = (
            data.setdefault("robots", {})
            .setdefault(_YAML_KEY[self._robot_type], [])
        )
        if 0 <= self._robot_index < len(robots_list):
            nodes = robots_list[self._robot_index].setdefault("nodes", [])
            if self._node_index < 0:
                nodes.append(node_entry)
                # Adopt the appended slot so a second Apply replaces this entry
                # instead of appending a duplicate, and flip into edit mode.
                self._node_index = len(nodes) - 1
                self.setWindowTitle("Configure Node")
                self.delete_btn.setVisible(True)
            else:
                nodes[self._node_index] = node_entry

        self._settings.save()
        return True

    def _on_apply(self) -> None:
        self._commit()

    def _on_save(self) -> None:
        if self._commit():
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

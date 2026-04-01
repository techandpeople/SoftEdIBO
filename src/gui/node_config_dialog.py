"""Node configuration dialog.

Adds or edits a single ESP32 node entry under a robot.
A node has three attributes: MAC address, node_type, and max_slots.

Node types and their default slot counts:
    standard   — 3   (direct GPIO valves, onboard pumps)
    mux        — 8   (74HC595 shift register + 74HC4051 sensor mux)
    reservoir  — 0   (dedicated reservoir node, no user-addressable slots)
"""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
    "node_pump":                  3,
    "node_multiplexed_pump":      8,
    "node_reservoir":             0,
    "node_multiplexed_reservoir": 0,
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
        form.addRow("Max slots:", self._slots_spin)

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
        stored_type = node_cfg.get("node_type", "standard")
        idx = self._type_combo.findText(stored_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        stored_slots = node_cfg.get("max_slots", NODE_TYPES.get(stored_type, 3))
        self._slots_spin.setValue(int(stored_slots))
        self._update_note()

        self._save_btn.clicked.connect(self._on_save)
        self._cancel_btn.clicked.connect(self.reject)
        self._delete_btn.clicked.connect(self._on_delete)

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
        default_slots = NODE_TYPES.get(node_type, 3)
        self._slots_spin.setValue(default_slots)
        self._update_note()

    def _update_note(self) -> None:
        nt = self._type_combo.currentText()
        notes = {
            "node_pump":                  "Direct GPIO valves, onboard inflate + deflate pumps.",
            "node_multiplexed_pump":      "74HC595 shift-register valves + 74HC4051 sensor mux.",
            "node_reservoir":             "Dedicated reservoir node — GPIO pumps, single tank.",
            "node_multiplexed_reservoir": "Dedicated reservoir node — shift-register pumps + sensor mux, multi-tank.",
        }
        self._note_lbl.setText(notes.get(nt, ""))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        mac       = self._mac_edit.text().strip()
        node_type = self._type_combo.currentText()
        max_slots = self._slots_spin.value()

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

        node_entry: dict = {"mac": mac, "node_type": node_type, "max_slots": max_slots}

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

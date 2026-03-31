"""Skin configuration dialog.

Edits a single skin entry: display name, skin ID, node MAC address, and
which slots (0–2) on that node this skin occupies.

Constraints enforced on save:
  - No slot may be used by another skin that shares the same MAC.
  - The total slots on a single MAC (across all its skins) cannot exceed 3.
"""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QWidget,
)

from src.config.settings import Settings
from src.gui.ui_node_config_dialog import Ui_NodeConfigDialog
from src.hardware.espnow_gateway import ESPNowGateway

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}


class NodeConfigDialog(QDialog, Ui_NodeConfigDialog):
    """Dialog for adding or editing a single skin entry.

    Args:
        robot_type: One of ``"turtle"``, ``"tree"``, or ``"thymio"``.
        robot_index: Index of the parent robot in the settings list.
        skin_index: Index of this skin in the robot's ``skins`` list, or
            ``-1`` to add a new skin.
        settings: Application settings instance.
        gateway: Shared ESP-NOW gateway (used by the test dialog).
        parent: Optional parent widget.
        prefill_mac: MAC address to pre-fill when adding a new skin.
    """

    def __init__(
        self,
        robot_type: str,
        robot_index: int,
        skin_index: int,
        settings: Settings,
        gateway: ESPNowGateway,
        parent: QWidget | None = None,
        prefill_mac: str = "",
    ):
        super().__init__(parent)
        self._robot_type = robot_type
        self._robot_index = robot_index
        self._skin_index = skin_index
        self._settings = settings
        self._gateway = gateway

        self.setupUi(self)

        is_new = skin_index < 0
        type_label = {
            "turtle": "Turtle Skin", "tree": "Tree Skin", "thymio": "Thymio Skin",
        }.get(robot_type, "Skin")
        self.setWindowTitle(("Add " if is_new else "Configure ") + type_label)

        # Delete button only shown when editing an existing skin
        self.delete_btn.setVisible(not is_new)
        self.delete_btn.setText("Delete Skin")

        # Test button enabled only when gateway is connected
        self.test_btn.setEnabled(gateway.is_connected)

        # Repurpose mac_form: add skin_id and name rows above MAC
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Display name (e.g. Skin 1)")
        self._skin_id_edit = QLineEdit()
        self._skin_id_edit.setPlaceholderText("skin_id (e.g. skin_1)")

        self.mac_form.insertRow(0, QLabel("Skin ID:"), self._skin_id_edit)
        self.mac_form.insertRow(1, QLabel("Name:"), self._name_edit)
        # mac_form row 2 is now "Node MAC:" / mac_edit (from .ui)

        # Repurpose chambers_group as slot selector
        self.chambers_group.setTitle("Slots (max 3 per MAC)")

        # 3 slot rows: checkbox + max pressure spinbox
        self._slot_checks: list[QCheckBox] = []
        self._max_spins: list[QSpinBox] = []
        self._default_max_kpa = 8
        for slot in range(3):
            row_widget = QWidget()
            hbox = QHBoxLayout(row_widget)
            hbox.setContentsMargins(4, 2, 4, 2)

            cb = QCheckBox(f"Slot {slot}")
            self._slot_checks.append(cb)
            hbox.addWidget(cb)

            hbox.addWidget(QLabel("Max (kPa):"))
            max_spin = QSpinBox()
            max_spin.setRange(1, 8)
            max_spin.setValue(self._default_max_kpa)
            max_spin.setSuffix(" kPa")
            max_spin.setFixedWidth(70)
            max_spin.setEnabled(False)
            self._max_spins.append(max_spin)
            hbox.addWidget(max_spin)

            cb.toggled.connect(max_spin.setEnabled)

            hbox.addStretch()
            self.chambers_vbox.addWidget(row_widget)
        self.chambers_vbox.addStretch()

        # Populate from existing config
        skin_cfg = self._load_skin_cfg()
        self._skin_id_edit.setText(skin_cfg.get("skin_id", ""))
        self._name_edit.setText(skin_cfg.get("name", ""))
        self.mac_edit.setText(skin_cfg.get("mac", "") or prefill_mac)
        max_pressures = skin_cfg.get("max_pressure", {})
        for slot in skin_cfg.get("slots", []):
            if 0 <= slot < 3:
                self._slot_checks[slot].setChecked(True)
                val = max_pressures.get(slot, max_pressures.get(str(slot)))
                if val is not None:
                    self._max_spins[slot].setValue(int(val))

        # Connect buttons
        self.delete_btn.clicked.connect(self._on_delete)
        self.test_btn.clicked.connect(self._on_test_actuators)
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        """Return all skin entries for this robot except the one being edited."""
        robots = self._settings.data.get("robots", {})
        robots_list = robots.get(_YAML_KEY[self._robot_type], [])
        if not (0 <= self._robot_index < len(robots_list)):
            return []
        all_skins = robots_list[self._robot_index].get("skins", [])
        return [sc for i, sc in enumerate(all_skins) if i != self._skin_index]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_test_actuators(self) -> None:
        mac = self.mac_edit.text().strip()
        if not mac:
            QMessageBox.warning(self, "Test Actuators", "Enter a MAC address first.")
            return

        skin_id = self._skin_id_edit.text().strip() or "preview"
        slots = [i for i, cb in enumerate(self._slot_checks) if cb.isChecked()]
        skin_cfgs = [{"skin_id": skin_id, "slots": slots}]

        from src.gui.test_actuators_dialog import TestActuatorsDialog

        dlg = TestActuatorsDialog(
            mac=mac,
            skin_cfgs=skin_cfgs,
            gateway=self._gateway,
            parent=self,
        )
        dlg.exec()

    def _on_save(self) -> None:
        skin_id = self._skin_id_edit.text().strip()
        name = self._name_edit.text().strip() or skin_id
        mac = self.mac_edit.text().strip()
        slots = [i for i, cb in enumerate(self._slot_checks) if cb.isChecked()]

        if not skin_id:
            QMessageBox.warning(self, "Missing Field", "Skin ID cannot be empty.")
            return
        if not mac:
            QMessageBox.warning(self, "Missing Field", "Node MAC cannot be empty.")
            return
        if not slots:
            QMessageBox.warning(self, "Missing Field", "Select at least one slot.")
            return

        # Validate slot conflicts on same MAC
        siblings = self._sibling_skins()
        used_on_mac = {
            slot
            for sc in siblings
            if sc.get("mac") == mac
            for slot in sc.get("slots", [])
        }
        conflicts = set(slots) & used_on_mac
        if conflicts:
            QMessageBox.warning(
                self, "Slot Conflict",
                f"Slot(s) {sorted(conflicts)} are already used by another skin on MAC {mac}.",
            )
            return
        if len(set(slots) | used_on_mac) > 3:
            QMessageBox.warning(
                self, "Capacity Exceeded",
                f"MAC {mac} would exceed 3 total slots.",
            )
            return

        # Collect max pressure (kPa) per slot (only store non-default values)
        max_pressure: dict[int, int] = {}
        for slot in slots:
            val = self._max_spins[slot].value()
            if val != self._default_max_kpa:
                max_pressure[slot] = val

        skin_entry: dict = {"skin_id": skin_id, "name": name, "mac": mac, "slots": slots}
        if max_pressure:
            skin_entry["max_pressure"] = max_pressure

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
            self,
            "Confirm Delete",
            "Delete this skin? This cannot be undone.",
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

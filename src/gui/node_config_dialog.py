"""Per-node configuration dialog with actuator testing."""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from src.config.settings import Settings
from src.gui.ui_node_config_dialog import Ui_NodeConfigDialog
from src.hardware.espnow_gateway import ESPNowGateway

_MAX_CHAMBERS = 3
_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}


class NodeConfigDialog(QDialog, Ui_NodeConfigDialog):
    """Dialog for configuring a single ESP32 node and its air chambers.

    Each chamber row has three slot checkboxes (0–2).  Checking a slot in one
    chamber automatically unchecks it in every other chamber (cross-chamber)
    and unchecks the other slots in the same row (one slot per chamber).

    Args:
        robot_type: One of ``"turtle"``, ``"tree"``, or ``"thymio"``.
        robot_index: Index of the parent robot in the settings list.
        node_index: Index of this node in the robot's ``nodes`` list, or
            ``-1`` to add a new node.
        settings: Application settings instance.
        gateway: Shared ESP-NOW gateway (used by the test dialog).
        parent: Optional parent widget.
    """

    def __init__(
        self,
        robot_type: str,
        robot_index: int,
        node_index: int,
        settings: Settings,
        gateway: ESPNowGateway,
        parent: QWidget | None = None,
        prefill_mac: str = "",
    ):
        super().__init__(parent)
        self._robot_type = robot_type
        self._robot_index = robot_index
        self._node_index = node_index
        self._settings = settings
        self._gateway = gateway
        self._skin_entries: list[dict] = []

        self.setupUi(self)

        is_new = node_index < 0
        type_label = {
            "turtle": "Turtle Node", "tree": "Tree Node", "thymio": "Thymio Node",
        }.get(robot_type, "Node")
        self.setWindowTitle(("Add " if is_new else "Configure ") + type_label)

        # Delete button only shown when editing an existing node
        self.delete_btn.setVisible(not is_new)

        # Test button enabled only when gateway is connected
        self.test_btn.setEnabled(gateway.is_connected)

        # Add-chamber button — must exist before _add_skin_row is called
        self._add_chamber_btn = QPushButton("+ Add Air Chamber")
        self._add_chamber_btn.clicked.connect(lambda: self._add_skin_row(None))
        self.chambers_vbox.addWidget(self._add_chamber_btn)
        self.chambers_vbox.addStretch()

        # Populate from config
        node_cfg = self._load_node_cfg()
        self.mac_edit.setText(node_cfg.get("mac", "") or prefill_mac)
        for skin_cfg in node_cfg.get("skins", []):
            self._add_skin_row(skin_cfg)
        self._on_slot_changed()
        self._update_add_chamber_btn()

        # Connect buttons
        self.delete_btn.clicked.connect(self._on_delete)
        self.test_btn.clicked.connect(self._on_test_actuators)
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)

    # ------------------------------------------------------------------
    # Load / collect helpers
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

    def _collect_skins(self) -> list[dict]:
        skins = []
        for se in self._skin_entries:
            if se["deleted"]:
                continue
            skin_id = se["id_edit"].text().strip()
            slots = [i for i, cb in enumerate(se["slot_checks"]) if cb.isChecked()]
            if skin_id and slots:
                skins.append({"skin_id": skin_id, "slots": slots})
        return skins

    def _active_chamber_count(self) -> int:
        return sum(1 for se in self._skin_entries if not se["deleted"])

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def _on_slot_changed(self) -> None:
        """Disable in every other chamber any slot that is already checked here."""
        active: list[tuple[int, set[int]]] = [
            (i, {s for s, cb in enumerate(se["slot_checks"]) if cb.isChecked()})
            for i, se in enumerate(self._skin_entries)
            if not se["deleted"]
        ]
        for i, se in enumerate(self._skin_entries):
            if se["deleted"]:
                continue
            others_used: set[int] = set()
            for j, used in active:
                if j != i:
                    others_used |= used
            for slot_idx, cb in enumerate(se["slot_checks"]):
                cb.setEnabled(slot_idx not in others_used)

    # ------------------------------------------------------------------
    # Dynamic chamber rows
    # ------------------------------------------------------------------

    def _add_skin_row(self, skin_cfg: dict | None) -> None:
        if self._active_chamber_count() >= _MAX_CHAMBERS:
            return

        skin_id = skin_cfg.get("skin_id", "") if skin_cfg else ""
        active_slots = set(skin_cfg.get("slots", [])) if skin_cfg else set()

        row_widget = QWidget()
        hbox = QHBoxLayout(row_widget)
        hbox.setContentsMargins(0, 0, 0, 0)

        id_edit = QLineEdit(skin_id)
        id_edit.setPlaceholderText("chamber_id")
        id_edit.setMinimumWidth(120)
        hbox.addWidget(QLabel("Chamber ID:"))
        hbox.addWidget(id_edit)

        slot_checks: list[QCheckBox] = []
        for slot in range(3):
            cb = QCheckBox(f"Slot {slot}")
            cb.setChecked(slot in active_slots)
            hbox.addWidget(cb)
            slot_checks.append(cb)

        # Connect each checkbox: uncheck siblings in this row, then enforce cross-chamber
        for this_slot, cb in enumerate(slot_checks):
            def _make_handler(s: int, checks: list[QCheckBox]):
                def _handler(checked: bool) -> None:
                    if checked:
                        for j, sibling in enumerate(checks):
                            if j != s and sibling.isChecked():
                                sibling.blockSignals(True)
                                sibling.setChecked(False)
                                sibling.blockSignals(False)
                    self._on_slot_changed()
                return _handler
            cb.toggled.connect(_make_handler(this_slot, slot_checks))

        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(28)
        del_btn.setToolTip("Remove this air chamber")
        hbox.addWidget(del_btn)

        entry: dict = {
            "widget": row_widget,
            "id_edit": id_edit,
            "slot_checks": slot_checks,
            "deleted": False,
        }
        self._skin_entries.append(entry)

        def _delete_skin() -> None:
            entry["deleted"] = True
            row_widget.hide()
            self._update_add_chamber_btn()
            self._on_slot_changed()

        del_btn.clicked.connect(_delete_skin)
        # Insert before the "+ Add Air Chamber" button and the stretch
        self.chambers_vbox.insertWidget(self.chambers_vbox.count() - 2, row_widget)
        self._update_add_chamber_btn()
        self._on_slot_changed()

    def _update_add_chamber_btn(self) -> None:
        self._add_chamber_btn.setEnabled(self._active_chamber_count() < _MAX_CHAMBERS)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_test_actuators(self) -> None:
        mac = self.mac_edit.text().strip()
        if not mac:
            QMessageBox.warning(self, "Test Actuators", "Enter a MAC address first.")
            return

        from src.gui.test_actuators_dialog import TestActuatorsDialog

        dlg = TestActuatorsDialog(
            mac=mac,
            skin_cfgs=self._collect_skins(),
            gateway=self._gateway,
            parent=self,
        )
        dlg.exec()

    def _on_save(self) -> None:
        mac = self.mac_edit.text().strip()
        node_entry = {"mac": mac, "skins": self._collect_skins()}

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
            self,
            "Confirm Delete",
            "Delete this node? This cannot be undone.",
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

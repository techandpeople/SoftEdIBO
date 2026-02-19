"""Per-node configuration dialog with actuator testing."""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.hardware.espnow_gateway import ESPNowGateway

_MAX_SKINS = 3
_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}


class NodeConfigDialog(QDialog):
    """Dialog for configuring a single ESP32 node and its skins.

    Each skin row has three slot checkboxes (0–2).  Checking a slot in one
    skin automatically unchecks it in every other skin (cross-skin) and
    unchecks the other slots in the same row (one slot per skin).

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
    ):
        super().__init__(parent)
        self._robot_type = robot_type
        self._robot_index = robot_index
        self._node_index = node_index
        self._settings = settings
        self._gateway = gateway
        self._skin_entries: list[dict] = []
        self._add_skin_btn: QPushButton | None = None

        node_cfg = self._load_node_cfg()
        self._build_ui(node_cfg)

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

    def _active_skin_count(self) -> int:
        return sum(1 for se in self._skin_entries if not se["deleted"])

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def _on_slot_changed(self) -> None:
        """Disable in every other skin any slot that is already checked here."""
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
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, node_cfg: dict) -> None:
        is_new = self._node_index < 0
        type_label = {
            "turtle": "Turtle Node", "tree": "Tree Node", "thymio": "Thymio Node",
        }.get(self._robot_type, "Node")
        self.setWindowTitle(("Add " if is_new else "Configure ") + type_label)
        self.setMinimumSize(520, 360)

        main_layout = QVBoxLayout(self)

        # ── MAC ───────────────────────────────────────────────────────
        mac_form = QFormLayout()
        self._mac_edit = QLineEdit(node_cfg.get("mac", ""))
        self._mac_edit.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        mac_form.addRow("Node MAC:", self._mac_edit)
        main_layout.addLayout(mac_form)

        # ── Skins ─────────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self._skins_vbox = QVBoxLayout(scroll_content)
        self._skins_vbox.setSpacing(4)
        scroll.setWidget(scroll_content)

        for skin_cfg in node_cfg.get("skins", []):
            self._add_skin_row(skin_cfg)
        self._on_slot_changed()

        self._add_skin_btn = QPushButton("+ Add Skin")
        self._add_skin_btn.clicked.connect(lambda: self._add_skin_row(None))
        self._skins_vbox.addWidget(self._add_skin_btn)
        self._skins_vbox.addStretch()
        self._update_add_skin_btn()

        skins_group = QGroupBox(f"Skins (max {_MAX_SKINS})")
        skins_group_layout = QVBoxLayout(skins_group)
        skins_group_layout.addWidget(scroll)
        main_layout.addWidget(skins_group)

        # ── Button row ────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        if not is_new:
            delete_btn = QPushButton("Delete Node")
            delete_btn.setStyleSheet("color: #cc2222;")
            delete_btn.clicked.connect(self._on_delete)
            btn_row.addWidget(delete_btn)

        test_btn = QPushButton("Test Actuators")
        test_btn.setEnabled(self._gateway.is_connected)
        test_btn.setToolTip("Open test dialog (requires gateway connection)")
        test_btn.clicked.connect(self._on_test_actuators)
        btn_row.addWidget(test_btn)

        btn_row.addStretch()

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        main_layout.addLayout(btn_row)

    def _add_skin_row(self, skin_cfg: dict | None) -> None:
        if self._active_skin_count() >= _MAX_SKINS:
            return

        skin_id = skin_cfg.get("skin_id", "") if skin_cfg else ""
        active_slots = set(skin_cfg.get("slots", [])) if skin_cfg else set()

        row_widget = QWidget()
        hbox = QHBoxLayout(row_widget)
        hbox.setContentsMargins(0, 0, 0, 0)

        id_edit = QLineEdit(skin_id)
        id_edit.setPlaceholderText("skin_id")
        id_edit.setMinimumWidth(120)
        hbox.addWidget(QLabel("Skin ID:"))
        hbox.addWidget(id_edit)

        slot_checks: list[QCheckBox] = []
        for slot in range(3):
            cb = QCheckBox(f"Slot {slot}")
            cb.setChecked(slot in active_slots)
            hbox.addWidget(cb)
            slot_checks.append(cb)

        # Connect each checkbox: uncheck siblings in this row, then enforce cross-skin
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
        del_btn.setToolTip("Remove this skin")
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
            self._update_add_skin_btn()
            self._on_slot_changed()

        del_btn.clicked.connect(_delete_skin)
        self._skins_vbox.insertWidget(self._skins_vbox.count() - 2, row_widget)
        self._update_add_skin_btn()
        self._on_slot_changed()

    def _update_add_skin_btn(self) -> None:
        if self._add_skin_btn is not None:
            self._add_skin_btn.setEnabled(self._active_skin_count() < _MAX_SKINS)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_test_actuators(self) -> None:
        mac = self._mac_edit.text().strip()
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
        mac = self._mac_edit.text().strip()
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

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
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.hardware.espnow_gateway import ESPNowGateway

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}
_DEFAULT_MAX_KPA  = 8.0
_MAX_ALLOWED_KPA  = 12.0
_CONFIRM_DELTA    = 2.0
_MAX_CHAMBERS     = 3


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
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._robot_type  = robot_type
        self._robot_index = robot_index
        self._skin_index  = skin_index
        self._settings    = settings
        self._gateway     = gateway
        self._rows: list[_ChamberRow] = []

        is_new = skin_index < 0
        self.setWindowTitle("Add Skin" if is_new else "Configure Skin")
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)

        # Skin ID / Name
        form = QFormLayout()
        self._skin_id_edit = QLineEdit()
        self._skin_id_edit.setPlaceholderText("e.g. belly")
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Display name (e.g. Belly)")
        form.addRow("Skin ID:", self._skin_id_edit)
        form.addRow("Name:",    self._name_edit)
        outer.addLayout(form)

        # Chamber rows inside a scroll area
        scroll      = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(200)
        inner_w     = QWidget()
        self._rows_layout = QVBoxLayout(inner_w)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(3)
        scroll.setWidget(inner_w)
        outer.addWidget(QLabel("Chambers:"))
        outer.addWidget(scroll)

        # Add chamber button
        self._add_chamber_btn = QPushButton("+ Add Chamber")
        self._add_chamber_btn.clicked.connect(self._on_add_chamber)
        outer.addWidget(self._add_chamber_btn)

        # Action buttons
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
        outer.addLayout(btn_row)

        # Pre-populate
        skin_cfg = self._load_skin_cfg()
        self._skin_id_edit.setText(skin_cfg.get("skin_id", ""))
        self._name_edit.setText(skin_cfg.get("name", ""))
        for ch in skin_cfg.get("chambers", []):
            self._add_row(
                mac=ch.get("mac", ""),
                slot=int(ch.get("slot", 0)),
                max_pressure=float(ch.get("max_pressure", _DEFAULT_MAX_KPA)),
            )
        if not self._rows:
            self._add_row()  # start with one empty row

        self._test_btn.clicked.connect(self._on_test)
        self._delete_btn.clicked.connect(self._on_delete)
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn.clicked.connect(self._on_save)

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

    def _on_save(self) -> None:
        skin_id = self._skin_id_edit.text().strip()
        name    = self._name_edit.text().strip() or skin_id

        if not skin_id:
            QMessageBox.warning(self, "Missing Field", "Skin ID cannot be empty.")
            return
        if not self._rows:
            QMessageBox.warning(self, "Missing Field", "Add at least one chamber.")
            return

        chambers = []
        for row in self._rows:
            mac, slot, max_p = row.get_values()
            if not mac:
                QMessageBox.warning(self, "Missing Field",
                                    "Each chamber must have a node MAC selected.")
                return
            chambers.append({"mac": mac, "slot": slot, "max_pressure": max_p})

        # Check for (mac, slot) conflicts within this skin
        seen = set()
        for ch in chambers:
            key = (ch["mac"], ch["slot"])
            if key in seen:
                QMessageBox.warning(
                    self, "Duplicate Chamber",
                    f"Slot {ch['slot']} on {ch['mac']} is used more than once in this skin.",
                )
                return
            seen.add(key)

        # Check for (mac, slot) conflicts with sibling skins
        used_by_siblings = {
            (ch.get("mac"), ch.get("slot"))
            for sk in self._sibling_skins()
            for ch in sk.get("chambers", [])
        }
        conflicts = [ch for ch in chambers if (ch["mac"], ch["slot"]) in used_by_siblings]
        if conflicts:
            parts = [f"{c['mac']} #{c['slot']}" for c in conflicts]
            QMessageBox.warning(
                self, "Chamber Conflict",
                "These chambers are already used by another skin:\n" + "\n".join(parts),
            )
            return

        # Warn on large max_pressure changes
        prev_cfg  = self._load_skin_cfg()
        prev_chs  = {(c.get("mac"), c.get("slot")): c for c in prev_cfg.get("chambers", [])}
        big_changes: list[str] = []
        for ch in chambers:
            key = (ch["mac"], ch["slot"])
            old_max = float(prev_chs.get(key, {}).get("max_pressure", _DEFAULT_MAX_KPA))
            if abs(ch["max_pressure"] - old_max) >= _CONFIRM_DELTA:
                big_changes.append(
                    f"{ch['mac']} #{ch['slot']}: {old_max:.1f} → {ch['max_pressure']:.1f} kPa"
                )
        if big_changes:
            reply = QMessageBox.question(
                self, "Confirm Large Pressure Change",
                "Large max-pressure change detected:\n"
                + "\n".join(big_changes)
                + "\n\nKeep these changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Only store max_pressure when it differs from the default
        for ch in chambers:
            if abs(ch["max_pressure"] - _DEFAULT_MAX_KPA) < 1e-9:
                del ch["max_pressure"]

        skin_entry: dict = {"skin_id": skin_id, "name": name, "chambers": chambers}

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

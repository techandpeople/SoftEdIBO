"""Robot connection management panel."""

import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.gui.ui_robot_panel import Ui_RobotPanel
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.serial_ports import list_esp32_ports
from src.robots.base_robot import BaseRobot

_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}

# Known node types and their default slot counts.
NODE_TYPES: dict[str, int] = {
    "standard":  3,
    "mux":       8,
    "reservoir": 0,   # reservoir nodes have no user-addressable slots
}


class RobotPanel(QWidget, Ui_RobotPanel):
    """Panel for managing robots and their ESP32 nodes.

    Tree structure per robot:
      • top-level item   = robot
        ├── [N] ● MAC  (node_type, used/max slots)   — double-click to edit node
        └── [S] Skin name  (chamber summary)          — double-click to edit skin

    Signals:
        robot_configured: Emitted after any config change.
    """

    robot_configured = Signal()
    gateway_changed  = Signal(bool)

    def __init__(self, gateway: ESPNowGateway, settings: Settings):
        super().__init__()
        self._gateway  = gateway
        self._settings = settings
        self._robots: list[BaseRobot] = []

        self.setupUi(self)

        for tree in (self.turtle_tree, self.tree_tree, self.thymio_tree):
            tree.setColumnCount(2)
            tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            tree.setColumnWidth(1, 110)

        baud = str(self._settings.gateway_baud)
        idx  = self.baud_rate_combo.findText(baud)
        if idx >= 0:
            self.baud_rate_combo.setCurrentIndex(idx)

        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._on_gateway_connect)
        self.scan_btn.clicked.connect(self._on_scan)

        self.add_turtle_btn.clicked.connect(lambda: self._on_add_robot("turtle"))
        self.add_tree_btn.clicked.connect(lambda: self._on_add_robot("tree"))
        self.add_thymio_btn.clicked.connect(lambda: self._on_add_robot("thymio"))

        _all_trees = (self.turtle_tree, self.tree_tree, self.thymio_tree)
        for tree, robot_type in (
            (self.turtle_tree, "turtle"),
            (self.tree_tree,   "tree"),
            (self.thymio_tree, "thymio"),
        ):
            others = [t for t in _all_trees if t is not tree]
            tree.itemPressed.connect(
                lambda _item, _col, o=others: [t.clearSelection() for t in o]
            )
            tree.itemDoubleClicked.connect(self._on_item_double_clicked)
            tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tree.customContextMenuRequested.connect(
                lambda pos, t=tree, rt=robot_type: self._on_context_menu(pos, t, rt)
            )
            tree.keyPressEvent = (
                lambda event, t=tree: self._on_tree_key_press(event, t)
            )

        self._refresh_ports()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self, robots: list[BaseRobot]) -> None:
        self._robots = robots
        self._refresh_all_trees()

    # ------------------------------------------------------------------
    # Gateway
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = (
            self.port_combo.currentData()
            or self.port_combo.currentText()
            or self._settings.gateway_port
        )
        esp_ports = list_esp32_ports()
        self.port_combo.clear()
        if not esp_ports:
            default_port = "COM3" if sys.platform == "win32" else "/dev/ttyUSB0"
            port = current or default_port
            self.port_combo.addItem(port, port)
        else:
            for p in esp_ports:
                desc  = p.description or ""
                label = f"{p.device} — {desc}" if desc and desc != "n/a" else p.device
                self.port_combo.addItem(label, p.device)
            devices = [self.port_combo.itemData(i) for i in range(self.port_combo.count())]
            if current and current not in devices:
                self.port_combo.insertItem(0, current, current)
        for i in range(self.port_combo.count()):
            if self.port_combo.itemData(i) == current:
                self.port_combo.setCurrentIndex(i)
                break

    def _on_gateway_connect(self) -> None:
        if self._gateway.is_connected:
            self._gateway.disconnect()
            self.gateway_status_label.setText("Disconnected")
            self.connect_btn.setText("Connect")
            self.scan_btn.setEnabled(False)
            self._refresh_all_trees()
            self.gateway_changed.emit(False)
        else:
            port = self.port_combo.currentData() or self.port_combo.currentText()
            baud = int(self.baud_rate_combo.currentText())
            self._gateway._port      = port
            self._gateway._baud_rate = baud
            if self._gateway.connect():
                self.gateway_status_label.setText(f"Connected ({port})")
                self.connect_btn.setText("Disconnect")
                self.scan_btn.setEnabled(True)
                self.gateway_changed.emit(True)
                self._settings.data.setdefault("gateway", {})
                self._settings.data["gateway"]["serial_port"] = port
                self._settings.data["gateway"]["baud_rate"]   = baud
                self._settings.save()
            else:
                self.gateway_status_label.setText(f"Connection failed ({port})")

    def _on_scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning…")
        self._gateway.scan()
        QTimer.singleShot(2000, self._on_scan_done)

    def _on_scan_done(self) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan Nodes")
        self._refresh_all_trees()
        n    = len(self._gateway.known_macs)
        port = self.port_combo.currentData() or self.port_combo.currentText()
        suffix = f" · {n} node{'s' if n != 1 else ''} found" if n else " · no nodes found"
        self.gateway_status_label.setText(f"Connected ({port}){suffix}")

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _refresh_all_trees(self) -> None:
        known      = self._gateway.known_macs if self._gateway.is_connected else frozenset()
        robot_data = self._settings.data.get("robots", {})
        self._fill_tree(self.turtle_tree, "turtle", robot_data.get("turtles", []), known)
        self._fill_tree(self.tree_tree,   "tree",   robot_data.get("trees",   []), known)
        self._fill_tree(self.thymio_tree, "thymio", robot_data.get("thymios", []), known)

    def _fill_tree(
        self,
        tree: QTreeWidget,
        robot_type: str,
        robots_list: list[dict],
        known: frozenset,
    ) -> None:
        tree.clear()
        for robot_index, robot_cfg in enumerate(robots_list):
            name = (
                robot_cfg.get("thymio_id", f"thymio-{robot_index + 1}")
                if robot_type == "thymio"
                else robot_cfg.get("id", f"{robot_type}-{robot_index + 1}")
            )

            robot_item = QTreeWidgetItem([name])
            robot_item.setData(0, Qt.ItemDataRole.UserRole, {
                "robot_type":  robot_type,
                "robot_index": robot_index,
                "item_type":   "robot",
            })
            tree.addTopLevelItem(robot_item)

            # Inline "+ Node" / "+ Skin" buttons
            btn_w = QWidget()
            hbox  = QHBoxLayout(btn_w)
            hbox.setContentsMargins(2, 1, 2, 1)
            hbox.setSpacing(3)
            add_node_btn = QPushButton("+ Node")
            add_node_btn.setMaximumHeight(22)
            add_skin_btn = QPushButton("+ Skin")
            add_skin_btn.setMaximumHeight(22)
            ri = robot_index
            add_node_btn.clicked.connect(
                lambda _=False, rt=robot_type, ridx=ri: self._on_add_node(rt, ridx)
            )
            add_skin_btn.clicked.connect(
                lambda _=False, rt=robot_type, ridx=ri: self._on_add_skin(rt, ridx)
            )
            hbox.addWidget(add_node_btn)
            hbox.addWidget(add_skin_btn)
            tree.setItemWidget(robot_item, 1, btn_w)

            # --- Node children ---
            skins_for_robot = robot_cfg.get("skins", [])
            for node_index, node_cfg in enumerate(robot_cfg.get("nodes", [])):
                mac        = node_cfg.get("mac", "")
                node_type  = node_cfg.get("node_type", "standard")
                max_slots  = int(node_cfg.get("max_slots", NODE_TYPES.get(node_type, 3)))
                online     = bool(mac and mac in known)
                dot        = "●" if online else "○"
                color      = QColor("#2a9d2a") if online else QColor("#cc2222")

                used = sum(
                    1
                    for sk in skins_for_robot
                    for ch in sk.get("chambers", [])
                    if ch.get("mac") == mac
                )
                label = f"[N]  {dot}  {mac}  ({node_type}, {used}/{max_slots})"
                node_item = QTreeWidgetItem([label])
                node_item.setForeground(0, color)
                node_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "robot_type":  robot_type,
                    "robot_index": robot_index,
                    "item_type":   "node",
                    "node_index":  node_index,
                })
                robot_item.addChild(node_item)

            # --- Skin children ---
            for skin_index, skin_cfg in enumerate(skins_for_robot):
                skin_name = skin_cfg.get("name") or skin_cfg.get("skin_id", "")
                chambers  = skin_cfg.get("chambers", [])
                ch_parts  = [f"{c['mac'][-5:]}#{c['slot']}" for c in chambers]
                label     = f"[S]  {skin_name}  ({', '.join(ch_parts)})"
                skin_item = QTreeWidgetItem([label])
                skin_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "robot_type":  robot_type,
                    "robot_index": robot_index,
                    "item_type":   "skin",
                    "skin_index":  skin_index,
                })
                robot_item.addChild(skin_item)

            robot_item.setExpanded(True)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _on_context_menu(self, pos, tree: QTreeWidget, robot_type: str) -> None:
        item = tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return

        item_type = data.get("item_type")
        menu = QMenu(self)

        if item_type == "robot":
            robot_index = data["robot_index"]
            configure_action = menu.addAction("Configure…") if robot_type == "thymio" else None
            rename_action    = menu.addAction("Rename…")    if robot_type != "thymio" else None
            delete_action    = menu.addAction("Delete Robot")
            action = menu.exec(tree.viewport().mapToGlobal(pos))
            if configure_action and action == configure_action:
                self._on_configure_thymio(robot_index)
            elif rename_action and action == rename_action:
                self._on_rename_robot(robot_type, robot_index)
            elif action == delete_action:
                self._on_delete_robot(robot_type, robot_index)

        elif item_type in ("node", "skin"):
            delete_action = menu.addAction(
                "Delete Node" if item_type == "node" else "Delete Skin"
            )
            action = menu.exec(tree.viewport().mapToGlobal(pos))
            if action == delete_action:
                if item_type == "node":
                    self._delete_node(data["robot_type"], data["robot_index"], data["node_index"])
                else:
                    self._delete_skin(data["robot_type"], data["robot_index"], data["skin_index"])

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def _on_tree_key_press(self, event: QKeyEvent, tree: QTreeWidget) -> None:
        if event.key() == Qt.Key.Key_Delete:
            item = tree.currentItem()
            if item is None:
                return
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data is None:
                return
            item_type = data.get("item_type")
            if item_type == "node":
                self._delete_node(data["robot_type"], data["robot_index"], data["node_index"])
            elif item_type == "skin":
                self._delete_skin(data["robot_type"], data["robot_index"], data["skin_index"])
            elif item_type == "robot":
                self._on_delete_robot(data["robot_type"], data["robot_index"])
        else:
            QTreeWidget.keyPressEvent(tree, event)

    # ------------------------------------------------------------------
    # Double-click
    # ------------------------------------------------------------------

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        item_type = data.get("item_type")
        if item_type == "node":
            self._open_node_dialog(data["robot_type"], data["robot_index"], data["node_index"])
        elif item_type == "skin":
            self._open_skin_dialog(data["robot_type"], data["robot_index"], data["skin_index"])
        elif item_type == "robot":
            rtype = data["robot_type"]
            ridx  = data["robot_index"]
            if rtype == "thymio":
                self._on_configure_thymio(ridx)
            else:
                self._on_rename_robot(rtype, ridx)

    # ------------------------------------------------------------------
    # Add / open dialogs
    # ------------------------------------------------------------------

    def _on_add_node(self, robot_type: str, robot_index: int) -> None:
        prefill_mac = ""
        known = self._gateway.known_macs
        if known:
            all_assigned = {
                node.get("mac", "")
                for robots in self._settings.data.get("robots", {}).values()
                for robot in robots
                for node in robot.get("nodes", [])
            }
            unassigned = sorted(known - all_assigned)
            if unassigned:
                items  = unassigned + ["Enter manually…"]
                choice, ok = QInputDialog.getItem(
                    self, "Add Node", "Select a discovered node:", items, 0, False
                )
                if not ok:
                    return
                if choice != "Enter manually…":
                    prefill_mac = choice
        self._open_node_dialog(robot_type, robot_index, -1, prefill_mac)

    def _on_add_skin(self, robot_type: str, robot_index: int) -> None:
        self._open_skin_dialog(robot_type, robot_index, -1)

    def _open_node_dialog(
        self, robot_type: str, robot_index: int, node_index: int,
        prefill_mac: str = "",
    ) -> None:
        from src.gui.node_config_dialog import NodeConfigDialog
        dlg = NodeConfigDialog(
            robot_type=robot_type,
            robot_index=robot_index,
            node_index=node_index,
            settings=self._settings,
            parent=self,
            prefill_mac=prefill_mac,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.robot_configured.emit()

    def _open_skin_dialog(
        self, robot_type: str, robot_index: int, skin_index: int,
    ) -> None:
        from src.gui.skin_config_dialog import SkinConfigDialog
        dlg = SkinConfigDialog(
            robot_type=robot_type,
            robot_index=robot_index,
            skin_index=skin_index,
            settings=self._settings,
            gateway=self._gateway,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.robot_configured.emit()

    # ------------------------------------------------------------------
    # Delete helpers
    # ------------------------------------------------------------------

    def _delete_node(self, robot_type: str, robot_index: int, node_index: int) -> None:
        reply = QMessageBox.question(
            self, "Delete Node",
            "Delete this node? Skins referencing its chambers will lose those chambers.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        robots_list = (
            self._settings.data.get("robots", {}).get(_YAML_KEY[robot_type], [])
        )
        if 0 <= robot_index < len(robots_list):
            nodes = robots_list[robot_index].get("nodes", [])
            if 0 <= node_index < len(nodes):
                nodes.pop(node_index)
                self._settings.save()
                self.robot_configured.emit()

    def _delete_skin(self, robot_type: str, robot_index: int, skin_index: int) -> None:
        reply = QMessageBox.question(
            self, "Delete Skin", "Delete this skin? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        robots_list = (
            self._settings.data.get("robots", {}).get(_YAML_KEY[robot_type], [])
        )
        if 0 <= robot_index < len(robots_list):
            skins = robots_list[robot_index].get("skins", [])
            if 0 <= skin_index < len(skins):
                skins.pop(skin_index)
                self._settings.save()
                self.robot_configured.emit()

    # ------------------------------------------------------------------
    # Thymio helpers
    # ------------------------------------------------------------------

    def _thymio_config_dialog(
        self, thymio_id: str = "", host: str = "localhost", port: int = 8596,
    ) -> tuple[str, str, int] | None:
        dlg  = QDialog(self)
        dlg.setWindowTitle("Thymio Configuration")
        form = QFormLayout()
        id_edit   = QLineEdit(thymio_id)
        host_edit = QLineEdit(host)
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(port)
        form.addRow("Thymio ID:", id_edit)
        form.addRow("Host:",      host_edit)
        form.addRow("Port:",      port_spin)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout = QVBoxLayout(dlg)
        layout.addLayout(form)
        layout.addWidget(btn_box)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        tid = id_edit.text().strip()
        if not tid:
            return None
        return tid, host_edit.text().strip() or "localhost", port_spin.value()

    def _on_configure_thymio(self, robot_index: int) -> None:
        robots_list = self._settings.data.get("robots", {}).get("thymios", [])
        if not (0 <= robot_index < len(robots_list)):
            return
        cfg    = robots_list[robot_index]
        result = self._thymio_config_dialog(
            thymio_id=cfg.get("thymio_id", ""),
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 8596)),
        )
        if result is None:
            return
        cfg["thymio_id"], cfg["host"], cfg["port"] = result
        self._settings.save()
        self.robot_configured.emit()

    def _on_add_robot(self, robot_type: str) -> None:
        if robot_type == "thymio":
            existing   = self._settings.data.get("robots", {}).get("thymios", [])
            default_id = f"thymio-{len(existing) + 1}"
            result     = self._thymio_config_dialog(thymio_id=default_id)
            if result is None:
                return
            tid, host, port = result
            entry: dict = {"thymio_id": tid, "host": host, "port": port,
                           "nodes": [], "skins": []}
        else:
            existing = (
                self._settings.data.get("robots", {}).get(_YAML_KEY[robot_type], [])
            )
            default  = f"{robot_type}-{len(existing) + 1}"
            name, ok = QInputDialog.getText(
                self, f"Add {robot_type.capitalize()}", "Robot ID:", text=default
            )
            if not ok or not name.strip():
                return
            entry = {"id": name.strip(), "nodes": [], "skins": []}

        (
            self._settings.data
            .setdefault("robots", {})
            .setdefault(_YAML_KEY[robot_type], [])
            .append(entry)
        )
        self._settings.save()
        self.robot_configured.emit()

    def _on_rename_robot(self, robot_type: str, robot_index: int) -> None:
        robots_list = (
            self._settings.data.get("robots", {}).get(_YAML_KEY[robot_type], [])
        )
        if not (0 <= robot_index < len(robots_list)):
            return
        robot_cfg = robots_list[robot_index]
        field, label = ("thymio_id", "Thymio ID") if robot_type == "thymio" else ("id", "Robot ID")
        current  = robot_cfg.get(field, "")
        new_name, ok = QInputDialog.getText(self, "Rename", f"{label}:", text=current)
        if ok and new_name.strip():
            robot_cfg[field] = new_name.strip()
            self._settings.save()
            self.robot_configured.emit()

    def _on_delete_robot(self, robot_type: str, robot_index: int) -> None:
        reply = QMessageBox.question(
            self, "Delete Robot", "Delete this robot and all its nodes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        robots_list = (
            self._settings.data.get("robots", {}).get(_YAML_KEY[robot_type], [])
        )
        if 0 <= robot_index < len(robots_list):
            robots_list.pop(robot_index)
            self._settings.save()
            self.robot_configured.emit()

"""Robot connection management panel."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.base_robot import BaseRobot

# Maps internal robot_type strings to the settings.yaml list key
_YAML_KEY = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}


class RobotPanel(QWidget):
    """Panel for managing robots and their ESP32 nodes.

    Displays a two-level tree per robot type:
      • top-level items  = robot entries (with an inline "+ Node" button)
      • child items      = ESP32 nodes with a ●/○ online indicator

    Double-clicking a node opens :class:`NodeConfigDialog`.
    Right-clicking a robot item offers Rename / Delete options.

    Signals:
        robot_configured: Emitted after any config change so the main
            window can reload settings and recreate robot objects.
    """

    robot_configured = Signal()

    def __init__(self, gateway: ESPNowGateway, settings: Settings):
        super().__init__()
        self._gateway = gateway
        self._settings = settings
        self._robots: list[BaseRobot] = []

        self._port_combo: QComboBox
        self._gateway_status: QLabel
        self._connect_btn: QPushButton
        self._scan_btn: QPushButton
        self._turtle_tree: QTreeWidget
        self._tree_tree: QTreeWidget
        self._thymio_tree: QTreeWidget

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_gateway_section())
        layout.addWidget(self._build_type_section("Turtles", "turtle"))
        layout.addWidget(self._build_type_section("Trees", "tree"))
        layout.addWidget(self._build_type_section("Thymios", "thymio"))
        layout.addStretch()

    def _build_gateway_section(self) -> QGroupBox:
        box = QGroupBox("ESP-NOW Gateway")
        hbox = QHBoxLayout(box)

        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(150)
        hbox.addWidget(self._port_combo)

        refresh_ports_btn = QPushButton("↺")
        refresh_ports_btn.setToolTip("Refresh serial port list")
        refresh_ports_btn.setFixedWidth(28)
        refresh_ports_btn.clicked.connect(self._refresh_ports)
        hbox.addWidget(refresh_ports_btn)

        self._gateway_status = QLabel("Disconnected")
        hbox.addWidget(self._gateway_status)
        hbox.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_gateway_connect)
        hbox.addWidget(self._connect_btn)

        self._scan_btn = QPushButton("Scan Nodes")
        self._scan_btn.setEnabled(False)
        self._scan_btn.clicked.connect(self._on_scan)
        hbox.addWidget(self._scan_btn)

        self._refresh_ports()
        return box

    def _build_type_section(self, title: str, robot_type: str) -> QGroupBox:
        box = QGroupBox(title)
        vbox = QVBoxLayout(box)

        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderHidden(True)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree.setColumnWidth(1, 72)
        tree.setRootIsDecorated(True)
        tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree, rt=robot_type: self._on_context_menu(pos, t, rt)
        )
        vbox.addWidget(tree)

        label = "Thymio" if robot_type == "thymio" else "Robot"
        add_btn = QPushButton(f"+ Add {label}")
        add_btn.clicked.connect(lambda _=False, rt=robot_type: self._on_add_robot(rt))
        vbox.addWidget(add_btn)

        if robot_type == "turtle":
            self._turtle_tree = tree
        elif robot_type == "tree":
            self._tree_tree = tree
        else:
            self._thymio_tree = tree

        return box

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def refresh(self, robots: list[BaseRobot]) -> None:
        """Repopulate all trees from current settings."""
        self._robots = robots
        self._refresh_all_trees()

    # ------------------------------------------------------------------
    # Gateway
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentText() or self._settings.gateway_port
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []

        self._port_combo.clear()
        if not ports:
            self._port_combo.addItem(current or "/dev/ttyUSB0")
        else:
            for p in ports:
                self._port_combo.addItem(p)
            if current and self._port_combo.findText(current) < 0:
                self._port_combo.insertItem(0, current)

        idx = self._port_combo.findText(current)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)

    def _on_gateway_connect(self) -> None:
        if self._gateway.is_connected:
            self._gateway.disconnect()
            self._gateway_status.setText("Disconnected")
            self._connect_btn.setText("Connect")
            self._scan_btn.setEnabled(False)
            self._refresh_all_trees()
        else:
            port = self._port_combo.currentText()
            self._gateway._port = port
            if self._gateway.connect():
                self._gateway_status.setText(f"Connected ({port})")
                self._connect_btn.setText("Disconnect")
                self._scan_btn.setEnabled(True)
            else:
                self._gateway_status.setText(f"Connection failed ({port})")

    def _on_scan(self) -> None:
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("Scanning…")
        self._gateway.scan()
        QTimer.singleShot(2000, self._on_scan_done)

    def _on_scan_done(self) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("Scan Nodes")
        self._refresh_all_trees()

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _refresh_all_trees(self) -> None:
        known = self._gateway.known_macs if self._gateway.is_connected else frozenset()
        robot_data = self._settings.data.get("robots", {})
        self._fill_tree(self._turtle_tree, "turtle", robot_data.get("turtles", []), known)
        self._fill_tree(self._tree_tree,   "tree",   robot_data.get("trees",   []), known)
        self._fill_tree(self._thymio_tree, "thymio", robot_data.get("thymios", []), known)

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
                "robot_type": robot_type,
                "robot_index": robot_index,
            })
            tree.addTopLevelItem(robot_item)

            # Inline "+ Node" button in column 1
            add_node_btn = QPushButton("+ Node")
            add_node_btn.setMaximumHeight(22)
            ri = robot_index
            add_node_btn.clicked.connect(
                lambda _=False, rt=robot_type, ridx=ri: self._on_add_node(rt, ridx)
            )
            tree.setItemWidget(robot_item, 1, add_node_btn)

            # Node children
            for node_index, node_cfg in enumerate(robot_cfg.get("nodes", [])):
                mac = node_cfg.get("mac", "")
                online = bool(mac and mac in known)
                dot = "●" if online else "○"
                node_item = QTreeWidgetItem([f"{dot}  {mac}"])
                node_item.setForeground(0, QColor("#2a9d2a") if online else QColor("#cc2222"))
                node_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "robot_type": robot_type,
                    "robot_index": robot_index,
                    "node_index": node_index,
                })
                robot_item.addChild(node_item)

            robot_item.setExpanded(True)

    # ------------------------------------------------------------------
    # Context menu (right-click on robot item)
    # ------------------------------------------------------------------

    def _on_context_menu(self, pos, tree: QTreeWidget, robot_type: str) -> None:
        item = tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None or "node_index" in data:
            return  # node items handled via double-click

        robot_index = data["robot_index"]
        menu = QMenu(self)
        rename_action = menu.addAction("Rename…")
        delete_action = menu.addAction("Delete Robot")
        action = menu.exec(tree.viewport().mapToGlobal(pos))

        if action == rename_action:
            self._on_rename_robot(robot_type, robot_index)
        elif action == delete_action:
            self._on_delete_robot(robot_type, robot_index)

    # ------------------------------------------------------------------
    # Dialog / action handlers
    # ------------------------------------------------------------------

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None or "node_index" not in data:
            return  # robot-level items — ignore double-click
        self._open_node_dialog(
            data["robot_type"], data["robot_index"], data["node_index"]
        )

    def _on_add_node(self, robot_type: str, robot_index: int) -> None:
        self._open_node_dialog(robot_type, robot_index, -1)

    def _open_node_dialog(
        self, robot_type: str, robot_index: int, node_index: int
    ) -> None:
        from src.gui.node_config_dialog import NodeConfigDialog

        dialog = NodeConfigDialog(
            robot_type=robot_type,
            robot_index=robot_index,
            node_index=node_index,
            settings=self._settings,
            gateway=self._gateway,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.robot_configured.emit()

    def _on_add_robot(self, robot_type: str) -> None:
        if robot_type == "thymio":
            existing = self._settings.data.get("robots", {}).get("thymios", [])
            default = f"thymio-{len(existing) + 1}"
            name, ok = QInputDialog.getText(self, "Add Thymio", "Thymio ID:", text=default)
            if not ok or not name.strip():
                return
            entry: dict = {"thymio_id": name.strip(), "nodes": []}
        else:
            existing = (
                self._settings.data.get("robots", {})
                .get(_YAML_KEY[robot_type], [])
            )
            default = f"{robot_type}-{len(existing) + 1}"
            name, ok = QInputDialog.getText(
                self, f"Add {robot_type.capitalize()}", "Robot ID:", text=default
            )
            if not ok or not name.strip():
                return
            entry = {"id": name.strip(), "nodes": []}

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

        if robot_type == "thymio":
            field, label = "thymio_id", "Thymio ID"
        else:
            field, label = "id", "Robot ID"

        current = robot_cfg.get(field, "")
        new_name, ok = QInputDialog.getText(self, "Rename", f"{label}:", text=current)
        if ok and new_name.strip():
            robot_cfg[field] = new_name.strip()
            self._settings.save()
            self.robot_configured.emit()

    def _on_delete_robot(self, robot_type: str, robot_index: int) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Robot",
            "Delete this robot and all its nodes?",
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

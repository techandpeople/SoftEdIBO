"""Per-robot configuration and actuator test dialog."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.robots.base_robot import BaseRobot
from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.tree.tree_robot import TreeRobot
from src.robots.turtle.turtle_robot import TurtleRobot


class RobotConfigDialog(QDialog):
    """Dialog for editing a robot's hardware configuration and testing its actuators.

    The top section shows editable skin entries loaded from ``settings.yaml``
    (skin ID, display name, MAC address, chamber slots).  The bottom section
    provides a sequential actuator test that inflates then deflates each
    chamber one at a time.

    Args:
        robot: Live robot instance.
        settings: Application settings (source of configuration data).
        parent: Optional parent widget.
    """

    def __init__(
        self,
        robot: BaseRobot,
        settings: Settings,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._robot = robot
        self._settings = settings

        # Tracked widget entries for save
        self._skin_entries: list[dict] = []
        self._thymio_entries: list[dict] = []

        self.setWindowTitle(f"Configure: {robot.name}")
        self.setMinimumSize(660, 540)

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(
            QLabel(f"<b>{type(robot).__name__}</b> — {robot.name}")
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        scroll.setWidget(content)
        main_layout.addWidget(scroll)

        if isinstance(robot, TurtleRobot):
            self._build_skin_config("turtle")
            self._build_test_section()
        elif isinstance(robot, TreeRobot):
            self._build_skin_config("tree")
            self._build_test_section()
        elif isinstance(robot, ThymioRobot):
            self._build_thymio_config()

        self._content_layout.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_save)
        btn_box.rejected.connect(self.reject)
        main_layout.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Sequential actuator test
    # ------------------------------------------------------------------

    def _run_sequential_test(self, btn: QPushButton) -> None:
        """Inflate then deflate each chamber of each skin, one at a time."""
        skins = getattr(self._robot, "skins", {})
        steps: list[tuple] = [
            (skin, slot)
            for skin in skins.values()
            for slot in sorted(skin.chambers)
        ]
        if not steps:
            return

        btn.setEnabled(False)
        btn.setText("Testing…")
        idx = [0]

        def _inflate() -> None:
            if idx[0] >= len(steps):
                btn.setEnabled(True)
                btn.setText("Test Actuators")
                return
            skin, slot = steps[idx[0]]
            skin.inflate(slot)
            QTimer.singleShot(500, _deflate)

        def _deflate() -> None:
            skin, slot = steps[idx[0]]
            skin.deflate(slot)
            idx[0] += 1
            QTimer.singleShot(1000, _inflate)

        _inflate()

    # ------------------------------------------------------------------
    # Skin config (Turtle + Tree share the same flat skins[] structure)
    # ------------------------------------------------------------------

    def _find_robot_cfg(self, robot_key: str) -> dict | None:
        """Find the settings dict for self._robot by matching robot_id."""
        yaml_key = {"turtle": "turtles", "tree": "trees"}[robot_key]
        robots_list = self._settings.data.get("robots", {}).get(yaml_key, [])
        for cfg in robots_list:
            if cfg.get("id") == self._robot.robot_id:
                return cfg
        return None

    def _build_skin_config(self, robot_key: str) -> None:
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout(config_group)

        # Robot ID row + inline test button
        id_row = QHBoxLayout()
        id_row.addWidget(QLabel(f"Robot ID: <b>{self._robot.robot_id}</b>"))
        id_row.addStretch()
        test_btn = QPushButton("Test Actuators")
        test_btn.clicked.connect(
            lambda _=False, b=test_btn: self._run_sequential_test(b)
        )
        id_row.addWidget(test_btn)
        config_layout.addLayout(id_row)

        robot_cfg = self._find_robot_cfg(robot_key) or {}
        for skin_cfg in robot_cfg.get("skins", []):
            self._add_skin_widgets(config_layout, skin_cfg)

        add_skin_btn = QPushButton("+ Add Skin")
        add_skin_btn.clicked.connect(
            lambda: self._add_skin_widgets(config_layout, None)
        )
        config_layout.addWidget(add_skin_btn)

        self._content_layout.addWidget(config_group)

    def _add_skin_widgets(
        self, parent_layout: QVBoxLayout, skin_cfg: dict | None
    ) -> None:
        skin_id = skin_cfg.get("skin_id", "") if skin_cfg else ""
        name = skin_cfg.get("name", skin_id) if skin_cfg else ""
        mac = skin_cfg.get("mac", "") if skin_cfg else ""
        active_slots = set(skin_cfg.get("slots", [])) if skin_cfg else set()

        skin_group = QGroupBox(f"Skin: {name or skin_id}")
        form = QFormLayout(skin_group)

        skin_id_edit = QLineEdit(skin_id)
        name_edit = QLineEdit(name)
        name_edit.textChanged.connect(
            lambda t, g=skin_group: g.setTitle(f"Skin: {t}")
        )
        mac_edit = QLineEdit(mac)

        form.addRow("Skin ID:", skin_id_edit)
        form.addRow("Name:", name_edit)
        form.addRow("MAC:", mac_edit)

        slot_checks: list[QCheckBox] = []
        slot_row = QHBoxLayout()
        for slot in range(3):
            cb = QCheckBox(f"Slot {slot}")
            cb.setChecked(slot in active_slots)
            slot_row.addWidget(cb)
            slot_checks.append(cb)
        slot_row.addStretch()
        form.addRow("Slots:", slot_row)

        del_btn = QPushButton("Delete Skin")
        form.addRow("", del_btn)

        entry: dict = {
            "skin_id_edit": skin_id_edit,
            "name_edit": name_edit,
            "mac_edit": mac_edit,
            "slot_checks": slot_checks,
            "group": skin_group,
            "deleted": False,
        }
        self._skin_entries.append(entry)

        def _delete_skin() -> None:
            entry["deleted"] = True
            skin_group.hide()

        del_btn.clicked.connect(_delete_skin)
        parent_layout.addWidget(skin_group)

    def _collect_skins(self) -> list[dict]:
        skins = []
        for se in self._skin_entries:
            if se["deleted"]:
                continue
            skin_id = se["skin_id_edit"].text().strip()
            name = se["name_edit"].text().strip() or skin_id
            mac = se["mac_edit"].text().strip()
            slots = [
                i for i, cb in enumerate(se["slot_checks"])
                if cb.isChecked()
            ]
            if skin_id and mac and slots:
                skins.append({"skin_id": skin_id, "name": name, "mac": mac, "slots": slots})
        return skins

    # ------------------------------------------------------------------
    # Test section (Turtle + Tree)
    # ------------------------------------------------------------------

    def _build_test_section(self) -> None:
        test_group = QGroupBox("Test Actuators")
        test_layout = QVBoxLayout(test_group)

        skins = getattr(self._robot, "skins", {})
        if not skins:
            test_layout.addWidget(
                QLabel("No skins available (robot not connected).")
            )
        else:
            # List skins and their chambers for reference
            for skin in skins.values():
                slots = sorted(skin.chambers)
                slot_str = ", ".join(f"Slot {s}" for s in slots)
                test_layout.addWidget(
                    QLabel(f"  {skin.name}: {slot_str}")
                )

            run_btn = QPushButton("Test Actuators")
            run_btn.clicked.connect(
                lambda _=False, b=run_btn: self._run_sequential_test(b)
            )
            test_layout.addWidget(run_btn)

        self._content_layout.addWidget(test_group)

    # ------------------------------------------------------------------
    # Thymio config
    # ------------------------------------------------------------------

    def _build_thymio_config(self) -> None:
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout(config_group)

        thymios = self._settings.data.get("robots", {}).get("thymios", [])
        for thymio_cfg in thymios:
            self._add_thymio_widgets(config_layout, thymio_cfg)

        add_btn = QPushButton("+ Add Thymio")
        add_btn.clicked.connect(
            lambda: self._add_thymio_widgets(config_layout, None)
        )
        config_layout.addWidget(add_btn)

        self._content_layout.addWidget(config_group)

    def _add_thymio_widgets(
        self, parent_layout: QVBoxLayout, thymio_cfg: dict | None
    ) -> None:
        thymio_id = thymio_cfg["thymio_id"] if thymio_cfg else ""
        host = thymio_cfg.get("host", "localhost") if thymio_cfg else "localhost"
        port = int(thymio_cfg.get("port", 8596)) if thymio_cfg else 8596
        mac = thymio_cfg.get("node_mac", "") if thymio_cfg else ""
        skins_cfg = thymio_cfg.get("skins", []) if thymio_cfg else []

        box = QGroupBox(f"Thymio: {thymio_id}")
        form = QFormLayout(box)

        id_edit = QLineEdit(thymio_id)
        id_edit.textChanged.connect(
            lambda t, g=box: g.setTitle(f"Thymio: {t}")
        )
        host_edit = QLineEdit(host)
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(port)
        mac_edit = QLineEdit(mac)

        form.addRow("Thymio ID:", id_edit)
        form.addRow("Host:", host_edit)
        form.addRow("Port:", port_spin)
        form.addRow("Node MAC:", mac_edit)

        # Test button only for the robot currently open
        is_current = thymio_cfg and thymio_cfg.get("thymio_id") == self._robot.robot_id
        if is_current:
            test_btn = QPushButton("Test Actuators")
            test_btn.clicked.connect(
                lambda _=False, b=test_btn: self._run_sequential_test(b)
            )
            form.addRow("", test_btn)

        del_btn = QPushButton("Delete")
        form.addRow("", del_btn)

        entry: dict = {
            "id_edit": id_edit,
            "host_edit": host_edit,
            "port_spin": port_spin,
            "mac_edit": mac_edit,
            "group": box,
            "skins_cfg": skins_cfg,
            "deleted": False,
        }
        self._thymio_entries.append(entry)

        def _delete_thymio() -> None:
            entry["deleted"] = True
            box.hide()

        del_btn.clicked.connect(_delete_thymio)
        parent_layout.addWidget(box)

    def _collect_thymios(self) -> list[dict]:
        thymios = []
        for te in self._thymio_entries:
            if te["deleted"]:
                continue
            thymio_id = te["id_edit"].text().strip()
            host = te["host_edit"].text().strip() or "localhost"
            port = te["port_spin"].value()
            mac = te["mac_edit"].text().strip()
            if thymio_id:
                thymios.append({
                    "thymio_id": thymio_id,
                    "host": host,
                    "port": port,
                    "node_mac": mac,
                    "skins": te["skins_cfg"],
                })
        return thymios

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        """Write edited configuration back to settings.yaml and close."""
        data = self._settings.data
        robots_data = data.setdefault("robots", {})

        if isinstance(self._robot, TurtleRobot):
            robot_cfg = self._find_robot_cfg("turtle")
            if robot_cfg is not None:
                robot_cfg["skins"] = self._collect_skins()
        elif isinstance(self._robot, TreeRobot):
            robot_cfg = self._find_robot_cfg("tree")
            if robot_cfg is not None:
                robot_cfg["skins"] = self._collect_skins()
        elif isinstance(self._robot, ThymioRobot):
            robots_data["thymios"] = self._collect_thymios()

        self._settings.save()
        self.accept()

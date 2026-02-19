"""Per-robot configuration and actuator test dialog."""

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

    The top section shows editable fields loaded from ``settings.yaml``
    (MAC addresses, skin IDs, chamber slots).  The bottom section provides
    inflate/deflate buttons that operate on the live robot object.

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
        self._node_entries: list[dict] = []
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
            self._build_node_config("turtle")
            self._build_turtle_test()
        elif isinstance(robot, TreeRobot):
            self._build_node_config("tree")
            self._build_tree_test()
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
    # Node-based config (Turtle + Tree share the same YAML structure)
    # ------------------------------------------------------------------

    def _build_node_config(self, robot_key: str) -> None:
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout(config_group)

        nodes = (
            self._settings.data
            .get("robots", {})
            .get(robot_key, {})
            .get("nodes", [])
        )
        for node_cfg in nodes:
            self._add_node_widgets(config_layout, node_cfg)

        add_node_btn = QPushButton("+ Add Node")
        add_node_btn.clicked.connect(
            lambda: self._add_node_widgets(config_layout, None)
        )
        config_layout.addWidget(add_node_btn)

        self._content_layout.addWidget(config_group)

    def _add_node_widgets(
        self, parent_layout: QVBoxLayout, node_cfg: dict | None
    ) -> None:
        mac = node_cfg["mac"] if node_cfg else ""
        skins = node_cfg.get("skins", []) if node_cfg else []

        node_group = QGroupBox(f"Node: {mac}")
        node_outer = QVBoxLayout(node_group)

        mac_row = QHBoxLayout()
        mac_row.addWidget(QLabel("MAC:"))
        mac_edit = QLineEdit(mac)
        mac_edit.textChanged.connect(
            lambda t, g=node_group: g.setTitle(f"Node: {t}")
        )
        mac_row.addWidget(mac_edit)
        del_node_btn = QPushButton("Delete Node")
        mac_row.addWidget(del_node_btn)
        node_outer.addLayout(mac_row)

        skin_container = QWidget()
        skin_layout = QVBoxLayout(skin_container)
        skin_layout.setContentsMargins(0, 0, 0, 0)
        node_outer.addWidget(skin_container)

        entry: dict = {
            "mac_edit": mac_edit,
            "group": node_group,
            "skin_entries": [],
            "skin_layout": skin_layout,
            "deleted": False,
        }

        for skin_cfg in skins:
            se = self._add_skin_widgets(skin_layout, skin_cfg)
            entry["skin_entries"].append(se)

        add_skin_btn = QPushButton("+ Add Skin")
        add_skin_btn.clicked.connect(
            lambda: entry["skin_entries"].append(
                self._add_skin_widgets(entry["skin_layout"], None)
            )
        )
        node_outer.addWidget(add_skin_btn)

        self._node_entries.append(entry)

        def _delete_node() -> None:
            entry["deleted"] = True
            node_group.hide()

        del_node_btn.clicked.connect(_delete_node)
        parent_layout.addWidget(node_group)

    def _add_skin_widgets(
        self, parent_layout: QVBoxLayout, skin_cfg: dict | None
    ) -> dict:
        skin_id = skin_cfg["skin_id"] if skin_cfg else ""
        active_slots = set(skin_cfg.get("slots", [])) if skin_cfg else set()

        skin_group = QGroupBox(f"Skin: {skin_id}")
        skin_layout = QVBoxLayout(skin_group)

        header = QHBoxLayout()
        header.addWidget(QLabel("Skin ID:"))
        id_edit = QLineEdit(skin_id)
        id_edit.textChanged.connect(
            lambda t, g=skin_group: g.setTitle(f"Skin: {t}")
        )
        header.addWidget(id_edit)

        slot_checks: list[QCheckBox] = []
        for slot in range(3):
            cb = QCheckBox(f"Slot {slot}")
            cb.setChecked(slot in active_slots)
            header.addWidget(cb)
            slot_checks.append(cb)

        del_btn = QPushButton("Delete")
        header.addWidget(del_btn)
        skin_layout.addLayout(header)

        entry: dict = {
            "skin_id_edit": id_edit,
            "slot_checks": slot_checks,
            "group": skin_group,
            "deleted": False,
        }

        def _delete_skin() -> None:
            entry["deleted"] = True
            skin_group.hide()

        del_btn.clicked.connect(_delete_skin)
        parent_layout.addWidget(skin_group)
        return entry

    def _collect_nodes(self) -> list[dict]:
        nodes = []
        for ne in self._node_entries:
            if ne["deleted"]:
                continue
            mac = ne["mac_edit"].text().strip()
            skins = []
            for se in ne["skin_entries"]:
                if se["deleted"]:
                    continue
                slots = [
                    i for i, cb in enumerate(se["slot_checks"])
                    if cb.isChecked()
                ]
                skin_id = se["skin_id_edit"].text().strip()
                if skin_id and slots:
                    skins.append({"skin_id": skin_id, "slots": slots})
            if mac:
                nodes.append({"mac": mac, "skins": skins})
        return nodes

    # ------------------------------------------------------------------
    # Turtle test
    # ------------------------------------------------------------------

    def _build_turtle_test(self) -> None:
        test_group = QGroupBox("Test Actuators")
        test_layout = QVBoxLayout(test_group)

        if not isinstance(self._robot, TurtleRobot) or not self._robot.skins:
            test_layout.addWidget(
                QLabel("No skins available (robot not connected).")
            )
        else:
            for skin_id, skin in self._robot.skins.items():
                skin_box = QGroupBox(skin_id)
                skin_box_layout = QVBoxLayout(skin_box)

                all_row = QHBoxLayout()
                inflate_all_btn = QPushButton("Inflate All")
                deflate_all_btn = QPushButton("Deflate All")
                inflate_all_btn.clicked.connect(
                    lambda _=False, s=skin: s.inflate()
                )
                deflate_all_btn.clicked.connect(
                    lambda _=False, s=skin: s.deflate()
                )
                all_row.addWidget(inflate_all_btn)
                all_row.addWidget(deflate_all_btn)
                all_row.addStretch()
                skin_box_layout.addLayout(all_row)

                for slot in sorted(skin.chambers.keys()):
                    slot_row = QHBoxLayout()
                    slot_row.addWidget(QLabel(f"  Chamber {slot}:"))
                    inf_btn = QPushButton("Inflate")
                    def_btn = QPushButton("Deflate")
                    inf_btn.clicked.connect(
                        lambda _=False, s=skin, sl=slot: s.inflate(sl)
                    )
                    def_btn.clicked.connect(
                        lambda _=False, s=skin, sl=slot: s.deflate(sl)
                    )
                    slot_row.addWidget(inf_btn)
                    slot_row.addWidget(def_btn)
                    slot_row.addStretch()
                    skin_box_layout.addLayout(slot_row)

                test_layout.addWidget(skin_box)

        self._content_layout.addWidget(test_group)

    # ------------------------------------------------------------------
    # Tree test
    # ------------------------------------------------------------------

    def _build_tree_test(self) -> None:
        test_group = QGroupBox("Test Actuators")
        test_layout = QVBoxLayout(test_group)

        if not isinstance(self._robot, TreeRobot) or not self._robot.branches:
            test_layout.addWidget(
                QLabel("No branches available (robot not connected).")
            )
        else:
            for branch_id, branch in self._robot.branches.items():
                row = QHBoxLayout()
                row.addWidget(QLabel(f"Branch {branch_id}:"))
                inf_btn = QPushButton("Inflate")
                def_btn = QPushButton("Deflate")
                inf_btn.clicked.connect(
                    lambda _=False, b=branch: b.inflate()
                )
                def_btn.clicked.connect(
                    lambda _=False, b=branch: b.deflate()
                )
                row.addWidget(inf_btn)
                row.addWidget(def_btn)
                row.addStretch()
                test_layout.addLayout(row)

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
        mac = thymio_cfg.get("node_mac", "") if thymio_cfg else ""
        skins_cfg = thymio_cfg.get("skins", []) if thymio_cfg else []

        box = QGroupBox(f"Thymio: {thymio_id}")
        form = QFormLayout(box)

        id_edit = QLineEdit(thymio_id)
        id_edit.textChanged.connect(
            lambda t, g=box: g.setTitle(f"Thymio: {t}")
        )
        mac_edit = QLineEdit(mac)
        form.addRow("Thymio ID:", id_edit)
        form.addRow("Node MAC:", mac_edit)

        del_btn = QPushButton("Delete")
        form.addRow("", del_btn)

        entry: dict = {
            "id_edit": id_edit,
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
            mac = te["mac_edit"].text().strip()
            if thymio_id:
                thymios.append({
                    "thymio_id": thymio_id,
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
            robots_data["turtle"] = {"nodes": self._collect_nodes()}
        elif isinstance(self._robot, TreeRobot):
            robots_data["tree"] = {"nodes": self._collect_nodes()}
        elif isinstance(self._robot, ThymioRobot):
            robots_data["thymios"] = self._collect_thymios()

        self._settings.save()
        self.accept()

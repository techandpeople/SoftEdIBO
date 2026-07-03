"""Per-robot configuration and actuator test dialog."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import Settings
from src.gui.base_dialog import BaseDialog
from src.gui.thymio_config_form import ThymioConfigForm
from src.gui.ui_robot_config_dialog import Ui_RobotConfigDialog
from src.robots.base_robot import BaseRobot
from src.robots.esp_robot import EspRobot
from src.robots.thymio.thymio_robot import ThymioRobot

_TEST_ACTUATORS = "Test Actuators"
_TEST_DRIVE = "Test Drive"


class RobotConfigDialog(BaseDialog, Ui_RobotConfigDialog):
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
        self.setupUi(self)
        self._robot = robot
        self._settings = settings

        # Tracked widget entries for save
        self._skin_entries: list[dict] = []
        self._thymio_entries: list[dict] = []

        self.setWindowTitle(f"Configure: {robot.name}")
        self.intro_label.setText(f"<b>{type(robot).__name__}</b> — {robot.name}")

        # The static frame (intro, scroll area, button box) lives in the .ui;
        # the per-robot config groups are built here into ``content_layout``.
        # Thymio first: it is an EspRobot too, but configures its wheeled base.
        if isinstance(robot, ThymioRobot):
            self._build_thymio_config()
        elif isinstance(robot, EspRobot):
            self._build_skin_config(robot.robot_kind)
            self._build_test_section()

        self.content_layout.addStretch()

        self.button_box.accepted.connect(self._on_save)
        self.button_box.rejected.connect(self.reject)
        apply_btn = self.button_box.button(
            QDialogButtonBox.StandardButton.Apply)
        apply_btn.setWhatsThis(
            "Write the edited configuration to settings.yaml without closing "
            "this window, so you can keep editing. Same as Save but leaves the "
            "dialog open.")
        apply_btn.clicked.connect(lambda: self._commit())

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
                btn.setText(_TEST_ACTUATORS)
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

    def _run_drive_test(self, btn: QPushButton) -> None:
        """Quick wheeled-base check: forward ~0.8 s with the top LED green,
        then stop and LED off. Exercises the whole link (dongle or gateway C6)."""
        robot = self._robot
        btn.setEnabled(False)
        btn.setText("Driving…")
        robot.set_leds(0, 32, 0)
        robot.set_motors(150, 150)

        def _stop() -> None:
            robot.set_motors(0, 0)
            robot.set_leds(0, 0, 0)
            btn.setEnabled(True)
            btn.setText(_TEST_DRIVE)

        QTimer.singleShot(800, _stop)

    # ------------------------------------------------------------------
    # Skin config (Turtle & Tree share the same flat skins[] structure)
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
        test_btn = QPushButton(_TEST_ACTUATORS)
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

        self.content_layout.addWidget(config_group)

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
    # Test section (Turtle & Tree)
    # ------------------------------------------------------------------

    def _build_test_section(self) -> None:
        test_group = QGroupBox(_TEST_ACTUATORS)
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
                    QLabel(f"  {skin.skin_id}: {slot_str}")
                )

            run_btn = QPushButton(_TEST_ACTUATORS)
            run_btn.clicked.connect(
                lambda _=False, b=run_btn: self._run_sequential_test(b)
            )
            test_layout.addWidget(run_btn)

        self.content_layout.addWidget(test_group)

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

        self.content_layout.addWidget(config_group)

    def _add_thymio_widgets(
        self, parent_layout: QVBoxLayout, thymio_cfg: dict | None
    ) -> None:
        thymio_id = thymio_cfg["thymio_id"] if thymio_cfg else ""
        skins_cfg = thymio_cfg.get("skins", []) if thymio_cfg else []

        box = QGroupBox(f"Thymio: {thymio_id}")
        layout = QVBoxLayout(box)

        form = ThymioConfigForm(
            lambda: getattr(self._robot, "gateway", None), box)
        if thymio_cfg:
            form.set_values(thymio_cfg)
        form.id_edit.textChanged.connect(
            lambda t, g=box: g.setTitle(f"Thymio: {t}")
        )
        layout.addWidget(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        # Test buttons only for the robot currently open
        is_current = thymio_cfg and thymio_cfg.get("thymio_id") == self._robot.robot_id
        if is_current:
            drive_btn = QPushButton(_TEST_DRIVE)
            drive_btn.setWhatsThis(
                "Quick wheeled-base check: drives this Thymio forward for about a "
                "second with the top LED green, then stops. Needs 'Drive wheels "
                "wirelessly' on and the transport reachable (dongle plugged / "
                "gateway connected)."
            )
            drive_btn.clicked.connect(
                lambda _=False, b=drive_btn: self._run_drive_test(b)
            )
            buttons.addWidget(drive_btn)
            test_btn = QPushButton(_TEST_ACTUATORS)
            test_btn.clicked.connect(
                lambda _=False, b=test_btn: self._run_sequential_test(b)
            )
            buttons.addWidget(test_btn)

        del_btn = QPushButton("Delete")
        buttons.addWidget(del_btn)
        layout.addLayout(buttons)

        entry: dict = {
            "form": form,
            "group": box,
            # The original settings entry: keys the form doesn't edit (the
            # robot's "+ Node" nodes list, notably) must survive a save.
            "cfg": dict(thymio_cfg or {}),
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
            values = te["form"].values()
            if values["thymio_id"]:
                cfg = {**te["cfg"], **values, "skins": te["skins_cfg"]}
                # Dead keys from configs saved before the dongle/gateway
                # transports (TDM host/port, unused per-robot node_mac).
                for stale in ("host", "port", "node_mac"):
                    cfg.pop(stale, None)
                thymios.append(cfg)
        return thymios

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _commit(self) -> None:
        """Write edited configuration back to settings.yaml, without closing.
        Shared by Save (which then closes) and Apply (which stays open)."""
        data = self._settings.data
        robots_data = data.setdefault("robots", {})

        if isinstance(self._robot, ThymioRobot):
            robots_data["thymios"] = self._collect_thymios()
        elif isinstance(self._robot, EspRobot):
            robot_cfg = self._find_robot_cfg(self._robot.robot_kind)
            if robot_cfg is not None:
                robot_cfg["skins"] = self._collect_skins()

        self._settings.save()

    def _on_save(self) -> None:
        """Write edited configuration back to settings.yaml and close."""
        self._commit()
        self.accept()

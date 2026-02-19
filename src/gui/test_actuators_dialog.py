"""Test actuators dialog — inflate/deflate individual chambers via the gateway."""

from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.hardware.espnow_gateway import ESPNowGateway


class TestActuatorsDialog(QDialog):
    """Dialog for sending inflate/deflate commands to a node's chambers.

    Commands are sent directly via the gateway without going through the
    robot layer, so the dialog works with the current (possibly unsaved)
    node configuration.

    Args:
        mac: Target ESP32 MAC address.
        skin_cfgs: List of skin config dicts (``skin_id`` + ``slots``).
        gateway: Connected ESP-NOW gateway.
        parent: Optional parent widget.
    """

    def __init__(
        self,
        mac: str,
        skin_cfgs: list[dict],
        gateway: ESPNowGateway,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._mac = mac
        self._gateway = gateway
        self.setWindowTitle(f"Test Actuators — {mac}")
        self.setMinimumSize(420, 200)
        self._build_ui(skin_cfgs)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, skin_cfgs: list[dict]) -> None:
        main_layout = QVBoxLayout(self)

        if not skin_cfgs:
            main_layout.addWidget(QLabel("No skins configured for this node."))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            content_layout = QVBoxLayout(content)
            scroll.setWidget(content)

            for skin_cfg in skin_cfgs:
                content_layout.addWidget(self._build_skin_group(skin_cfg))
            content_layout.addStretch()
            main_layout.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        main_layout.addLayout(row)

    def _build_skin_group(self, skin_cfg: dict) -> QGroupBox:
        skin_id = skin_cfg.get("skin_id", "—")
        slots: list[int] = sorted(skin_cfg.get("slots", []))

        box = QGroupBox(f"Skin: {skin_id}")
        vbox = QVBoxLayout(box)

        # Inflate All / Deflate All row
        all_row = QHBoxLayout()
        inf_all = QPushButton("Inflate All")
        def_all = QPushButton("Deflate All")
        inf_all.clicked.connect(lambda _=False, sl=slots: self._inflate_slots(sl))
        def_all.clicked.connect(lambda _=False, sl=slots: self._deflate_slots(sl))
        all_row.addWidget(inf_all)
        all_row.addWidget(def_all)
        all_row.addStretch()
        vbox.addLayout(all_row)

        # Per-slot rows
        for slot in slots:
            slot_row = QHBoxLayout()
            slot_row.addWidget(QLabel(f"  Slot {slot}:"))
            inf_btn = QPushButton("Inflate")
            def_btn = QPushButton("Deflate")
            inf_btn.clicked.connect(lambda _=False, s=slot: self._inflate_slot(s))
            def_btn.clicked.connect(lambda _=False, s=slot: self._deflate_slot(s))
            slot_row.addWidget(inf_btn)
            slot_row.addWidget(def_btn)
            slot_row.addStretch()
            vbox.addLayout(slot_row)

        return box

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _inflate_slot(self, slot: int) -> None:
        self._gateway.send(self._mac, "inflate", chamber=slot, value=255)

    def _deflate_slot(self, slot: int) -> None:
        self._gateway.send(self._mac, "deflate", chamber=slot)

    def _inflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._inflate_slot(slot)

    def _deflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._deflate_slot(slot)

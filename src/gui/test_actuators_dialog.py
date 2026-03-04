"""Test actuators dialog — inflate/deflate individual chambers via the gateway."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.ui_test_actuators_dialog import Ui_TestActuatorsDialog
from src.hardware.espnow_gateway import ESPNowGateway


class TestActuatorsDialog(QDialog, Ui_TestActuatorsDialog):
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

    # Emitted from the gateway read thread; connected to _update_pressure (main thread)
    _pressure_received = Signal(int, int)   # chamber, pressure_adc

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
        self._active = True
        self._pressure_labels: dict[int, QLabel] = {}   # slot => label

        self.setupUi(self)
        self.setWindowTitle(f"Test Actuators — {mac}")
        self.close_btn.clicked.connect(self.accept)

        if not skin_cfgs:
            self.no_chambers_label.setVisible(True)
        else:
            self.chambers_scroll.setVisible(True)
            for skin_cfg in skin_cfgs:
                self.chambers_vbox.addWidget(self._build_chamber_group(skin_cfg))
            self.chambers_vbox.addStretch()

        self._pressure_received.connect(self._update_pressure)
        self._gateway.on_message(self._on_gateway_message)
        self.finished.connect(self._on_closed)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_chamber_group(self, skin_cfg: dict) -> QGroupBox:
        skin_id = skin_cfg.get("skin_id", "—")
        slots: list[int] = sorted(skin_cfg.get("slots", []))

        box = QGroupBox(f"Air Chamber: {skin_id}")
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
            pressure_lbl = QLabel("—")
            pressure_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pressure_lbl.setMinimumWidth(110)
            slot_row.addWidget(pressure_lbl)
            self._pressure_labels[slot] = pressure_lbl
            vbox.addLayout(slot_row)

        return box

    # ------------------------------------------------------------------
    # Pressure updates (gateway callback => signal => main thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        """Called from the gateway read thread."""
        if not self._active:
            return
        if data.get("source") != self._mac or data.get("type") != "status":
            return
        chamber = data.get("chamber")
        pressure = data.get("pressure")
        if isinstance(chamber, int) and isinstance(pressure, int):
            self._pressure_received.emit(chamber, pressure)

    def _update_pressure(self, chamber: int, pressure: int) -> None:
        """Called in the main thread via Signal."""
        lbl = self._pressure_labels.get(chamber)
        if lbl:
            lbl.setText(f"ADC: {pressure}")

    def _on_closed(self) -> None:
        self._active = False

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

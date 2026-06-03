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

from src.gui.led_ring_tester import LedRingTester
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
        led_count: int = 24,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._mac = mac
        self._gateway = gateway
        self._active = True
        self._pressure_labels: dict[int, QLabel] = {}   # slot => label
        self._valve_states: dict[tuple[int, int], tuple[bool, QPushButton]] = {}  # (slot, side) => (open, button)

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

        # WS2812 LED ring tester (node_direct boards). Insert before the
        # Close button (the last widget in the dialog's vertical layout).
        if led_count > 0:
            self._led_tester = LedRingTester(led_count, self._send_led)
            self.verticalLayout.insertWidget(
                self.verticalLayout.count() - 1, self._led_tester)

        # Pump controls (toggle style, monospace font for fixed width)
        self._pump_states: dict[int, tuple[bool, QPushButton]] = {}  # pump => (on, button)
        pump_group = QGroupBox("Pump Control")
        pump_layout = QHBoxLayout(pump_group)
        monospace_style = "font-family: Courier; font-size: 10pt;"

        pump_inf_btn = QPushButton("Inflate Pump: OFF")
        pump_inf_btn.setMaximumWidth(160)
        pump_inf_btn.setStyleSheet(monospace_style)
        pump_inf_btn.clicked.connect(lambda _=False, p=0, btn=pump_inf_btn: self._toggle_pump(p, btn))
        self._pump_states[0] = (False, pump_inf_btn)
        pump_layout.addWidget(pump_inf_btn)

        pump_def_btn = QPushButton("Deflate Pump: OFF")
        pump_def_btn.setMaximumWidth(160)
        pump_def_btn.setStyleSheet(monospace_style)
        pump_def_btn.clicked.connect(lambda _=False, p=1, btn=pump_def_btn: self._toggle_pump(p, btn))
        self._pump_states[1] = (False, pump_def_btn)
        pump_layout.addWidget(pump_def_btn)

        stop_all_btn = QPushButton("⏹ STOP ALL (Close valves + Off pumps)")
        stop_all_btn.setStyleSheet("background-color: #FF6B6B; font-weight: bold;")
        stop_all_btn.clicked.connect(self._stop_all)
        pump_layout.addWidget(stop_all_btn)

        pump_layout.addStretch()
        self.verticalLayout.insertWidget(
            self.verticalLayout.count() - 1, pump_group)

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

            # Manual valve toggle controls (monospace font for fixed width)
            monospace_style = "font-family: Courier; font-size: 10pt;"

            val_inf_btn = QPushButton("Inflate Valve: CLOSED")
            val_inf_btn.setMaximumWidth(180)
            val_inf_btn.setStyleSheet(monospace_style)
            val_inf_btn.clicked.connect(lambda _=False, s=slot, btn=val_inf_btn: self._toggle_valve(s, 0, btn))
            self._valve_states[(slot, 0)] = (False, val_inf_btn)
            slot_row.addWidget(val_inf_btn)

            val_def_btn = QPushButton("Deflate Valve: CLOSED")
            val_def_btn.setMaximumWidth(180)
            val_def_btn.setStyleSheet(monospace_style)
            val_def_btn.clicked.connect(lambda _=False, s=slot, btn=val_def_btn: self._toggle_valve(s, 1, btn))
            self._valve_states[(slot, 1)] = (False, val_def_btn)
            slot_row.addWidget(val_def_btn)

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

    def _send_led(self, index: int | None, color_hex: str | None, pattern: str = "solid") -> None:
        """Forward an LED change to the node. color_hex None => turn off;
        index None => whole ring; otherwise a single pixel."""
        if color_hex is None:
            self._gateway.send(self._mac, "set_led", pattern="off")
        elif index is None:
            self._gateway.send(self._mac, "set_led", color=color_hex, pattern=pattern)
        else:
            self._gateway.send(self._mac, "set_led", color=color_hex,
                               index=index, pattern=pattern)

    def _inflate_slot(self, slot: int) -> None:
        self._gateway.send(self._mac, "inflate", chamber=slot, value=255)
        self._update_pump_button(0, True)  # Inflate pump is now ON

    def _deflate_slot(self, slot: int) -> None:
        self._gateway.send(self._mac, "deflate", chamber=slot)
        self._update_pump_button(1, True)  # Deflate pump is now ON

    def _inflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._inflate_slot(slot)

    def _deflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._deflate_slot(slot)

    def _toggle_valve(self, chamber: int, side: int, btn: QPushButton) -> None:
        """Toggle valve open/closed and update button appearance."""
        key = (chamber, side)
        is_open, _ = self._valve_states.get(key, (False, btn))
        is_open = not is_open

        # Update state
        self._valve_states[key] = (is_open, btn)

        # Update button appearance (fixed-width for consistent size)
        side_name = "Inflate" if side == 0 else "Deflate"
        status = "OPEN  " if is_open else "CLOSED"
        btn.setText(f"{side_name} Valve: {status}")

        # Send command to firmware
        self._gateway.send(self._mac, "valve_manual", chamber=chamber,
                          side=side, open=1 if is_open else 0)

    def _toggle_pump(self, pump: int, btn: QPushButton) -> None:
        """Toggle pump on/off and update button appearance."""
        is_on, _ = self._pump_states.get(pump, (False, btn))
        is_on = not is_on

        # Update state
        self._pump_states[pump] = (is_on, btn)

        # Update button appearance (fixed-width for consistent size)
        pump_name = "Inflate" if pump == 0 else "Deflate"
        status = "ON " if is_on else "OFF"
        btn.setText(f"{pump_name} Pump: {status}")

        # Send command to firmware
        self._gateway.send(self._mac, "pump_manual", pump=pump, on=1 if is_on else 0)

    def _update_pump_button(self, pump: int, is_on: bool) -> None:
        """Update pump button state (e.g., when inflate/deflate changes the pump state)."""
        if pump not in self._pump_states:
            return
        _, btn = self._pump_states[pump]
        self._pump_states[pump] = (is_on, btn)
        pump_name = "Inflate" if pump == 0 else "Deflate"
        status = "ON " if is_on else "OFF"
        btn.setText(f"{pump_name} Pump: {status}")

    def _stop_all(self) -> None:
        """Close all valves and turn off all pumps."""
        # Close all valves
        for key in self._valve_states:
            chamber, side = key
            _, btn = self._valve_states[key]
            self._valve_states[key] = (False, btn)
            side_name = "Inflate" if side == 0 else "Deflate"
            btn.setText(f"{side_name} Valve: CLOSED")
            self._gateway.send(self._mac, "valve_manual", chamber=chamber, side=side, open=0)

        # Turn off all pumps
        for pump in self._pump_states:
            _, btn = self._pump_states[pump]
            self._pump_states[pump] = (False, btn)
            pump_name = "Inflate" if pump == 0 else "Deflate"
            btn.setText(f"{pump_name} Pump: OFF")
            self._gateway.send(self._mac, "pump_manual", pump=pump, on=0)

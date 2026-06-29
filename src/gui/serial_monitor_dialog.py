"""Raw serial debugging terminal for the ESP-NOW gateway.

Taps the gateway's raw serial stream (see ``ESPNowGateway.on_raw``) and shows
every line to/from the gateway, with an input box for sending arbitrary lines.
"""

import time

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget

from src.gui.base_dialog import BaseDialog
from src.gui.ui_serial_monitor_dialog import Ui_SerialMonitorDialog
from src.hardware.espnow_gateway import ESPNowGateway

_PREFIX = {"rx": "«", "tx": "»"}


class SerialMonitorDialog(BaseDialog, Ui_SerialMonitorDialog):
    """Live raw serial monitor for the gateway link.

    The gateway's RX tap fires on its read thread, so incoming lines are routed
    through ``_line_received`` (a queued Qt signal) to reach the GUI thread
    safely. Outgoing (TX) lines come from the GUI thread directly.
    """

    # Marshals raw lines from the gateway read thread onto the GUI thread.
    _line_received = Signal(str, str)

    def __init__(self, gateway: ESPNowGateway, parent: QWidget | None = None):
        super().__init__(parent)
        self._gateway = gateway
        self.setupUi(self)

        self.output.setFont(QFont("monospace"))

        self.send_btn.clicked.connect(self._on_send)
        self.input_edit.returnPressed.connect(self._on_send)
        self.clear_btn.clicked.connect(self.output.clear)
        self.close_btn.clicked.connect(self.accept)

        self._line_received.connect(self._append)
        self._gateway.on_raw(self._on_raw)

        self._update_status()

    # ------------------------------------------------------------------
    # Gateway tap (may run on the serial read thread)
    # ------------------------------------------------------------------

    def _on_raw(self, direction: str, text: str) -> None:
        # Emitting a signal is thread-safe; the slot runs on the GUI thread.
        self._line_received.emit(direction, text)

    # ------------------------------------------------------------------
    # GUI thread
    # ------------------------------------------------------------------

    def _append(self, direction: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.output.appendPlainText(f"{stamp} {_PREFIX.get(direction, '?')} {text}")
        if self.autoscroll_check.isChecked():
            self.output.verticalScrollBar().setValue(
                self.output.verticalScrollBar().maximum()
            )

    def _on_send(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        if self._gateway.send_raw(text):
            self.input_edit.clear()
        else:
            self._append("rx", "[not sent — gateway disconnected]")
        self._update_status()

    def _update_status(self) -> None:
        connected = self._gateway.is_connected
        self.status_label.setText(
            f"Connected ({self._gateway._port})" if connected else "Disconnected"
        )
        self.input_edit.setEnabled(connected)
        self.send_btn.setEnabled(connected)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def done(self, result: int) -> None:
        # Detach the tap so the gateway stops calling into a dead dialog.
        self._gateway.remove_raw_callback(self._on_raw)
        super().done(result)

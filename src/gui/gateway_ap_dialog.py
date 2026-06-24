"""Tools => Gateway WiFi AP… — configure the SoftAP the gateway broadcasts.

Only meaningful for a gateway flashed with the access-point firmware build
(``-DGATEWAY_AP``). The dialog reads the current network name from the live
gateway (``get_ap``) and writes changes back (``set_ap``); the firmware persists
them in NVS so they survive reboots. The gateway must be connected — it is owned
by ``MainWindow`` (auto-connect on startup or via the Robots tab).

Gateway replies arrive on the serial read thread, so they are bounced to the GUI
thread through a Qt signal before touching widgets.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit

from src.gui.ui_gateway_ap_dialog import Ui_GatewayApDialog
from src.hardware.espnow_gateway import ESPNowGateway

logger = logging.getLogger(__name__)

# How long to wait for the gateway's get_ap reply before assuming no response.
_LOAD_TIMEOUT_MS = 2500

_SAVE_ERRORS = {
    "empty_ssid": "The network name can't be empty.",
    "bad_password": "Password must be at least 8 characters (or left blank to "
                    "keep the current one).",
}


class GatewayApDialog(QDialog, Ui_GatewayApDialog):
    """Read/write the gateway's WiFi access-point name and password."""

    # Marshals a gateway message from the serial read thread to the GUI thread.
    _message_received = Signal(dict)

    def __init__(self, gateway: ESPNowGateway, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._gateway = gateway
        self._loaded = False

        self._save_btn = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        self.button_box.button(
            QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        self.show_pass_check.toggled.connect(self._on_show_pass)

        self._message_received.connect(self._handle_message)
        self._gateway.on_message(self._on_gateway_message)

        if not self._gateway.is_connected:
            self._set_status("Gateway not connected. Connect it first, then "
                             "reopen this dialog.")
            self._set_inputs_enabled(False)
            return

        self._set_inputs_enabled(False)
        self._gateway.send_gateway("get_ap")
        QTimer.singleShot(_LOAD_TIMEOUT_MS, self._on_load_timeout)

    # ------------------------------------------------------------------
    # Gateway messaging
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict[str, Any]) -> None:
        """Serial-read-thread callback — hand off to the GUI thread."""
        self._message_received.emit(data)

    def _handle_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")
        if msg_type == "ap_config":
            self._loaded = True
            self.ssid_edit.setText(data.get("ssid", ""))
            self.pass_edit.clear()  # firmware never returns the stored password
            secured = data.get("secured", True)
            self._set_status(
                "Connected. Edit the network name or password and click Save."
                + ("" if secured else "  (currently an open network)"))
            self._set_inputs_enabled(True)
            self._save_btn.setEnabled(True)
        elif msg_type == "error" and data.get("reason") == "ap_not_supported":
            self._loaded = True
            self._set_status(
                "This gateway is not running the access-point firmware. Re-flash "
                "it with the AP build (Tools → Flash Firmware → 'Run as WiFi "
                "access point for Thymio robots').")
            self._set_inputs_enabled(False)
        elif msg_type == "ap_set":
            self._save_btn.setEnabled(True)
            if data.get("ok"):
                self._set_status(f"Saved. Network is now “{data.get('ssid', '')}”.")
                self.pass_edit.clear()
            else:
                reason = data.get("reason", "unknown")
                self._set_status("Could not save: "
                                 + _SAVE_ERRORS.get(reason, reason))

    def _on_load_timeout(self) -> None:
        if not self._loaded:
            self._set_status("No response from the gateway. Make sure it is "
                             "connected and running.")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        ssid = self.ssid_edit.text().strip()
        if not ssid:
            self._set_status("Enter a network name first.")
            return
        # Empty password field means "keep the current password" — omit it so
        # the firmware leaves the stored value untouched (rather than opening
        # the network). A typed password replaces it.
        kwargs: dict[str, Any] = {"ssid": ssid}
        password = self.pass_edit.text()
        if password:
            kwargs["pass"] = password

        if not self._gateway.send_gateway("set_ap", **kwargs):
            self._set_status("Gateway not connected — nothing was saved.")
            return
        self._save_btn.setEnabled(False)
        self._set_status("Saving…")

    def _on_show_pass(self, checked: bool) -> None:
        self.pass_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

    # ------------------------------------------------------------------
    # Helpers / lifecycle
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self.ssid_edit.setEnabled(enabled)
        self.pass_edit.setEnabled(enabled)
        self.show_pass_check.setEnabled(enabled)

    def done(self, result: int) -> None:
        # Both buttons and the window's X funnel through done() — deregister the
        # message tap here so it covers every close path.
        self._gateway.remove_message_callback(self._on_gateway_message)
        super().done(result)

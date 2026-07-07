"""Tools => Update Nodes (OTA)… — flash node firmware wirelessly.

Lists every flashable node declared across the configured robots, lets the user
pick which to update, and pushes the bundled firmware to each through the
gateway. Two transports are offered: ESP-NOW (slow but works anywhere, see
:class:`~src.hardware.node_ota_updater.NodeOTAUpdater`) and WiFi (fast, via the
gateway SoftAP, see :class:`~src.hardware.wifi_ota_updater.WifiOTAUpdater`). The
gateway stays cabled to the PC; only the nodes are updated over the air.

The gateway object is owned by ``MainWindow`` and is expected to be already
connected (auto-connect on startup, or via the Robots tab / Settings). On open
the dialog kicks a non-blocking scan so the online/offline column is fresh.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
)
from PySide6.QtCore import Qt

from src.config.settings import Settings
from src.gui.setup_wizard import firmware_for_node_type, firmware_for_c6
from src.gui.base_dialog import BaseDialog
from src.gui.ui_ota_update_dialog import Ui_OTAUpdateDialog
from src.hardware.gateway import Gateway
from src.hardware.node_ota_updater import NodeOTAUpdater
from src.hardware.wifi_ota_updater import WifiOTAUpdater, C6WifiOTAUpdater

logger = logging.getLogger(__name__)

# transport_combo indices.
_TRANSPORT_ESPNOW, _TRANSPORT_WIFI = range(2)

_DIALOG_TITLE = "Update Nodes"

# The gateway's C6 half (Thymio RCP) — WiFi-OTA only, reached by target "thymio"
# (not a MAC), so it gets a synthetic row key + a fixed "wired" state.
_C6_KEY = "__c6_rcp__"
_C6_LABEL = "C6 (Thymio RCP)"
_C6_TYPE = "thymio_rcp"

# Columns
_COL_SEL, _COL_MAC, _COL_TYPE, _COL_LED, _COL_ONLINE, _COL_PROGRESS, _COL_STATUS = range(7)


class _OTAWorker(QThread):
    """Flashes a list of nodes sequentially, one transfer at a time.

    The transport is picked once for the whole batch: ESP-NOW (slow, works
    anywhere) or WiFi (fast, needs the gateway SoftAP + this PC on that network).
    """

    progress = Signal(str, int)   # mac, percent
    status = Signal(str, str)     # mac, message
    done = Signal()

    def __init__(self, gateway: Gateway, jobs: list[tuple[str, Path]],
                 transport: int = _TRANSPORT_ESPNOW):
        super().__init__()
        self._gateway = gateway
        self._jobs = jobs
        self._transport = transport
        self._current: NodeOTAUpdater | WifiOTAUpdater | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._current is not None:
            self._current.cancel()

    def _make_updater(self, mac: str, path: Path) -> NodeOTAUpdater | WifiOTAUpdater:
        # WiFi updaters carry no AP credentials: the gateway injects its own
        # stored ssid/pass (Tools → Gateway WiFi AP…) while forwarding ota_wifi.
        on_progress = lambda p, m=mac: self.progress.emit(m, p)
        on_log = lambda s, m=mac: self.status.emit(m, s)
        if mac == _C6_KEY:
            # The C6 is addressed by target "thymio", not a MAC, and only over WiFi.
            return C6WifiOTAUpdater(
                self._gateway, path,
                on_progress=on_progress, on_log=on_log,
            )
        if self._transport == _TRANSPORT_WIFI:
            return WifiOTAUpdater(
                self._gateway, mac, path,
                on_progress=on_progress, on_log=on_log,
            )
        return NodeOTAUpdater(
            self._gateway, mac, path, on_progress=on_progress, on_log=on_log,
        )

    def run(self) -> None:
        for mac, path in self._jobs:
            if self._cancelled:
                self.status.emit(mac, "Cancelled")
                continue
            self.status.emit(mac, "Starting…")
            self._current = self._make_updater(mac, path)
            ok, msg = self._current.run()
            self.status.emit(mac, ("✓ " if ok else "✗ ") + msg)
        self._current = None
        self.done.emit()


class OTAUpdateDialog(BaseDialog, Ui_OTAUpdateDialog):
    """Multi-select node firmware updater (ESP-NOW or WiFi transport)."""

    # Marshals a gateway message from the serial read thread to the GUI thread.
    _message_received = Signal(dict)

    def __init__(self, gateway: Gateway, settings: Settings,
                 session_active: bool = False, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._gateway = gateway
        self._settings = settings
        self._session_active = session_active
        self._worker: _OTAWorker | None = None
        self._row_by_mac: dict[str, int] = {}

        # Table tweaks not expressible in the .ui.
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            _COL_STATUS, QHeaderView.ResizeMode.Stretch)

        self.select_all_btn.clicked.connect(self._on_select_online)
        self.flash_btn.clicked.connect(self._on_flash)
        self.close_btn.clicked.connect(self.reject)
        self.transport_combo.currentIndexChanged.connect(self._on_transport_changed)
        self._on_transport_changed(self.transport_combo.currentIndex())

        self.save_ap_btn.clicked.connect(self._on_save_ap)

        self._populate()
        self._update_banner()
        # Non-blocking refresh of the online column.
        self._message_received.connect(self._handle_gateway_message)
        self._gateway.on_message(self._on_gateway_message)
        if self._gateway.is_connected:
            self._gateway.scan()
            QTimer.singleShot(2000, self._refresh_online)
            # The gateway owns the AP config (NVS): mirror its real name here,
            # and Save AP writes edits back. The stored password never leaves
            # the gateway — during an update it hands it to the node itself.
            self._gateway.send_gateway("get_ap")

    # ------------------------------------------------------------------
    # Gateway AP (the gateway stores the credentials; this edits them)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict[str, Any]) -> None:
        """Serial-read-thread callback — hand off to the GUI thread."""
        self._message_received.emit(data)

    def _handle_gateway_message(self, data: dict[str, Any]) -> None:
        mtype = data.get("type")
        if mtype == "ap_config" and data.get("ssid"):
            self.ap_ssid_edit.setText(data["ssid"])
        elif mtype == "ap_set":
            self.save_ap_btn.setEnabled(True)
            if data.get("ok"):
                self.ap_pass_edit.clear()
                self._flash_ap_status(
                    f"Access point saved — network is now “{data.get('ssid', '')}”.")
            else:
                QMessageBox.warning(self, _DIALOG_TITLE,
                                    "The gateway rejected the AP change: "
                                    f"{data.get('reason', 'unknown')}")

    def _on_save_ap(self) -> None:
        if not self._gateway.is_connected:
            QMessageBox.information(self, _DIALOG_TITLE,
                                    "Connect the gateway first.")
            return
        ssid = self.ap_ssid_edit.text().strip()
        password = self.ap_pass_edit.text()
        if not ssid:
            QMessageBox.information(self, _DIALOG_TITLE,
                                    "The network name can't be empty.")
            return
        if password and len(password) < 8:
            QMessageBox.information(
                self, _DIALOG_TITLE,
                "A new password needs at least 8 characters (or leave it blank "
                "to keep the current one).")
            return
        self.save_ap_btn.setEnabled(False)
        kwargs = {"ssid": ssid}
        if password:                       # omitted = keep the stored password
            kwargs["pass"] = password
        self._gateway.send_gateway("set_ap", **kwargs)

    def _flash_ap_status(self, text: str) -> None:
        """Show transient AP feedback in the hint label, then restore it."""
        original = self.wifi_hint_label.text()
        self.wifi_hint_label.setText(text)
        QTimer.singleShot(5000, lambda: self.wifi_hint_label.setText(original))

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _nodes_from_settings(self) -> list[tuple[str, str, str]]:
        """Return (robot_label, mac, node_type) for every flashable node."""
        out: list[tuple[str, str, str]] = []
        robots = self._settings.data.get("robots", {})
        for group in robots.values():
            if not isinstance(group, list):
                continue
            for robot in group:
                rid = robot.get("id") or robot.get("thymio_id") or "robot"
                for node in robot.get("nodes", []):
                    mac = node.get("mac")
                    ntype = node.get("node_type", "")
                    if mac and firmware_for_node_type(ntype) is not None:
                        out.append((rid, mac, ntype))
        return out

    def _populate(self) -> None:
        nodes = self._nodes_from_settings()
        self.table.setRowCount(len(nodes) + 1)   # + the gateway's C6 (Thymio RCP)
        for row, (_rid, mac, ntype) in enumerate(nodes):
            self._init_row(row, mac, ntype)
        # Always offer the C6 half of the gateway (WiFi-OTA only), last.
        self._init_row(len(nodes), _C6_KEY, _C6_TYPE, mac_text=_C6_LABEL)
        self.table.resizeColumnsToContents()
        self._refresh_online()

    def _init_row(self, row: int, key: str, ntype: str, mac_text: str | None = None) -> None:
        self._row_by_mac[key] = row
        sel = QTableWidgetItem()
        sel.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        sel.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(row, _COL_SEL, sel)
        self.table.setItem(row, _COL_MAC, QTableWidgetItem(mac_text or key))
        self.table.setItem(row, _COL_TYPE, QTableWidgetItem(ntype))
        self.table.setItem(row, _COL_LED, QTableWidgetItem("?"))
        self.table.setItem(row, _COL_ONLINE, QTableWidgetItem("?"))

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        self.table.setCellWidget(row, _COL_PROGRESS, bar)
        self.table.setItem(row, _COL_STATUS, QTableWidgetItem(""))

    def _refresh_online(self) -> None:
        # online = answered the most recent scan (see Gateway.online_macs); a
        # node powered off since then reads "offline" rather than staying stuck
        # "online" the way the ever-growing known_macs set would show it.
        online = self._gateway.online_macs if self._gateway.is_connected else frozenset()
        for mac, row in self._row_by_mac.items():
            self._refresh_row(mac, row, online)

    def _refresh_row(self, mac: str, row: int, online) -> None:
        item = self.table.item(row, _COL_ONLINE)
        led = self.table.item(row, _COL_LED)
        if mac == _C6_KEY:
            # Reached over the wired UART to the S3, not ESP-NOW — no MAC to scan.
            if item is not None:
                item.setText("wired")
            if led is not None:
                led.setText("—")
            return
        if item is not None:
            item.setText("online" if mac in online else "offline")
        if led is not None:
            led.setText(self._led_text(mac, self.table.item(row, _COL_TYPE).text()))

    def _led_text(self, mac: str, ntype: str) -> str:
        """LED-ring variant to show for a node: 'rgb'/'rgbw' if reported, '?' if
        not yet known (offline / pre-reporting firmware), '—' for node types
        with no distinct RGBW build."""
        if (firmware_for_node_type(ntype, rgbw=True)
                == firmware_for_node_type(ntype, rgbw=False)):
            return "—"
        rgbw = self._gateway.node_rgbw(mac) if self._gateway.is_connected else None
        if rgbw is None:
            return "?"
        return "rgbw" if rgbw else "rgb"

    def _update_banner(self) -> None:
        if not self._gateway.is_connected:
            self.banner_label.setText(
                "⚠ Gateway not connected. Connect it in Settings or the "
                "Robots tab, then reopen this dialog."
            )
            self.flash_btn.setEnabled(False)
        elif self._session_active:
            self.banner_label.setText(
                "⚠ A session is running. Stop it before updating firmware."
            )
            self.flash_btn.setEnabled(False)
        else:
            self.banner_label.setText("")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_transport_changed(self, index: int) -> None:
        """Show the AP editor + hint only for the WiFi transport."""
        wifi = index == _TRANSPORT_WIFI
        for w in (self.ap_ssid_label, self.ap_ssid_edit,
                  self.ap_pass_label, self.ap_pass_edit,
                  self.save_ap_btn, self.wifi_hint_label):
            w.setVisible(wifi)

    def _on_select_online(self) -> None:
        online = self._gateway.online_macs if self._gateway.is_connected else frozenset()
        for mac, row in self._row_by_mac.items():
            item = self.table.item(row, _COL_SEL)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Checked if mac in online else Qt.CheckState.Unchecked
                )

    def _selected_jobs(self) -> list[tuple[str, Path]]:
        debug = self.debug_check.isChecked()
        # RGBW applies only to node types with an RGBW build (node_direct/
        # multiplexed); firmware_for_node_type falls back to the plain bin for
        # the others. Each node self-reports its variant in its ready/pong frame,
        # so we flash the matching bin automatically. The checkbox is only a
        # fallback for nodes that haven't reported it (offline, or firmware from
        # before the field existed) — e.g. the first OTA that installs it.
        fallback_rgbw = self.rgbw_check.isChecked()
        jobs: list[tuple[str, Path]] = []
        for mac, row in self._row_by_mac.items():
            if self.table.item(row, _COL_SEL).checkState() != Qt.CheckState.Checked:
                continue
            if mac == _C6_KEY:
                fw = firmware_for_c6()
                if not fw.exists():
                    self.table.item(row, _COL_STATUS).setText(
                        "✗ firmware not found — build rcp_c6 first "
                        "(pio run -d firmware/thymio_rcp -e rcp_c6)")
                    continue
                jobs.append((mac, fw))
                continue
            ntype = self.table.item(row, _COL_TYPE).text()
            reported = self._gateway.node_rgbw(mac)
            rgbw = fallback_rgbw if reported is None else reported
            fw = firmware_for_node_type(ntype, debug, rgbw)
            if fw is None or not fw.exists():
                self.table.item(row, _COL_STATUS).setText(
                    f"✗ firmware not found: {fw}"
                )
                continue
            jobs.append((mac, fw))
        return jobs

    def _on_flash(self) -> None:
        if not self._gateway.is_connected or self._session_active:
            return
        jobs = self._selected_jobs()
        if not jobs:
            QMessageBox.information(self, _DIALOG_TITLE, "No nodes selected.")
            return

        transport = self.transport_combo.currentIndex()
        if any(mac == _C6_KEY for mac, _ in jobs) and transport != _TRANSPORT_WIFI:
            QMessageBox.information(
                self, _DIALOG_TITLE,
                "The C6 (Thymio RCP) updates over WiFi only. Pick the WiFi "
                "transport.")
            return

        via = ("fast via the gateway WiFi access point (the PC stays on the "
               "cable)" if transport == _TRANSPORT_WIFI else "over ESP-NOW")
        if QMessageBox.question(
            self, _DIALOG_TITLE,
            f"Flash {len(jobs)} node(s) {via}? Do not power them off "
            "during the update.",
        ) != QMessageBox.StandardButton.Yes:
            return

        self._set_running(True)
        self._worker = _OTAWorker(self._gateway, jobs, transport)
        self._worker.progress.connect(self._on_progress)
        self._worker.status.connect(self._on_status)
        self._worker.done.connect(self._on_done)
        # Release the QThread only once it has *actually* stopped. ``done`` is
        # emitted from inside run() while the thread is still winding down, so
        # dropping the reference there would delete the QThread with
        # isRunning() still true → "QThread: Destroyed while thread is still
        # running" → hard abort. ``finished`` fires after run() returns and the
        # thread has stopped, so releasing it then is safe.
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        self.flash_btn.setEnabled(not running)
        self.select_all_btn.setEnabled(not running)
        self.debug_check.setEnabled(not running)
        self.rgbw_check.setEnabled(not running)
        self.transport_combo.setEnabled(not running)
        self.ap_ssid_edit.setEnabled(not running)
        self.ap_pass_edit.setEnabled(not running)
        self.save_ap_btn.setEnabled(not running)
        self.close_btn.setText("Cancel" if running else "Close")

    def _on_progress(self, mac: str, pct: int) -> None:
        row = self._row_by_mac.get(mac)
        if row is not None:
            bar = self.table.cellWidget(row, _COL_PROGRESS)
            if isinstance(bar, QProgressBar):
                bar.setValue(pct)

    def _on_status(self, mac: str, msg: str) -> None:
        row = self._row_by_mac.get(mac)
        if row is not None:
            self.table.item(row, _COL_STATUS).setText(msg)

    def _on_done(self) -> None:
        # Emitted from inside the worker's run(): the thread is still running
        # here, so only refresh the UI. The worker is released in
        # _on_worker_finished, wired to the QThread's finished signal.
        self._set_running(False)

    def _on_worker_finished(self) -> None:
        """Release the worker once its thread has truly stopped.

        ``finished`` is delivered after run() returns, so isRunning() is already
        false and dropping the last reference here cannot trip Qt's "destroyed
        while running" guard; wait() only joins the already-exiting OS thread.
        """
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.wait()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def reject(self) -> None:
        # Never tear the dialog down while the worker thread is still running:
        # once exec() returns, the dialog wrapper (and its _worker attribute)
        # can be garbage-collected, and deleting a running QThread aborts the
        # process. The updaters honour cancel within ~0.2 s, so this wait
        # returns promptly rather than freezing the GUI.
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait()
        super().reject()

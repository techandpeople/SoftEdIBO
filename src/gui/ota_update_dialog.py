"""Tools => Update Nodes (OTA)… — flash node firmware wirelessly over ESP-NOW.

Lists every flashable node declared across the configured robots, lets the user
pick which to update, and streams the bundled firmware to each through the
gateway (see :class:`~src.hardware.node_ota_updater.NodeOTAUpdater`). The gateway
stays cabled to the PC; only the nodes are updated over the air.

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
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from PySide6.QtCore import Qt

from src.config.settings import Settings
from src.gui.setup_wizard import firmware_for_node_type
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.node_ota_updater import NodeOTAUpdater

logger = logging.getLogger(__name__)

# Columns
_COL_SEL, _COL_MAC, _COL_TYPE, _COL_ONLINE, _COL_PROGRESS, _COL_STATUS = range(6)


class _OTAWorker(QThread):
    """Flashes a list of nodes sequentially, one ESP-NOW stream at a time."""

    progress = Signal(str, int)   # mac, percent
    status = Signal(str, str)     # mac, message
    done = Signal()

    def __init__(self, gateway: ESPNowGateway, jobs: list[tuple[str, Path]]):
        super().__init__()
        self._gateway = gateway
        self._jobs = jobs
        self._current: NodeOTAUpdater | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._current is not None:
            self._current.cancel()

    def run(self) -> None:
        for mac, path in self._jobs:
            if self._cancelled:
                self.status.emit(mac, "Cancelled")
                continue
            self.status.emit(mac, "Starting…")
            updater = NodeOTAUpdater(
                self._gateway, mac, path,
                on_progress=lambda p, m=mac: self.progress.emit(m, p),
                on_log=lambda s, m=mac: self.status.emit(m, s),
            )
            self._current = updater
            ok, msg = updater.run()
            self.status.emit(mac, ("✓ " if ok else "✗ ") + msg)
        self._current = None
        self.done.emit()


class OTAUpdateDialog(QDialog):
    """Multi-select node firmware updater over ESP-NOW."""

    def __init__(self, gateway: ESPNowGateway, settings: Settings,
                 session_active: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Nodes (OTA)")
        self.setMinimumSize(680, 420)
        self._gateway = gateway
        self._settings = settings
        self._session_active = session_active
        self._worker: _OTAWorker | None = None
        self._row_by_mac: dict[str, int] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Flash node firmware wirelessly through the gateway. The gateway must "
            "stay connected by USB; only the nodes are updated over the air."
        ))

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        layout.addWidget(self._banner)

        self._debug_check = QCheckBox("Debug build (verbose Serial output)")
        layout.addWidget(self._debug_check)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["", "MAC", "Type", "Online", "Progress", "Status"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        self._select_all = QPushButton("Select online")
        self._select_all.clicked.connect(self._on_select_online)
        self._flash_btn = QPushButton("Flash selected")
        self._flash_btn.clicked.connect(self._on_flash)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        btns.addWidget(self._select_all)
        btns.addStretch(1)
        btns.addWidget(self._flash_btn)
        btns.addWidget(self._close_btn)
        layout.addLayout(btns)

        self._populate()
        self._update_banner()
        # Non-blocking refresh of the online column.
        if self._gateway.is_connected:
            self._gateway.scan()
            QTimer.singleShot(2000, self._refresh_online)

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
        self._table.setRowCount(len(nodes))
        for row, (_rid, mac, ntype) in enumerate(nodes):
            self._row_by_mac[mac] = row

            sel = QTableWidgetItem()
            sel.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            sel.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(row, _COL_SEL, sel)
            self._table.setItem(row, _COL_MAC, QTableWidgetItem(mac))
            self._table.setItem(row, _COL_TYPE, QTableWidgetItem(ntype))
            self._table.setItem(row, _COL_ONLINE, QTableWidgetItem("?"))

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            self._table.setCellWidget(row, _COL_PROGRESS, bar)
            self._table.setItem(row, _COL_STATUS, QTableWidgetItem(""))
        self._table.resizeColumnsToContents()
        self._refresh_online()

    def _refresh_online(self) -> None:
        known = self._gateway.known_macs if self._gateway.is_connected else frozenset()
        for mac, row in self._row_by_mac.items():
            item = self._table.item(row, _COL_ONLINE)
            if item is not None:
                item.setText("online" if mac in known else "offline")

    def _update_banner(self) -> None:
        if not self._gateway.is_connected:
            self._banner.setText(
                "⚠ Gateway not connected. Connect it in Settings or the "
                "Robots tab, then reopen this dialog."
            )
            self._flash_btn.setEnabled(False)
        elif self._session_active:
            self._banner.setText(
                "⚠ A session is running. Stop it before updating firmware."
            )
            self._flash_btn.setEnabled(False)
        else:
            self._banner.setText("")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_select_online(self) -> None:
        known = self._gateway.known_macs if self._gateway.is_connected else frozenset()
        for mac, row in self._row_by_mac.items():
            item = self._table.item(row, _COL_SEL)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Checked if mac in known else Qt.CheckState.Unchecked
                )

    def _selected_jobs(self) -> list[tuple[str, Path]]:
        debug = self._debug_check.isChecked()
        jobs: list[tuple[str, Path]] = []
        for mac, row in self._row_by_mac.items():
            if self._table.item(row, _COL_SEL).checkState() != Qt.CheckState.Checked:
                continue
            ntype = self._table.item(row, _COL_TYPE).text()
            fw = firmware_for_node_type(ntype, debug)
            if fw is None or not fw.exists():
                self._table.item(row, _COL_STATUS).setText(
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
            QMessageBox.information(self, "Update Nodes", "No nodes selected.")
            return
        if QMessageBox.question(
            self, "Update Nodes",
            f"Flash {len(jobs)} node(s) over the air? Do not power them off "
            "during the update.",
        ) != QMessageBox.StandardButton.Yes:
            return

        self._set_running(True)
        self._worker = _OTAWorker(self._gateway, jobs)
        self._worker.progress.connect(self._on_progress)
        self._worker.status.connect(self._on_status)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        self._flash_btn.setEnabled(not running)
        self._select_all.setEnabled(not running)
        self._debug_check.setEnabled(not running)
        self._close_btn.setText("Cancel" if running else "Close")

    def _on_progress(self, mac: str, pct: int) -> None:
        row = self._row_by_mac.get(mac)
        if row is not None:
            bar = self._table.cellWidget(row, _COL_PROGRESS)
            if isinstance(bar, QProgressBar):
                bar.setValue(pct)

    def _on_status(self, mac: str, msg: str) -> None:
        row = self._row_by_mac.get(mac)
        if row is not None:
            self._table.item(row, _COL_STATUS).setText(msg)

    def _on_done(self) -> None:
        self._set_running(False)
        self._worker = None

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().reject()

"""Guided, dongle-free Thymio address discovery (Thymio config -> Discover...).

Runs a long sniff through the gateway's C6 while telling the user to power the
Thymios on: a Wireless Thymio announces itself at boot, so switching each robot
on during the scan is all it takes - no RF dongle. Addresses appear live, in
first-seen order, so powering robots on one at a time maps address -> robot.

The sniff runs off-thread (:func:`~src.gui.async_task.run_async`);
``discover_thymios`` reports each new address from the gateway's serial reader
thread, bridged onto the GUI thread through a queued signal. Closing the dialog
sets the scan's stop event so the C6 goes back to plain-RCP mode right away.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QWidget

from src.gui.async_task import run_async
from src.gui.ui_thymio_discover_dialog import Ui_ThymioDiscoverDialog
from src.robots.thymio.thymio_discovery import discover_thymios


class ThymioDiscoverDialog(QDialog, Ui_ThymioDiscoverDialog):
    """Live-updating scan dialog; :meth:`address` is the user's pick."""

    #: One scan window - long enough to walk over and power-cycle a robot.
    SCAN_SECS = 20.0

    # Serial-reader thread -> GUI thread bridge for live address updates.
    _found = Signal(str)

    def __init__(self, gateway: Any, channel: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._gateway = gateway
        self._channel = int(channel)
        self._stop: threading.Event | None = None
        self._deadline = 0.0

        self._found.connect(self._add_address)
        self.rescan_btn.clicked.connect(self._start_scan)
        self.addr_list.itemSelectionChanged.connect(self._update_ok)
        self.addr_list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._update_status)

        self._start_scan()

    def address(self) -> str:
        """The selected address (hex, e.g. ``"6a25"``), or ``""``."""
        item = self.addr_list.currentItem()
        return item.text() if item is not None else ""

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        self.addr_list.clear()
        self._update_ok()
        self.rescan_btn.setEnabled(False)
        self._stop = threading.Event()
        self._deadline = time.monotonic() + self.SCAN_SECS
        self._tick.start()
        self._update_status()
        run_async(
            lambda stop=self._stop: discover_thymios(
                self._gateway, channel=self._channel, secs=self.SCAN_SECS,
                on_found=self._found.emit, stop=stop),
            on_done=self._scan_done, on_error=self._scan_failed, parent=self,
        )

    def _scan_done(self, addrs: list) -> None:
        self._tick.stop()
        self.rescan_btn.setEnabled(True)
        n = self.addr_list.count()
        self.status_label.setText(
            f"Scan finished - {n} Thymio(s) seen."
            if n else
            "Scan finished - nothing seen. Power a Thymio on DURING the scan "
            "and try again.")

    def _scan_failed(self, exc: Exception) -> None:
        self._tick.stop()
        self.rescan_btn.setEnabled(True)
        self.status_label.setText(f"Scan failed: {exc}")

    def done(self, result: int) -> None:
        if self._stop is not None:
            self._stop.set()          # ends the sniff; C6 back to plain RCP
        super().done(result)

    # ------------------------------------------------------------------
    # GUI updates
    # ------------------------------------------------------------------

    def _add_address(self, addr: str) -> None:
        for i in range(self.addr_list.count()):
            if self.addr_list.item(i).text() == addr:
                return
        self.addr_list.addItem(addr)
        if self.addr_list.currentItem() is None:
            self.addr_list.setCurrentRow(0)
        self._update_ok()
        self._update_status()

    def _update_ok(self) -> None:
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.addr_list.currentItem() is not None)

    def _update_status(self) -> None:
        left = max(0, int(self._deadline - time.monotonic()))
        n = self.addr_list.count()
        self.status_label.setText(
            f"Scanning channel {self._channel}... {left} s left - "
            f"{n} Thymio(s) seen. Power them on now.")

"""Tools => Emergency Flash... - cable-flash a node whose own USB is dead.

When a node's USB-serial path stops working, OTA (over ESP-NOW) is the normal
recovery route - but that only works while the node still runs OTA-capable
firmware. If it is bricked (bad flash, wrong partition table), you must write a
known-good image over the wires. This dialog does that through a *second* ESP32
held in reset as a transparent USB-to-serial bridge to the target's UART0.

The bridge can't drive the target's EN/IO0 lines, so:
  * esptool runs with ``no_reset=True`` (no auto reset/boot toggling) - the user
    puts the target into download mode by hand (hold BOOT, tap EN, release);
  * the default baud is conservative (hand-wired bridges are noisy).

Firmware images and the esptool invocation are shared with the setup wizard
(``NODE_FIRMWARES`` / ``_esptool_cmd``), so dev uses the local ``firmware/`` bins
and a frozen nightly/release uses the CI-built bundle - both via
``Settings.BUNDLE``. Flashing itself reuses the wizard's QProcess approach.
"""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtWidgets import QDialogButtonBox

from src.gui.async_task import run_async
from src.gui.setup_wizard import NODE_FIRMWARES, _esptool_cmd
from src.gui.base_dialog import BaseDialog
from src.gui.ui_emergency_flash_dialog import Ui_EmergencyFlashDialog
from src.hardware.serial_ports import list_esp32_ports

logger = logging.getLogger(__name__)

# Conservative first; hand-wired bridges drop bytes at high speed.
_BAUD_RATES = ["115200", "230400", "460800", "921600"]


class EmergencyFlashDialog(BaseDialog, Ui_EmergencyFlashDialog):
    """Cable-flash a node over a USB-serial bridge, with no esptool auto-reset."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._proc: QProcess | None = None

        for label in NODE_FIRMWARES:
            self.type_combo.addItem(label)
        self.baud_combo.addItems(_BAUD_RATES)  # default = first = 115200

        self.log.setMaximumBlockCount(1000)
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.flash_btn.clicked.connect(self._start_flash)
        self.button_box.button(
            QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)

        self._refresh_ports()

    # ------------------------------------------------------------------
    # Ports
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        run_async(
            lambda: [p.device for p in list_esp32_ports()],
            on_done=lambda ports, cur=current: self._populate_ports(ports, cur),
            parent=self,
        )

    def _populate_ports(self, ports: list[str], current: str) -> None:
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if current in ports:
            self.port_combo.setCurrentText(current)
        else:
            # Classic USB-UART bridges enumerate as /dev/ttyUSB*.
            usb = [p for p in ports if "USB" in p]
            if usb:
                self.port_combo.setCurrentText(usb[0])

    # ------------------------------------------------------------------
    # Flashing
    # ------------------------------------------------------------------

    def _start_flash(self) -> None:
        port = self.port_combo.currentText()
        if not port:
            self.log.appendPlainText("No serial port selected.")
            return

        variant = "debug" if self.debug_check.isChecked() else "release"
        firmware = NODE_FIRMWARES[self.type_combo.currentText()][variant]
        if not firmware.exists():
            self.log.appendPlainText(
                f"Firmware binary not found:\n  {firmware}\n\n"
                "Build it first (scripts/build-firmware.sh) or use a release build."
            )
            return

        self.flash_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        self.log.appendPlainText(
            "Put the target in download mode now: hold BOOT, tap EN/RST, "
            'release BOOT - keep doing it while it says "Connecting...".\n'
        )
        self.log.appendPlainText(f"Flashing {firmware.name} to {port}...\n")

        prog, args = _esptool_cmd(
            port, firmware, baud=self.baud_combo.currentText(), no_reset=True,
        )
        self._proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._proc.setProcessEnvironment(env)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.readyReadStandardError.connect(self._on_error_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(prog, args)

    def _parse_progress(self, raw: str) -> None:
        self.log.appendPlainText(raw.rstrip())
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", raw):
            self.progress.setValue(int(float(m.group(1))))

    def _on_output(self) -> None:
        if self._proc is not None:
            self._parse_progress(bytes(
                self._proc.readAllStandardOutput().data()).decode(errors="replace"))

    def _on_error_output(self) -> None:
        if self._proc is not None:
            self._parse_progress(bytes(
                self._proc.readAllStandardError().data()).decode(errors="replace"))

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if exit_code == 0:
            self.progress.setValue(100)
            self.log.appendPlainText(
                "\nFlash completed. Remove the IO0/BOOT jumper and reset the "
                "target - it now runs OTA-capable firmware.")
        else:
            self.log.appendPlainText(
                f"\nFlash failed (exit code {exit_code}). Check the wiring "
                "(TX->TX, RX->RX, common GND), redo the download-mode buttons, "
                "or lower the baud rate.")
        self.flash_btn.setEnabled(True)
        self._proc = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def done(self, result: int) -> None:
        # Don't leave a flash running after the dialog closes.
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()
            self._proc.waitForFinished(1000)
        super().done(result)

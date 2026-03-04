"""First-run setup wizard for flashing gateway and node firmware."""

import re
import sys
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from src.config.settings import Settings
from src.hardware.serial_ports import list_esp32_ports

SENTINEL_PATH: Path = Settings.ROOT / "data" / ".setup_done"
# Read-only bundled assets live in BUNDLE (_internal/ when frozen, ROOT in dev)
GATEWAY_BIN: Path = Settings.BUNDLE / "firmware" / "gateway" / "firmware.bin"
NODE_BIN: Path = Settings.BUNDLE / "firmware" / "air_chamber_node" / "firmware.bin"


def _esptool_cmd(port: str, firmware: Path) -> tuple[str, list[str]]:
    """Return (program, args) to invoke esptool, handling frozen mode.

    In a PyInstaller bundle, the standalone ``esptool`` binary sits next to
    the main executable.  In development, esptool is called as a module via
    the current Python interpreter.
    """
    flash_args = ["--chip", "esp32", "--port", port, "--baud", "921600",
                  "write_flash", "0x0", str(firmware)]
    if getattr(sys, "frozen", False):
        suffix = ".exe" if sys.platform == "win32" else ""
        esptool_bin = Path(sys.executable).parent / f"esptool{suffix}"
        return str(esptool_bin), flash_args
    return sys.executable, ["-m", "esptool"] + flash_args


def needs_setup() -> bool:
    """Return True if the setup wizard has not been completed yet."""
    return not SENTINEL_PATH.exists()


def _list_ports() -> list[str]:
    """Return serial port names for ESP32 devices (all COM* on Windows)."""
    return [p.device for p in list_esp32_ports()]


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to SoftEdIBO Setup")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p>This wizard will flash the firmware to:</p>"
            "<ul>"
            "<li>The <b>ESP-NOW gateway</b></li>"
            "<li>Each <b>air-chamber node</b></li>"
            "</ul>"
            "<p>Connect each device via USB before the corresponding step.</p>"
            "<p>You can re-run this wizard at any time from <b>Tools → Flash Firmware…</b></p>"
        ))
        layout.addStretch()


class _FlashPage(QWizardPage):
    """Base page for flashing a single firmware binary via esptool."""

    def __init__(self, title: str, subtitle: str, firmware_path: Path):
        super().__init__()
        self.setTitle(title)
        self.setSubTitle(subtitle)
        self._firmware = firmware_path
        self._proc: QProcess | None = None
        self._done = False
        self._skipped = False

        layout = QVBoxLayout(self)

        # Port row
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Serial port:"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(180)
        port_row.addWidget(self._port_combo)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(refresh_btn)
        port_row.addStretch()
        layout.addLayout(port_row)

        # Flash / Skip buttons (side by side)
        btn_row = QHBoxLayout()
        self._flash_btn = QPushButton("Flash Firmware")
        self._flash_btn.clicked.connect(self._start_flash)
        btn_row.addWidget(self._flash_btn)
        self._skip_btn = QPushButton("Skip this step")
        self._skip_btn.clicked.connect(self._toggle_skip)
        btn_row.addWidget(self._skip_btn)
        layout.addLayout(btn_row)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # Log output
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(1000)
        layout.addWidget(self._log)

        self._refresh_ports()

    # ------------------------------------------------------------------

    def initializePage(self) -> None:
        self._refresh_ports()

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentText()
        self._port_combo.clear()
        ports = _list_ports()
        for p in ports:
            self._port_combo.addItem(p)
        if current in ports:
            self._port_combo.setCurrentText(current)

    def _toggle_skip(self) -> None:
        self._skipped = not self._skipped
        self._skip_btn.setText("Don't skip" if self._skipped else "Skip this step")
        self._flash_btn.setEnabled(not self._skipped)
        self.completeChanged.emit()

    def _start_flash(self) -> None:
        port = self._port_combo.currentText()
        if not port:
            self._log.appendPlainText("No serial port selected.")
            return

        if not self._firmware.exists():
            self._log.appendPlainText(
                f"Firmware binary not found:\n  {self._firmware}\n\n"
                "Place the compiled firmware.bin file there and try again."
            )
            return

        self._flash_btn.setEnabled(False)
        self._skipped = False
        self._skip_btn.setText("Skip this step")
        self._progress.setValue(0)
        self._log.clear()
        self._done = False
        self.completeChanged.emit()

        self._log.appendPlainText(f"Flashing {self._firmware.name} to {port}…\n")

        prog, args = _esptool_cmd(port, self._firmware)
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(prog, args)

    def _on_output(self) -> None:
        raw = self._proc.readAllStandardOutput().data().decode(errors="replace")
        self._log.appendPlainText(raw.rstrip())
        # Parse percentage from esptool output: "Writing at 0x00000000... (42 %)"
        for m in re.finditer(r'\((\d+)\s*%\)', raw):
            self._progress.setValue(int(m.group(1)))

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if exit_code == 0:
            self._progress.setValue(100)
            self._log.appendPlainText("\nFlash completed successfully.")
            self._done = True
        else:
            self._log.appendPlainText(f"\nFlash failed (exit code {exit_code}).")
            self._flash_btn.setEnabled(True)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._done or self._skipped


class FlashGatewayPage(_FlashPage):
    def __init__(self):
        super().__init__(
            "Flash Gateway Firmware",
            "Connect the ESP-NOW gateway via USB, select the port, then click Flash.",
            GATEWAY_BIN,
        )


class FlashNodePage(_FlashPage):
    """Flash page for air-chamber nodes; allows flashing multiple units."""

    def __init__(self):
        super().__init__(
            "Flash Node Firmware",
            "Connect an air-chamber node via USB, select the port, then click Flash.",
            NODE_BIN,
        )

        # "Flash another node" button — enabled after each successful flash
        self._another_btn = QPushButton("Flash Another Node")
        self._another_btn.clicked.connect(self._reset_for_another)
        self._another_btn.setEnabled(False)

        layout = self.layout()
        # Insert before the log (last widget)
        layout.insertWidget(layout.count() - 1, self._another_btn)

    def _on_finished(self, exit_code: int, exit_status) -> None:
        super()._on_finished(exit_code, exit_status)
        if exit_code == 0:
            self._another_btn.setEnabled(True)

    def _reset_for_another(self) -> None:
        """Prepare for flashing the next node; keep _done=True so Next stays enabled."""
        self._log.clear()
        self._progress.setValue(0)
        self._flash_btn.setEnabled(True)
        self._another_btn.setEnabled(False)
        # _done remains True — user has already flashed at least one node

    # isComplete inherited from _FlashPage: _done or _skipped


class DonePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Setup Complete")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p>Setup is complete.</p>"
            "<p>Click <b>Finish</b> to open the application.</p>"
            "<p>You can re-flash firmware at any time from "
            "<b>Tools → Flash Firmware…</b></p>"
        ))
        layout.addStretch()

    def initializePage(self) -> None:
        """Create the sentinel file the first time this page is shown."""
        SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_PATH.touch()


# ------------------------------------------------------------------
# Wizard
# ------------------------------------------------------------------

class SetupWizard(QWizard):
    """First-run wizard: flashes gateway and node firmware via esptool."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SoftEdIBO — First-Run Setup")
        self.setMinimumSize(660, 520)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self.addPage(WelcomePage())
        self.addPage(FlashGatewayPage())
        self.addPage(FlashNodePage())
        self.addPage(DonePage())

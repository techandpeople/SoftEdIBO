"""First-run setup wizard for flashing gateway and node firmware."""

import re
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWizard,
    QWizardPage,
)

from src.config.settings import Settings
from src.gui.async_task import run_async
from src.gui.ui_wizard_done_page import Ui_DonePage
from src.gui.ui_wizard_flash_page import Ui_FlashPage
from src.gui.ui_wizard_welcome_page import Ui_WelcomePage
from src.hardware.serial_ports import list_esp32_ports

SENTINEL_PATH: Path = Settings.ROOT / "data" / ".setup_done"

# Wizard page ids — used for nextId() branching off the welcome page's choice.
PAGE_WELCOME = 0
PAGE_GATEWAY = 1
PAGE_NODE = 2
PAGE_DONE = 3
# Read-only bundled assets live in BUNDLE (_internal/ when frozen, repo root in dev)
#
# Gateway firmware — two board variants; each needs the matching esptool --chip.
# Both merged images flash at 0x0.
GATEWAY_FIRMWARES: dict[str, dict[str, Any]] = {
    "Seeed XIAO ESP32-C6  (new, USB-C)": {
        "path": Settings.BUNDLE / "firmware" / "gateway" / "firmware.bin",
        "chip": "esp32c6",
    },
    "ESP32-WROOM-32  (classic DevKit)": {
        "path": Settings.BUNDLE / "firmware" / "gateway" / "firmware-esp32.bin",
        "chip": "esp32",
    },
}

# Available node firmware binaries, keyed by the canonical ``node_type`` used in
# config (settings.yaml). Each entry holds release + debug variants; the wizard
# exposes a checkbox to pick the debug build (verbose Serial, see firmware/dbg.h).
# This is the single source of truth shared by the setup wizard and the OTA
# updater dialog.
NODE_TYPE_FIRMWARES: dict[str, dict[str, Path]] = {
    "node_direct": {
        "release": Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-direct-release.bin",
        "debug":   Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-direct-debug.bin",
    },
    "node_multiplexed": {
        "release": Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-release.bin",
        "debug":   Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-debug.bin",
    },
    # node_magnet_sensor ships a single build (no separate debug variant), so both
    # keys point at the same bin — the debug checkbox is a no-op for it.
    "node_magnet_sensor": {
        "release": Settings.BUNDLE / "firmware" / "node_magnet_sensor" / "firmware-release.bin",
        "debug":   Settings.BUNDLE / "firmware" / "node_magnet_sensor" / "firmware-release.bin",
    },
}

# Human-readable labels for the wizard's node-type picker.
_NODE_TYPE_LABELS: dict[str, str] = {
    "node_direct":         "node_direct  (3 chambers, GPIO valves, onboard pumps, LED ring)",
    "node_multiplexed":    "node_multiplexed  (up to 12 chambers, optional pressure/vacuum tanks)",
    "node_magnet_sensor":  "node_magnet_sensor  (4x MLX90393 magnetic touch board)",
}

# Display-label -> {release, debug} view used by the wizard's FlashNodePage.
NODE_FIRMWARES: dict[str, dict[str, Path]] = {
    _NODE_TYPE_LABELS[nt]: fw for nt, fw in NODE_TYPE_FIRMWARES.items()
}


def firmware_for_node_type(node_type: str, debug: bool = False) -> Path | None:
    """Return the bundled firmware bin for a ``node_type``, or None if unknown.

    ``node_magnet_sensor`` has no separate debug build, so ``debug`` is a no-op
    for it. Used by both the setup wizard and the OTA updater dialog.
    """
    entry = NODE_TYPE_FIRMWARES.get(node_type)
    if entry is None:
        return None
    return entry["debug" if debug else "release"]


def _esptool_cmd(port: str, firmware: Path, chip: str = "esp32") -> tuple[str, list[str]]:
    """Return (program, args) to invoke esptool, handling frozen mode.

    In a PyInstaller bundle, the standalone ``esptool`` binary sits next to
    the main executable.  In development, esptool is called as a module via
    the current Python interpreter. ``chip`` is "esp32" for the WROOM gateway
    and all nodes, "esp32c6" for the XIAO C6 gateway.
    """
    flash_args = ["--chip", chip, "--port", port, "--baud", "921600",
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

class WelcomePage(QWizardPage, Ui_WelcomePage):
    """Entry page: pick whether to flash the gateway, node(s), or both."""

    def __init__(self):
        super().__init__()
        self.setupUi(self)

    def flash_choice(self) -> str:
        """Return the selected path: "gateway", "node", or "both"."""
        if self.rb_gateway.isChecked():
            return "gateway"
        if self.rb_node.isChecked():
            return "node"
        return "both"

    def nextId(self) -> int:
        # Node-only jumps straight past the gateway page; the others start there.
        return PAGE_NODE if self.flash_choice() == "node" else PAGE_GATEWAY


class _FlashPage(QWizardPage, Ui_FlashPage):
    """Base page for flashing a single firmware binary via esptool."""

    # Subclasses can set this to a substring that should be preferred when
    # auto-selecting the port (e.g. "ACM" for gateway, "USB" for nodes).
    _preferred_port_hint: str = ""

    def __init__(self, title: str, subtitle: str, firmware_path: Path,
                 chip: str = "esp32"):
        super().__init__()
        self.setupUi(self)
        self.setTitle(title)
        self.setSubTitle(subtitle)
        self._firmware = firmware_path
        self._chip = chip
        self._proc: QProcess | None = None
        self._done = False

        # The static frame (port row, flash button, progress, log) lives in the
        # .ui; subclasses add their selectors into ``extra_layout`` and
        # ``extra_bottom_layout``.
        self.log.setMaximumBlockCount(1000)
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.flash_btn.clicked.connect(self._start_flash)

        self._refresh_ports()

    # ------------------------------------------------------------------

    def initializePage(self) -> None:
        self._refresh_ports()

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        # Port enumeration can stall briefly; keep the wizard responsive.
        run_async(
            _list_ports,
            on_done=lambda ports, cur=current: self._populate_ports(ports, cur),
            parent=self,
        )

    def _populate_ports(self, ports: list[str], current: str) -> None:
        self.port_combo.clear()
        for p in ports:
            self.port_combo.addItem(p)
        # Restore previous selection if still present.
        if current in ports:
            self.port_combo.setCurrentText(current)
        elif self._preferred_port_hint:
            # Auto-select the first port whose name matches the hint (e.g. "ACM"
            # for the gateway's USB-JTAG device, "USB" for classic node UART).
            preferred = [p for p in ports if self._preferred_port_hint in p]
            if preferred:
                self.port_combo.setCurrentText(preferred[0])

    def _start_flash(self) -> None:
        port = self.port_combo.currentText()
        if not port:
            self.log.appendPlainText("No serial port selected.")
            return

        if not self._firmware.exists():
            self.log.appendPlainText(
                f"Firmware binary not found:\n  {self._firmware}\n\n"
                f"Place the compiled {self._firmware.name} file there and try again."
            )
            return

        self.flash_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        self._done = False
        self.completeChanged.emit()

        self.log.appendPlainText(f"Flashing {self._firmware.name} to {port}…\n")

        prog, args = _esptool_cmd(port, self._firmware, self._chip)
        self._proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._proc.setProcessEnvironment(env)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.readyReadStandardError.connect(self._on_error_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(prog, args)

    def _parse_progress(self, raw: str) -> None:
        """Parse and display esptool output, updating the progress bar."""
        self.log.appendPlainText(raw.rstrip())
        # Parse percentage from esptool output (both old and new formats)
        # Old: "Writing at 0x00000000... (42 %)"
        # New: "Writing at 0x00000000 [====] 42.0% 212992/516100 bytes..."
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*%', raw):
            self.progress.setValue(int(float(m.group(1))))

    def _on_output(self) -> None:
        if self._proc is None:
            return
        raw = self._proc.readAllStandardOutput().data().decode(errors="replace")
        self._parse_progress(raw)

    def _on_error_output(self) -> None:
        if self._proc is None:
            return
        raw = self._proc.readAllStandardError().data().decode(errors="replace")
        self._parse_progress(raw)

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if exit_code == 0:
            self.progress.setValue(100)
            self.log.appendPlainText("\nFlash completed successfully.")
            self._done = True
        else:
            self.log.appendPlainText(f"\nFlash failed (exit code {exit_code}).")
            self.flash_btn.setEnabled(True)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._done


class FlashGatewayPage(_FlashPage):
    """Flash page for the gateway; lets the user pick the board variant."""

    # XIAO C6 gateway appears as /dev/ttyACM* (USB-JTAG, not USB-UART)
    _preferred_port_hint = "ACM"

    def __init__(self):
        first_label = next(iter(GATEWAY_FIRMWARES))
        first = GATEWAY_FIRMWARES[first_label]
        super().__init__(
            "Flash Gateway Firmware",
            "Connect the gateway board (appears as /dev/ttyACM0), then click Flash.",
            first["path"],
            chip=first["chip"],
        )

        # Gateway board selector — into the .ui's top extra_layout.
        board_row = QHBoxLayout()
        board_row.addWidget(QLabel("Gateway board:"))
        self._board_combo = QComboBox()
        self._board_combo.setMinimumWidth(320)
        for label in GATEWAY_FIRMWARES:
            self._board_combo.addItem(label)
        self._board_combo.currentTextChanged.connect(self._on_board_changed)
        board_row.addWidget(self._board_combo)
        board_row.addStretch()
        self.extra_layout.addLayout(board_row)

    def _on_board_changed(self, label: str) -> None:
        variant = GATEWAY_FIRMWARES[label]
        self._firmware = variant["path"]
        self._chip = variant["chip"]

    def nextId(self) -> int:
        # Continue to the node page only when the user chose to flash both.
        welcome = self.wizard().page(PAGE_WELCOME)
        if isinstance(welcome, WelcomePage) and welcome.flash_choice() == "both":
            return PAGE_NODE
        return PAGE_DONE


class FlashNodePage(_FlashPage):
    """Flash page for nodes; lets the user pick node type and flash multiple units."""

    # Nodes use a classic USB-UART bridge — appears as /dev/ttyUSB*
    _preferred_port_hint = "USB"

    def __init__(self):
        first_label = next(iter(NODE_FIRMWARES))
        super().__init__(
            "Flash Node Firmware",
            "Connect the node board (appears as /dev/ttyUSB0), then click Flash.",
            NODE_FIRMWARES[first_label]["release"],
        )

        # Node type selector — into the .ui's top extra_layout.
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Node type:"))
        self._type_combo = QComboBox()
        self._type_combo.setMinimumWidth(320)
        for label in NODE_FIRMWARES:
            self._type_combo.addItem(label)
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        type_row.addWidget(self._type_combo)
        type_row.addStretch()
        self.extra_layout.addLayout(type_row)

        # Debug-build checkbox — switches to the firmware-debug.bin variant.
        self._debug_check = QCheckBox("Debug build (verbose Serial output)")
        self._debug_check.toggled.connect(self._update_firmware_path)
        self.extra_layout.addWidget(self._debug_check)

        # "Flash another node" button — enabled after each successful flash,
        # into the .ui's extra_bottom_layout (just above the log).
        self._another_btn = QPushButton("Flash Another Node")
        self._another_btn.clicked.connect(self._reset_for_another)
        self._another_btn.setEnabled(False)
        self.extra_bottom_layout.addWidget(self._another_btn)

    def _on_type_changed(self, _label: str) -> None:
        self._update_firmware_path()

    def _update_firmware_path(self) -> None:
        variant = "debug" if self._debug_check.isChecked() else "release"
        self._firmware = NODE_FIRMWARES[self._type_combo.currentText()][variant]

    def _on_finished(self, exit_code: int, exit_status) -> None:
        super()._on_finished(exit_code, exit_status)
        if exit_code == 0:
            self._another_btn.setEnabled(True)

    def _reset_for_another(self) -> None:
        """Prepare for flashing the next node; keep _done=True so Next stays enabled."""
        self.log.clear()
        self.progress.setValue(0)
        self.flash_btn.setEnabled(True)
        self._another_btn.setEnabled(False)
        # _done remains True — user has already flashed at least one node

    def nextId(self) -> int:
        return PAGE_DONE

    # isComplete inherited from _FlashPage: returns _done


class DonePage(QWizardPage, Ui_DonePage):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

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

        # Explicit ids so the welcome page can branch via nextId().
        self.setPage(PAGE_WELCOME, WelcomePage())
        self.setPage(PAGE_GATEWAY, FlashGatewayPage())
        self.setPage(PAGE_NODE, FlashNodePage())
        self.setPage(PAGE_DONE, DonePage())

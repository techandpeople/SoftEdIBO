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

# Wizard page ids - used for nextId() branching off the welcome page's choice.
PAGE_WELCOME = 0
PAGE_GATEWAY = 1
PAGE_NODE = 2
PAGE_DONE = 3
# Read-only bundled assets live in BUNDLE (_internal/ when frozen, repo root in dev)
#
# Gateway firmware - the gateway is a Seeed XIAO ESP32-S3 (main) optionally wired to a
# companion XIAO ESP32-C6 (Thymio RCP). Each is its own chip and its own USB device, so
# the flash page offers a board dropdown that flashes either one on its own (or both, one
# after the other). Both are merged images flashed at 0x0.
#   - S3: ESP-NOW chamber control + SoftAP/WiFi-OTA + the C6 UART link (-DGATEWAY_AP
#     -DGATEWAY_THYMIO), firmware-s3.bin.
#   - C6: 802.15.4 radio co-processor for wireless Thymio (docs/THYMIO_WIRELESS_CONTROL.md),
#     firmware-c6.bin (the merged bundle; firmware.bin is the app-only WiFi-OTA image).
GATEWAY_FIRMWARES: dict[str, dict[str, Any]] = {
    "Gateway S3  (main board, USB-C)": {
        "chip":     "esp32s3",
        "firmware": Settings.BUNDLE / "firmware" / "gateway" / "firmware-s3.bin",
    },
    "Companion C6  (Thymio RCP, optional)": {
        "chip":     "esp32c6",
        "firmware": Settings.BUNDLE / "firmware" / "thymio_rcp" / "firmware-c6.bin",
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
        # RGBW LED-ring variants (SK6812, e.g. Adafruit 2862). Same direct board;
        # only the NeoPixel pixel type differs (-DLED_RGBW). The flash wizard and
        # OTA dialog expose an "RGBW LED ring" toggle that selects these; node
        # types without these keys fall back to the plain release/debug bins.
        "release_rgbw": Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-direct-rgbw-release.bin",
        "debug_rgbw":   Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-direct-rgbw-debug.bin",
    },
    "node_multiplexed": {
        "release": Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-release.bin",
        "debug":   Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-debug.bin",
        # RGBW LED-ring variants (SK6812). The multiplexed board drives four rings
        # (one 24-LED + three 16-LED) as separate strips on GPIO23/17/16/4; only the
        # NeoPixel pixel type differs (-DLED_RGBW). Selected by the same "RGBW LED
        # ring" toggle as node_direct.
        "release_rgbw": Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-rgbw-release.bin",
        "debug_rgbw":   Settings.BUNDLE / "firmware" / "node_actuator" / "firmware-multiplexed-rgbw-debug.bin",
    },
    # node_magnet_sensor ships a single build (no separate debug variant), so both
    # keys point at the same bin - the debug checkbox is a no-op for it.
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


def firmware_for_node_type(node_type: str, debug: bool = False,
                           rgbw: bool = False) -> Path | None:
    """Return the bundled firmware bin for a ``node_type``, or None if unknown.

    ``node_magnet_sensor`` has no separate debug build, so ``debug`` is a no-op
    for it. ``rgbw`` selects the RGBW LED-ring variant (``node_direct`` and
    ``node_multiplexed``); node types without an RGBW build ignore it and fall
    back to the plain release/debug bin. Used by both the setup wizard and the
    OTA dialog.
    """
    entry = NODE_TYPE_FIRMWARES.get(node_type)
    if entry is None:
        return None
    base = "debug" if debug else "release"
    if rgbw:
        return entry.get(f"{base}_rgbw", entry[base])
    return entry[base]


def firmware_for_c6() -> Path:
    """Path to the Thymio RCP (the gateway's C6 half) app image for WiFi-OTA.

    A bare app image (not a merged bundle) - the C6 is WiFi-OTA'd into an app
    partition. `scripts/build-firmware.sh` builds it (the `rcp_c6` env) and copies it
    here; `scripts/ota_c6_wifi.py` serves the same file. The OTA dialog reports
    "build it first" if it's missing.
    """
    return Settings.BUNDLE / "firmware" / "thymio_rcp" / "firmware.bin"


def _esptool_cmd(port: str, firmware: Path, chip: str = "esp32", *,
                 baud: str = "921600", no_reset: bool = False) -> tuple[str, list[str]]:
    """Return (program, args) to invoke esptool, handling frozen mode.

    In a PyInstaller bundle, the standalone ``esptool`` binary sits next to
    the main executable.  In development, esptool is called as a module via
    the current Python interpreter. ``chip`` is "esp32" for the WROOM gateway
    and all nodes, "esp32c6" for the XIAO C6 gateway.

    ``no_reset`` drops esptool's automatic reset/boot toggling (``--before`` /
    ``--after no_reset``). Needed by the emergency cable-flash, where the target
    is reached through a second ESP32 used as a USB-serial bridge: the bridge's
    DTR/RTS lines can't reach the target's EN/IO0, so the user enters download
    mode by hand. ``baud`` can be lowered for that hand-wired bridge.
    """
    flash_args = ["--chip", chip, "--port", port, "--baud", baud]
    if no_reset:
        # Underscore spellings stay compatible with both the bundled esptool 4.x
        # and dev's 5.x (which keeps "no_reset" as a deprecated alias of
        # "no-reset"); the hyphenated form does not exist in 4.x.
        flash_args += ["--before", "no_reset", "--after", "no_reset"]
    flash_args += ["write_flash", "0x0", str(firmware)]
    if getattr(sys, "frozen", False):
        suffix = ".exe" if sys.platform == "win32" else ""
        esptool_bin = Path(sys.executable).parent / f"esptool{suffix}"
        return str(esptool_bin), flash_args
    return sys.executable, ["-m", "esptool"] + flash_args


def needs_setup() -> bool:
    """Return True if the setup wizard has not been completed yet."""
    return not SENTINEL_PATH.exists()


def mark_setup_done() -> None:
    """Create the sentinel so the first-run wizard is not shown again.

    Called both when the wizard finishes and when it is skipped/cancelled -
    the hardware may already be flashed, and the wizard stays reachable from
    the Tools menu for re-flashing later.
    """
    SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL_PATH.touch()


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

    # Subclasses that let the user flash several boards/units in one visit set this
    # to the button label (e.g. "Flash Another Node"). The base then owns the whole
    # affordance: the button, enabling it after a successful flash, resetting for the
    # next flash, and re-arming when the user simply picks a different target. Left
    # empty means no such button (single-shot flash page).
    _another_label: str = ""

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
        self._another_btn: QPushButton | None = None

        # The static frame (port row, flash button, progress, log) lives in the
        # .ui; subclasses add their selectors into ``extra_layout`` and
        # ``extra_bottom_layout``.
        self.log.setMaximumBlockCount(1000)
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.flash_btn.clicked.connect(self._start_flash)

        # Optional "flash another" button (into the .ui's extra_bottom_layout, just
        # above the log). Present only when a subclass opts in via _another_label.
        if self._another_label:
            self._another_btn = QPushButton(self._another_label)
            self._another_btn.clicked.connect(self._reset_for_another)
            self._another_btn.setEnabled(False)
            self.extra_bottom_layout.addWidget(self._another_btn)

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

        self.log.appendPlainText(f"Flashing {self._firmware.name} to {port}...\n")

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
        raw = bytes(self._proc.readAllStandardOutput().data()).decode(errors="replace")
        self._parse_progress(raw)

    def _on_error_output(self) -> None:
        if self._proc is None:
            return
        raw = bytes(self._proc.readAllStandardError().data()).decode(errors="replace")
        self._parse_progress(raw)

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if exit_code == 0:
            self.progress.setValue(100)
            self.log.appendPlainText("\nFlash completed successfully.")
            self._done = True
            # Offer to flash the next board/unit without leaving the page.
            if self._another_btn is not None:
                self._another_btn.setEnabled(True)
        else:
            self.log.appendPlainText(f"\nFlash failed (exit code {exit_code}).")
            self.flash_btn.setEnabled(True)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._done

    def _reset_for_another(self) -> None:
        """Re-arm the page to flash another board/unit; keep _done so Next stays
        enabled (the user has already flashed at least one)."""
        self.log.clear()
        self.progress.setValue(0)
        self.flash_btn.setEnabled(True)
        if self._another_btn is not None:
            self._another_btn.setEnabled(False)

    def _rearm_if_flashed(self) -> None:
        """Re-arm Flash when the user picks a different firmware/target after a
        completed flash - an explicit "flash this one next" gesture, so they need not
        click the "Flash Another..." button first. Subclasses call this from their
        selector-change handler. No-op mid-flash (``_done`` is False while a flash runs)
        or when idle (Flash still enabled), and on pages without the button."""
        if self._another_btn is not None and self._done and not self.flash_btn.isEnabled():
            self._reset_for_another()


class FlashGatewayPage(_FlashPage):
    """Flash page for the gateway; a dropdown picks which board (S3 or C6).

    The gateway is a XIAO ESP32-S3 (main) optionally wired to a companion XIAO
    ESP32-C6 that speaks 802.15.4 to Thymio robots (docs/THYMIO_WIRELESS_CONTROL.md).
    Each is its own chip and its own USB device, so the board dropdown lets you flash
    either one on its own - flash just the C6 without touching the S3, or vice versa -
    and "Flash Another Board" flashes the second one right after, without leaving the
    page (swap the cable, pick the other board). Mirrors FlashNodePage.
    """

    # Both the S3 and the C6 enumerate as /dev/ttyACM* (native USB-Serial/JTAG).
    _preferred_port_hint = "ACM"
    _another_label = "Flash Another Board"

    def __init__(self):
        first = next(iter(GATEWAY_FIRMWARES.values()))
        super().__init__(
            "Flash Gateway Firmware",
            "Pick the board, connect it (appears as /dev/ttyACM*), then click Flash. "
            "The S3 and the companion C6 are separate USB devices - flash either on "
            "its own, or both one after the other.",
            first["firmware"],
            chip=first["chip"],
        )

        # Board selector - S3 (main) or the optional companion C6 (Thymio RCP).
        board_row = QHBoxLayout()
        board_row.addWidget(QLabel("Board:"))
        self._board_combo = QComboBox()
        self._board_combo.setMinimumWidth(320)
        for label in GATEWAY_FIRMWARES:
            self._board_combo.addItem(label)
        self._board_combo.setWhatsThis(
            "Which gateway board to flash. 'Gateway S3' is the main board (USB-ESP-NOW "
            "bridge + WiFi + Thymio UART link). 'Companion C6' is the optional XIAO "
            "ESP32-C6 that drives Thymio robots over 802.15.4 - only on gateways with "
            "wireless-Thymio support. Each is a separate USB device, so you can flash "
            "one without the other (e.g. re-flash only the C6, leaving the S3 alone)."
        )
        self._board_combo.currentTextChanged.connect(self._on_board_changed)
        board_row.addWidget(self._board_combo)
        board_row.addStretch()
        self.extra_layout.addLayout(board_row)

    def _on_board_changed(self, label: str) -> None:
        entry = GATEWAY_FIRMWARES[label]
        self._firmware = entry["firmware"]
        self._chip = entry["chip"]
        # Picking a different board after a flash re-arms Flash for it (base handles it).
        self._rearm_if_flashed()

    def nextId(self) -> int:
        # Continue to the node page only when the user chose to flash both.
        welcome = self.wizard().page(PAGE_WELCOME)
        if isinstance(welcome, WelcomePage) and welcome.flash_choice() == "both":
            return PAGE_NODE
        return PAGE_DONE


class FlashNodePage(_FlashPage):
    """Flash page for nodes; lets the user pick node type and flash multiple units."""

    # Nodes use a classic USB-UART bridge - appears as /dev/ttyUSB*
    _preferred_port_hint = "USB"
    _another_label = "Flash Another Node"

    def __init__(self):
        first_label = next(iter(NODE_FIRMWARES))
        super().__init__(
            "Flash Node Firmware",
            "Connect the node board (appears as /dev/ttyUSB0), then click Flash.",
            NODE_FIRMWARES[first_label]["release"],
        )

        # Node type selector - into the .ui's top extra_layout.
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

        # Debug-build checkbox - switches to the firmware-debug.bin variant.
        self._debug_check = QCheckBox("Debug build (verbose Serial output)")
        self._debug_check.toggled.connect(self._update_firmware_path)
        self.extra_layout.addWidget(self._debug_check)

        # RGBW-ring checkbox - switches to the -DLED_RGBW build. Only enabled for
        # node types that ship an RGBW variant (node_direct); see _on_type_changed.
        self._rgbw_check = QCheckBox("RGBW LED ring (SK6812, e.g. Adafruit 2862)")
        self._rgbw_check.setWhatsThis(
            "Flash the RGBW build for boards wired with an RGBW NeoPixel ring "
            "(SK6812, e.g. Adafruit 2862, 4 colour channels). Leave unchecked for "
            "plain RGB rings (WS2812, e.g. Adafruit 1586). The two pixel types use "
            "a different data format, so the wrong build drives the ring with "
            "shifted colours. Only available for node types that have an RGBW ring."
        )
        self._rgbw_check.toggled.connect(self._update_firmware_path)
        self.extra_layout.addWidget(self._rgbw_check)
        self._on_type_changed(self._type_combo.currentText())

    def _on_type_changed(self, _label: str) -> None:
        # The RGBW toggle only makes sense for node types with an RGBW build.
        fw = NODE_FIRMWARES[self._type_combo.currentText()]
        has_rgbw = "release_rgbw" in fw
        self._rgbw_check.setEnabled(has_rgbw)
        if not has_rgbw:
            self._rgbw_check.setChecked(False)
        self._update_firmware_path()

    def _update_firmware_path(self) -> None:
        fw = NODE_FIRMWARES[self._type_combo.currentText()]
        base = "debug" if self._debug_check.isChecked() else "release"
        if self._rgbw_check.isChecked() and f"{base}_rgbw" in fw:
            base = f"{base}_rgbw"
        self._firmware = fw[base]
        # Changing type/debug/RGBW after a flash re-arms Flash for the new selection.
        self._rearm_if_flashed()

    def nextId(self) -> int:
        return PAGE_DONE

    # isComplete, _on_finished, _reset_for_another, and the "Flash Another Node"
    # button are all inherited from _FlashPage (opted in via _another_label).


class DonePage(QWizardPage, Ui_DonePage):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

    def initializePage(self) -> None:
        """Create the sentinel file the first time this page is shown."""
        mark_setup_done()


# ------------------------------------------------------------------
# Wizard
# ------------------------------------------------------------------

class SetupWizard(QWizard):
    """First-run wizard: flashes gateway and node firmware via esptool."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SoftEdIBO - First-Run Setup")
        self.setMinimumSize(660, 520)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        # Explicit ids so the welcome page can branch via nextId().
        self.setPage(PAGE_WELCOME, WelcomePage())
        self.setPage(PAGE_GATEWAY, FlashGatewayPage())
        self.setPage(PAGE_NODE, FlashNodePage())
        self.setPage(PAGE_DONE, DonePage())

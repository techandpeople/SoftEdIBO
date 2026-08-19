"""Main application window for SoftEdIBO."""

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QTextEdit,
)

from src._version import __build_time__, __version__
from src.config.settings import Settings
from src.updater import AppUpdater
from src.data.database import Database
from src.gui.async_task import run_async
from src.gui.data_panel import DataPanel
from src.gui.emergency_stop_button import EmergencyStopButton
from src.gui.help_mode import HelpButton
from src.gui.home_panel import HomePanel
from src.gui.participant_panel import ParticipantPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel
from src.gui.settings_dialog import SettingsDialog
from src.gui.ui_main_window import Ui_MainWindow
from src.hardware.gateway import Gateway
from src.hardware.fill_calibration import resolve_fill_profiles
from src.robots.base_robot import BaseRobot
from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.tree.tree_robot import TreeRobot
from src.robots.turtle.turtle_robot import TurtleRobot

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow, Ui_MainWindow):
    """Main application window with tabbed panels."""

    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self._settings = Settings()

        self._db = Database.from_settings(self._settings.db_cfg, Settings.ROOT)
        self._db.connect()

        # Shared SoftEdIBO gateway (not connected yet; user clicks Connect)
        self._gateway = Gateway(
            port=self._settings.gateway_port,
            baud_rate=self._settings.gateway_baud,
        )

        self._home_panel = HomePanel()
        self._participant_panel = ParticipantPanel(self._db)
        self._session_panel = SessionPanel(self._db, gateway=self._gateway)
        self._robot_panel = RobotPanel(self._gateway, self._settings, self._db)
        self._data_panel = DataPanel(self._db)

        self.tabs.addTab(self._home_panel, "Home")
        self.tabs.addTab(self._participant_panel, "Participants")
        self.tabs.addTab(self._session_panel, "Session")
        self.tabs.addTab(self._robot_panel, "Robots")
        self.tabs.addTab(self._data_panel, "Data")

        self._home_panel.navigate_to.connect(self._on_navigate)
        self._session_panel.session_started.connect(self._home_panel.set_session_status)
        self._session_panel.session_started.connect(lambda _: self._on_navigate("Session"))
        self._session_panel.session_stopped.connect(lambda: self._home_panel.set_session_status(None))
        self._session_panel.session_finished.connect(self._data_panel.refresh)
        # Rebuild robots when the session panel calibrates fill times mid-flow.
        self._session_panel.reload_requested.connect(self._on_robot_configured)
        self._robot_panel.gateway_changed.connect(self._home_panel.set_gateway_status)
        self._robot_panel.robot_configured.connect(self._on_robot_configured)

        self._robots = self._load_robots_safe()
        self._session_panel.set_available_robots(self._robots)
        self._robot_panel.refresh(self._robots)

        # Menu bar actions (structure defined in main_window.ui)
        self.actionSettings.triggered.connect(self._open_settings)
        self.actionFlashFirmware.triggered.connect(self._open_flash_wizard)
        self.actionCheckForUpdates.triggered.connect(self._check_updates_manual)
        self.actionAbout.triggered.connect(self._show_about)

        # Tools => Activity Editor... - added programmatically so we don't have
        # to regenerate the .ui every time we ship a new managed entity. The
        # menuTools handle comes from ui_main_window.py.
        from PySide6.QtGui import QAction
        self.actionActivityEditor = QAction("Activity Editor...", self)
        self.actionActivityEditor.triggered.connect(self._open_activity_editor)
        self.menuTools.addAction(self.actionActivityEditor)

        self.actionUpdateNodesOTA = QAction("Update Nodes (OTA)...", self)
        self.actionUpdateNodesOTA.triggered.connect(self._open_ota_dialog)
        self.menuTools.addAction(self.actionUpdateNodesOTA)

        self.actionTrainTouch = QAction("Touch Gestures...", self)
        self.actionTrainTouch.triggered.connect(self._open_train_touch)
        self.menuTools.addAction(self.actionTrainTouch)

        self.actionPositionBench = QAction("Touch Position Bench...", self)
        self.actionPositionBench.triggered.connect(self._open_position_bench)
        self.menuTools.addAction(self.actionPositionBench)

        self.actionCalibrateFill = QAction("Calibrate Fill Times...", self)
        self.actionCalibrateFill.triggered.connect(self._open_fill_calibration)
        self.menuTools.addAction(self.actionCalibrateFill)

        self.actionCalibrateTouch = QAction("Calibrate Touch Coupling...", self)
        self.actionCalibrateTouch.triggered.connect(self._open_touch_calibration)
        self.menuTools.addAction(self.actionCalibrateTouch)

        self.actionEmergencyFlash = QAction("Emergency Flash (dead USB)...", self)
        self.actionEmergencyFlash.triggered.connect(self._open_emergency_flash)
        self.menuTools.addAction(self.actionEmergencyFlash)

        # "?" help-mode toggle in the menu-bar corner - hover any field to see
        # what it does. Reusable across windows (see src/gui/help_mode.py).
        self._help_button = HelpButton(self)
        self.menubar.setCornerWidget(self._help_button, Qt.Corner.TopRightCorner)

        # Always-visible emergency stop in the opposite corner. Kills every pump
        # and valve at once. Pressing the "0" key anywhere does the same, and
        # "1" re-arms while stopped (see the application event filter below).
        self._estop_button = EmergencyStopButton(self)
        self._rearm_confirm_open = False
        self._estop_button.stop_requested.connect(self._emergency_stop)
        self._estop_button.rearm_requested.connect(self._rearm)
        self.menubar.setCornerWidget(self._estop_button, Qt.Corner.TopLeftCorner)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # Track whether a session is live so OTA can refuse mid-actuation.
        self._session_active = False
        self._session_panel.session_started.connect(
            lambda *_: setattr(self, "_session_active", True))
        self._session_panel.session_stopped.connect(
            lambda *_: setattr(self, "_session_active", False))

        # Auto-connect the gateway on startup (configurable) so the user doesn't
        # reconnect every launch. Opening the serial port blocks while the driver
        # enumerates, so do it off the GUI thread to avoid freezing the window on
        # launch.
        if self._settings.gateway_auto_connect and not self._gateway.is_connected:
            run_async(
                self._gateway.connect,
                on_done=self._on_auto_connect,
                parent=self,
            )

        # OTA updater - silent background check 5 s after startup
        self._updater = AppUpdater(self)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.error.connect(
            lambda msg: self.statusBar().showMessage(f"Update error: {msg}", 6000)
        )
        self.setWindowTitle(f"SoftEdIBO  {__version__}")
        self.statusBar().show()  # keep bar always visible to prevent layout shifts
        QTimer.singleShot(5000, self._updater.check)

    def _on_auto_connect(self, ok: bool) -> None:
        """GUI-thread handler for the async startup auto-connect."""
        if ok:
            self._home_panel.set_gateway_status(True)
            self._robot_panel.sync_gateway_ui()
            self._robot_panel.auto_scan_if_enabled()

    # ------------------------------------------------------------------
    # Robot loading
    # ------------------------------------------------------------------

    def _load_robots(self) -> list[BaseRobot]:
        """Instantiate all robots declared in settings.yaml."""
        robots: list[BaseRobot] = []
        robot_data = self._settings.data.get("robots", {})

        # Turtle / Tree robots - same node hardware, each its own robot kind.
        for yaml_key, robot_cls in (("turtles", TurtleRobot), ("trees", TreeRobot)):
            for cfg in robot_data.get(yaml_key, []):
                if cfg.get("skins"):
                    robots.append(robot_cls(
                        robot_id=cfg.get("id", yaml_key[:-1]),
                        gateway=self._gateway,
                        node_configs=cfg.get("nodes", []),
                        # Fill in each chamber's effective fill curve (own override,
                        # else its skin-type template) before the robot builds its skins.
                        skin_configs=resolve_fill_profiles(self._settings.data, cfg["skins"]),
                    ))

        # Thymios - one RF dongle relays to several at once (each a node id), so all
        # the wireless ones share a single ThymioDongle owned by the window.
        thymios = robot_data.get("thymios", [])
        self._rebuild_thymio_dongle(thymios)

        gateway_idx = 0        # slot each C6-driven Thymio takes on the one C6
        for thymio_cfg in thymios:
            # Opt-in wheeled-base link: only build a (connecting) link when the config asks
            # for it, so existing configs and the sim path are untouched.
            link = None
            if thymio_cfg.get("wireless"):
                link = self._build_thymio_link(thymio_cfg, gateway_idx)
                if thymio_cfg.get("wireless_via") == "gateway":
                    gateway_idx += 1
            robots.append(ThymioRobot(
                robot_id=thymio_cfg["thymio_id"],
                gateway=self._gateway,
                node_configs=thymio_cfg.get("nodes", []),
                skin_configs=resolve_fill_profiles(
                    self._settings.data, thymio_cfg.get("skins", [])),
                link=link,
            ))

        return robots

    def _rebuild_thymio_dongle(self, thymios: list[dict]) -> None:
        """(Re)create the single shared RF dongle for the dongle-driven Thymios.

        Two wireless transports exist: the RF dongle (thymiodirect, shared across
        robots by node id) or the gateway's C6 (802.15.4, dongle-free);
        ``wireless_via: "gateway"`` picks the C6, anything else the dongle. Only
        ONE shared dongle is supported - differing ``dongle_port`` values are
        logged and the first (sorted) wins."""
        dongle_users = [t for t in thymios if t.get("wireless")
                        and t.get("wireless_via", "dongle") != "gateway"]
        old_dongle, self._thymio_dongle = getattr(self, "_thymio_dongle", None), None
        if old_dongle is not None:
            old_dongle.close()                     # config reload: drop the previous one
        if not dongle_users:
            return
        from src.robots.thymio.thymio_dongle import ThymioDongle
        # dongle_port omitted -> auto-detect; first robot that names one wins.
        ports = {p for t in dongle_users if (p := t.get("dongle_port"))}
        port = next(iter(sorted(ports)), None)
        if len(ports) > 1:
            logger.warning(
                "Thymio dongle: %d different dongle_port values configured %s - "
                "only one shared dongle is supported, using %s; robots paired "
                "to another dongle will not connect", len(ports), sorted(ports), port)
        self._thymio_dongle = ThymioDongle(serial_port=port)

    def _build_thymio_link(self, thymio_cfg: dict, gateway_idx: int):
        """The wheeled-base link a wireless Thymio config asks for: the gateway's
        C6 (``wireless_via: "gateway"``, one slot per robot) or the shared RF
        dongle (this robot picked out by ``node_id``; blank/0 -> first)."""
        if thymio_cfg.get("wireless_via") == "gateway":
            from src.robots.thymio.thymio_gateway_link import (
                DEFAULT_IMPACT_THRESHOLD, ThymioGatewayLink)
            return ThymioGatewayLink(
                gateway=self._gateway,
                channel=int(thymio_cfg.get("channel", 25)),
                index=gateway_idx,
                address=thymio_cfg.get("thymio_addr") or None,
                impact_threshold=float(thymio_cfg.get("impact_threshold")
                                       or DEFAULT_IMPACT_THRESHOLD),
            )
        from src.robots.thymio.thymio_link import ThymioLink
        return ThymioLink(dongle=self._thymio_dongle,
                          node_id=thymio_cfg.get("node_id") or None)

    def _load_robots_safe(self) -> list[BaseRobot]:
        """Wrapper around ``_load_robots`` that catches config errors.

        On failure a dialog offers to reset the config to the bundled default
        (dropping robot definitions but preserving nothing else) or to continue
        with no robots loaded.
        """
        import traceback
        try:
            return self._load_robots()
        except Exception:
            msg = traceback.format_exc()
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Configuration error")
            dlg.setIcon(QMessageBox.Icon.Critical)
            dlg.setText(
                "Failed to load robots from settings.yaml.\n\n"
                "This usually means the config file is from an older version "
                "or contains invalid data."
            )
            dlg.setDetailedText(msg)
            reset_btn = dlg.addButton("Reset config", QMessageBox.ButtonRole.ResetRole)
            dlg.addButton("Continue without robots", QMessageBox.ButtonRole.AcceptRole)
            dlg.exec()
            if dlg.clickedButton() is reset_btn:
                self._settings.reset_to_default()
                try:
                    return self._load_robots()
                except Exception:
                    pass
            return []

    def _open_flash_wizard(self) -> None:
        from src.gui.setup_wizard import SetupWizard
        wizard = SetupWizard(parent=self)
        wizard.exec()

    def _open_ota_dialog(self) -> None:
        """Tools => Update Nodes (OTA)... - flash node firmware over ESP-NOW."""
        from src.gui.ota_update_dialog import OTAUpdateDialog
        dlg = OTAUpdateDialog(
            self._gateway, self._settings,
            session_active=self._session_active, parent=self,
        )
        dlg.exec()

    def _open_train_touch(self) -> None:
        """Tools => Train Touch Models... - train per-skin-type gesture models
        from recorded sessions + their label CSVs."""
        from src.gui.train_touch_dialog import TrainTouchDialog
        # Skins of all configured robots - the guided capture binds to the one
        # owning the streaming node (compensated stream during inflation).
        skins = [s for r in self._robots
                 for s in (getattr(r, "skins", None) or {}).values()]
        TrainTouchDialog(parent=self, gateway=self._gateway,
                         skins=skins).exec()

    def _open_position_bench(self) -> None:
        """Tools => Touch Position Bench... - measure how finely a skin can
        locate a touch, beyond quadrants (docs/TOUCH_POSITION_ML_PLAN.md)."""
        from src.gui.position_bench_dialog import PositionBenchDialog
        PositionBenchDialog(self._gateway, parent=self).exec()

    def _open_fill_calibration(self) -> None:
        """Tools => Calibrate Fill Times... - measure each chamber's inflate time
        against the pressure sensor and store it as ``fill_time_ms``."""
        if self._session_active:
            QMessageBox.warning(
                self, "Calibrate Fill Times",
                "Stop the running session before calibrating - calibration "
                "drives the pumps directly.")
            return
        from src.gui.fill_calibration_dialog import FillCalibrationDialog
        dlg = FillCalibrationDialog(self._settings, self._gateway, parent=self)
        dlg.saved.connect(self._on_robot_configured)   # rebuild robots with new fill times
        dlg.exec()

    def _open_touch_calibration(self) -> None:
        """Tools => Calibrate Touch Coupling... - measure how each chamber shifts
        the magnet sensors and store the per-skin compensation matrix."""
        if self._session_active:
            QMessageBox.warning(
                self, "Calibrate Touch Coupling",
                "Stop the running session before calibrating - calibration "
                "drives the pumps directly.")
            return
        from src.gui.touch_calibration_dialog import TouchCalibrationDialog
        dlg = TouchCalibrationDialog(self._settings, self._gateway, parent=self)
        dlg.saved.connect(self._on_robot_configured)   # rebuild robots to apply the matrix
        dlg.exec()

    def _open_emergency_flash(self) -> None:
        """Tools => Emergency Flash... - cable-flash a node whose USB is dead,
        through a second ESP32 used as a USB-serial bridge (recovers a node that
        is too bricked for OTA over ESP-NOW)."""
        from src.gui.emergency_flash_dialog import EmergencyFlashDialog
        EmergencyFlashDialog(parent=self).exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, parent=self)
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.exec()

    def _open_activity_editor(self) -> None:
        """Tools => Activity Editor... - author block-based behaviours in the
        Visual Editor. Imported lazily so QtWebEngine is only loaded when the
        editor is actually opened. The connected robots are passed in so the
        editor can preview picked LED colours on them live.

        The app-wide event filter (the "0" panic key) is removed for the
        editor's lifetime: PySide6 crashes marshalling QtWebEngine's internal
        QtQuick objects through a Python global event filter (hover events over
        the web view). The editor drives no chambers and clears its LED preview
        on close, so losing the panic key while it is open is harmless - the
        modal already blocks the rest of the UI anyway."""
        from src.gui.activity_editor_dialog import ActivityEditorDialog
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        try:
            ActivityEditorDialog(self._db, parent=self,
                                 robots=self._robots).exec()
        finally:
            if app is not None:
                app.installEventFilter(self)

    def _on_settings_saved(self) -> None:
        """Apply settings changes that don't require a restart."""
        self._on_robot_configured()

    def _on_navigate(self, tab_name: str) -> None:
        """Switch to the tab matching tab_name."""
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == tab_name:
                self.tabs.setCurrentIndex(i)
                return

    def _on_robot_configured(self) -> None:
        """Reload settings and recreate robots after a config dialog saves."""
        self._settings.load()
        self._robots = self._load_robots_safe()
        self._session_panel.set_available_robots(self._robots)
        self._robot_panel.refresh(self._robots)

    # ------------------------------------------------------------------
    # OTA updates
    # ------------------------------------------------------------------

    def _on_update_available(self, version: str, url: str) -> None:
        """Show a non-intrusive notification in the status bar."""
        if getattr(self, "_update_notified", False):
            return
        self._update_notified = True
        self._pending_update_url = url
        lbl = QLabel(f"Update available: <b>{version}</b>")
        btn = QPushButton(f"Install {version}")
        btn.clicked.connect(lambda: self._start_update(version, url))
        self.statusBar().addPermanentWidget(lbl)
        self.statusBar().addPermanentWidget(btn)

    def _check_updates_manual(self) -> None:
        """Triggered from Tools => Check for Updates..."""
        self.statusBar().showMessage("Checking for updates...", 4000)
        self._updater.check()

    def _start_update(self, version: str, url: str) -> None:
        dlg = QProgressDialog(
            f"Downloading SoftEdIBO {version}...", "Cancel", 0, 100, self
        )
        dlg.setWindowTitle("Updating SoftEdIBO")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        dlg.canceled.connect(self._updater.cancel)

        def _on_progress(recv: int, total: int) -> None:
            if total > 0:
                dlg.setValue(int(recv / total * 100))

        def _on_done(path: Path) -> None:
            dlg.close()
            answer = QMessageBox.question(
                self,
                "Update ready",
                f"SoftEdIBO {version} was downloaded.\n\nRestart now to apply it?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                if sys.platform == "win32":
                    self._apply_windows_update(path)
                else:
                    self._apply_linux_update(Path(path))

        self._updater.download_progress.connect(_on_progress)
        self._updater.download_done.connect(_on_done)
        self._updater.download(url)
        dlg.exec()

    def _apply_windows_update(self, zip_path: Path) -> None:
        """Hand the zip to the installer, then quit so it can do the swap."""
        from src.updater import WindowsUpdateInstaller

        QMessageBox.information(
            self,
            "Applying update",
            "SoftEdIBO will close now and reopen once the update is installed.\n"
            "This takes about a minute.",
        )

        installer = WindowsUpdateInstaller()
        try:
            installer.launch(zip_path)
        except (OSError, ValueError) as exc:
            # ValueError: a portable install extracted to a drive root has no
            # parent to rename, so staging_dir's with_name() refuses.
            QMessageBox.critical(
                self,
                "Update failed",
                f"Could not start the update installer:\n{exc}",
            )
            return

        QApplication.quit()

    def _apply_linux_update(self, new_appimage: Path) -> None:
        """Replace the AppImage after exit, then relaunch it.

        Running from a detached shell avoids replacing the currently executing
        binary, which would fail with errno 26 (Text file busy).
        """
        import shlex
        import subprocess
        import tempfile

        target = Path(os.environ.get("APPIMAGE", sys.executable))
        pid = os.getpid()

        if not os.access(target.parent, os.W_OK):
            new_appimage.unlink(missing_ok=True)
            QMessageBox.critical(
                self,
                "Update failed",
                f"No write permission to {target.parent}.\n"
                "Move the AppImage to a writable folder or run with permissions to update it.",
            )
            return

        quoted_new = shlex.quote(str(new_appimage))
        quoted_target = shlex.quote(str(target))
        quoted_args = " ".join(shlex.quote(arg) for arg in sys.argv[1:])

        log_dir = Path.home() / ".local" / "share" / "SoftEdIBO" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        update_log = shlex.quote(str(log_dir / "update-apply.log"))

        script_content = (
            "#!/bin/sh\n"
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done\n"
            f"echo '[update] applying at '$(date '+%Y-%m-%dT%H:%M:%S%z') >> {update_log}\n"
            f"if mv -f {quoted_new} {quoted_target}; then\n"
            f"  echo '[update] mv ok' >> {update_log}\n"
            "else\n"
            f"  echo '[update] mv failed, trying cp' >> {update_log}\n"
            f"  cp -f {quoted_new} {quoted_target} && rm -f {quoted_new}\n"
            "fi\n"
            f"chmod +x {quoted_target}\n"
            f"echo '[update] relaunching' >> {update_log}\n"
            # setsid creates a new session so the new process is fully detached
            # from this script. Do NOT redirect AppImage stdout/stderr - Qt needs
            # an open stderr to connect to the display on some compositors.
            f"setsid env APPIMAGE={quoted_target} {quoted_target} {quoted_args} &\n"
            f"echo '[update] done PID=$!' >> {update_log}\n"
        )

        fd, script_path = tempfile.mkstemp(prefix="softedibo-update-", suffix=".sh")
        os.close(fd)
        script_file = Path(script_path)
        script_file.write_text(script_content, encoding="utf-8")
        script_file.chmod(0o700)

        try:
            subprocess.Popen(
                ["/bin/sh", str(script_file)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            script_file.unlink(missing_ok=True)
            new_appimage.unlink(missing_ok=True)
            QMessageBox.critical(self, "Update failed", f"Could not schedule update: {exc}")
            return

        QApplication.quit()

    def _show_about(self) -> None:
        build_line = (
            f"<br>Built: {__build_time__}"
            if __build_time__
            else ""
        )
        QMessageBox.about(
            self,
            "About SoftEdIBO",
            f"<b>SoftEdIBO</b><br>"
            f"Version: {__version__}"
            f"{build_line}<br><br>"
            f"Soft-based robot for inclusive education .<br><br>"
            f"LASIGE, Faculdade de Ciencias, Universidade de Lisboa",
        )

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    # Text-entry widgets where "0" must reach the field, not the panic key.
    _TEXT_INPUT_TYPES = (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)

    def eventFilter(self, obj, event) -> bool:
        """App-wide panic keys: a bare "0" fires the emergency stop, a bare
        "1" re-arms (same confirmed path as clicking the re-arm button).

        Installed on the QApplication so it works from any tab or dialog. It is
        suppressed while a text-entry widget (or an editable combo box) has
        focus, so typing digits into a value field still works. "1" is only
        consumed while stopped - it never re-arms an already-armed system.
        """
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if (not event.isAutoRepeat()
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and not self._focus_is_text_input()):
                if event.key() == Qt.Key.Key_0:
                    self._emergency_stop()
                    return True
                if (event.key() == Qt.Key.Key_1
                        and self._estop_button.is_stopped):
                    self._rearm()
                    return True
        return super().eventFilter(obj, event)

    def _focus_is_text_input(self) -> bool:
        w = QApplication.focusWidget()
        if w is None:
            return False
        if isinstance(w, QComboBox):
            return w.isEditable()
        return isinstance(w, self._TEXT_INPUT_TYPES)

    def _emergency_stop(self) -> None:
        """Halt everything: pumps off + valves closed on all nodes, session frozen.

        Idempotent - pressing the panic key repeatedly just re-issues the stop.
        """
        for robot in self._robots:
            try:
                robot.emergency_stop()
            except Exception:
                logger.exception("emergency_stop failed for %s", robot.robot_id)
        self._session_panel.emergency_stop()
        self._estop_button.set_stopped(True)
        self.statusBar().showMessage(
            "EMERGENCY STOP - all pumps off, valves closed. "
            "Press 1 or click the button to re-arm.")
        logger.warning("EMERGENCY STOP triggered")

    def _rearm(self) -> None:
        """Re-enable actuation after an emergency stop (asks for confirmation)."""
        # The '1' key reaches here through the app-wide event filter, so guard
        # against re-entry while the confirmation dialog is already open.
        if self._rearm_confirm_open:
            return
        self._rearm_confirm_open = True
        try:
            answer = QMessageBox.question(
                self, "Re-arm robots",
                "Re-arm all robots? Pumps and valves will be allowed to run again.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        finally:
            self._rearm_confirm_open = False
        if answer != QMessageBox.StandardButton.Yes:
            return
        for robot in self._robots:
            try:
                robot.rearm()
            except Exception:
                logger.exception("rearm failed for %s", robot.robot_id)
        self._session_panel.emergency_rearm()
        self._estop_button.set_stopped(False)
        self.statusBar().showMessage("Robots re-armed.", 4000)
        logger.warning("Robots re-armed after emergency stop")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Disconnect hardware and close the database on exit."""
        if self._gateway.is_connected:
            self._gateway.disconnect()
        dongle = getattr(self, "_thymio_dongle", None)
        if dongle is not None:
            dongle.close()   # stops thymiodirect's non-daemon threads
        self._db.close()
        super().closeEvent(event)


def create_app() -> tuple[QApplication, MainWindow]:
    """Create and return the application and main window."""
    app = QApplication(sys.argv)
    # Diagnostic only - off unless SOFTEDIBO_WATCHDOG is set. Dumps the GUI
    # thread's stack to stderr whenever the event loop stalls (busy cursor).
    from src.gui.loop_watchdog import install_loop_watchdog
    install_loop_watchdog(app)
    window = MainWindow()
    return app, window

"""Main application window for SoftEdIBO."""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
)

from src._version import __build_time__, __version__
from src.config.settings import Settings
from src.updater import AppUpdater
from src.data.database import Database
from src.gui.data_panel import DataPanel
from src.gui.home_panel import HomePanel
from src.gui.participant_panel import ParticipantPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel
from src.gui.settings_dialog import SettingsDialog
from src.gui.ui_main_window import Ui_MainWindow
from src.hardware.espnow_gateway import ESPNowGateway
from src.robots.base_robot import BaseRobot
from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.tree.tree_robot import TreeRobot
from src.robots.turtle.turtle_robot import TurtleRobot


class MainWindow(QMainWindow, Ui_MainWindow):
    """Main application window with tabbed panels."""

    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self._settings = Settings()

        self._db = Database.from_settings(self._settings.db_cfg, Settings.ROOT)
        self._db.connect()

        # Shared ESP-NOW gateway (not connected yet; user clicks Connect)
        self._gateway = ESPNowGateway(
            port=self._settings.gateway_port,
            baud_rate=self._settings.gateway_baud,
        )

        self._home_panel = HomePanel()
        self._participant_panel = ParticipantPanel(self._db)
        self._session_panel = SessionPanel(self._db)
        self._robot_panel = RobotPanel(self._gateway, self._settings)
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
        self._robot_panel.gateway_changed.connect(self._home_panel.set_gateway_status)
        self._robot_panel.robot_configured.connect(self._on_robot_configured)

        self._robots = self._load_robots()
        self._session_panel.set_available_robots(self._robots)
        self._robot_panel.refresh(self._robots)

        # Menu bar actions (structure defined in main_window.ui)
        self.actionSettings.triggered.connect(self._open_settings)
        self.actionFlashFirmware.triggered.connect(self._open_flash_wizard)
        self.actionCheckForUpdates.triggered.connect(self._check_updates_manual)
        self.actionAbout.triggered.connect(self._show_about)

        # OTA updater — silent background check 5 s after startup
        self._updater = AppUpdater(self)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.error.connect(
            lambda msg: self.statusBar().showMessage(f"Update error: {msg}", 6000)
        )
        self.setWindowTitle(f"SoftEdIBO  {__version__}")
        self.statusBar().show()  # keep bar always visible to prevent layout shifts
        QTimer.singleShot(5000, self._updater.check)

    # ------------------------------------------------------------------
    # Robot loading
    # ------------------------------------------------------------------

    def _load_robots(self) -> list[BaseRobot]:
        """Instantiate all robots declared in settings.yaml."""
        robots: list[BaseRobot] = []
        robot_data = self._settings.data.get("robots", {})

        # Turtles — one TurtleRobot per entry
        for turtle_cfg in robot_data.get("turtles", []):
            skin_cfgs = turtle_cfg.get("skins", [])
            if skin_cfgs:
                robots.append(
                    TurtleRobot(turtle_cfg.get("id", "turtle"), self._gateway, skin_cfgs)
                )

        # Trees — one TreeRobot per entry
        for tree_cfg in robot_data.get("trees", []):
            skin_cfgs = tree_cfg.get("skins", [])
            if skin_cfgs:
                robots.append(
                    TreeRobot(tree_cfg.get("id", "tree"), self._gateway, skin_cfgs)
                )

        # Thymios — one ThymioRobot per entry, each with its own host/port
        for thymio_cfg in robot_data.get("thymios", []):
            robots.append(
                ThymioRobot(
                    thymio_cfg["thymio_id"],
                    thymio_cfg.get("host", "localhost"),
                    int(thymio_cfg.get("port", 8596)),
                    gateway=self._gateway,
                    skin_configs=thymio_cfg.get("skins", []),
                )
            )

        return robots

    def _open_flash_wizard(self) -> None:
        from src.gui.setup_wizard import SetupWizard
        wizard = SetupWizard(parent=self)
        wizard.exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, parent=self)
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.exec()

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
        self._robots = self._load_robots()
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
        """Triggered from Tools => Check for Updates…"""
        self.statusBar().showMessage("Checking for updates…", 4000)
        self._updater.check()

    def _start_update(self, version: str, url: str) -> None:
        dlg = QProgressDialog(
            f"Downloading SoftEdIBO {version}…", "Cancel", 0, 100, self
        )
        dlg.setWindowTitle("Updating SoftEdIBO")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        dlg.canceled.connect(self._updater.cancel)

        def _on_progress(recv: int, total: int) -> None:
            if total > 0:
                dlg.setValue(int(recv / total * 100))

        def _on_done(path) -> None:
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
        """Launch a PowerShell script that extracts the update zip after we exit."""
        import subprocess

        exe = sys.executable
        pid = os.getpid()

        # zip_path is next to SoftEdIBO.exe — extract to its own directory
        # so files are replaced in-place without needing a separate install_dir.
        ps_script = (
            f"Wait-Process -Id {pid} -Timeout 30 -ErrorAction SilentlyContinue\n"
            f"Start-Sleep -Seconds 1\n"
            f"Expand-Archive -Path '{zip_path}' -DestinationPath (Split-Path '{zip_path}') -Force\n"
            f"Remove-Item '{zip_path}' -ErrorAction SilentlyContinue\n"
            f"Start-Process '{exe}'\n"
        )

        ps_file = zip_path.with_name("softedibo-update.ps1")
        ps_file.write_text(ps_script, encoding="utf-8")

        subprocess.Popen(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(ps_file)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
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
        # Prefer APPIMAGE_EXTRACT_AND_RUN, but keep a plain AppImage fallback.
        restart_cmd = (
            f"env APPIMAGE_EXTRACT_AND_RUN=1 APPIMAGE={quoted_target} "
            f"{quoted_target} {quoted_args}"
        ).strip()
        restart_cmd_fallback = (
            f"env APPIMAGE={quoted_target} {quoted_target} {quoted_args}"
        ).strip()

        log_dir = Path.home() / ".local" / "share" / "SoftEdIBO" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        update_log = shlex.quote(str(log_dir / "update-apply.log"))

        script_content = (
            "#!/bin/sh\n"
            "set -u\n"
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done\n"
            f"echo '[update] script v2 applying at '$(date '+%Y-%m-%dT%H:%M:%S%z') >> {update_log}\n"
            f"if mv -f {quoted_new} {quoted_target}; then\n"
            f"  echo '[update] replace via mv ok' >> {update_log}\n"
            "else\n"
            f"  echo '[update] mv failed, trying cp fallback' >> {update_log}\n"
            f"  if cp -f {quoted_new} {quoted_target}; then\n"
            f"    rm -f {quoted_new}\n"
            f"    echo '[update] replace via cp ok' >> {update_log}\n"
            "  else\n"
            f"    echo '[update] cp fallback failed' >> {update_log}\n"
            "  fi\n"
            "fi\n"
            f"if chmod +x {quoted_target}; then\n"
            f"  echo '[update] chmod ok' >> {update_log}\n"
            "else\n"
            f"  echo '[update] chmod failed' >> {update_log}\n"
            "fi\n"
            f"echo '[update] relaunch attempt 1' >> {update_log}\n"
            f"if {restart_cmd} >> {update_log} 2>&1; then\n"
            "  exit 0\n"
            "fi\n"
            f"echo '[update] relaunch attempt 1 failed, trying fallback' >> {update_log}\n"
            f"if {restart_cmd_fallback} >> {update_log} 2>&1; then\n"
            "  exit 0\n"
            "fi\n"
            f"echo '[update] relaunch failed' >> {update_log}\n"
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
            f"LASIGE, Faculdade de Ciências, Universidade de Lisboa",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Disconnect hardware and close the database on exit."""
        if self._gateway.is_connected:
            self._gateway.disconnect()
        self._db.close()
        super().closeEvent(event)


def create_app() -> tuple[QApplication, MainWindow]:
    """Create and return the application and main window."""
    app = QApplication(sys.argv)
    window = MainWindow()
    return app, window

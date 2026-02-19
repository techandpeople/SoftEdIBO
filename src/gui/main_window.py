"""Main application window for SoftEdIBO."""

import sys

from PySide6.QtWidgets import QApplication, QMainWindow

from src.config.settings import Settings
from src.data.database import Database
from src.gui.data_panel import DataPanel
from src.gui.home_panel import HomePanel
from src.gui.participant_panel import ParticipantPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel
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

        # Database — path resolved relative to project root via settings.yaml
        db_path = self._settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = Database(str(db_path))
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
        self._session_panel.session_stopped.connect(lambda: self._home_panel.set_session_status(None))
        self._session_panel.session_finished.connect(self._data_panel.refresh)
        self._robot_panel.gateway_changed.connect(self._home_panel.set_gateway_status)
        self._robot_panel.robot_configured.connect(self._on_robot_configured)

        self._robots = self._load_robots()
        self._session_panel.set_available_robots(self._robots)
        self._robot_panel.refresh(self._robots)

    # ------------------------------------------------------------------
    # Robot loading
    # ------------------------------------------------------------------

    def _load_robots(self) -> list[BaseRobot]:
        """Instantiate all robots declared in settings.yaml."""
        robots: list[BaseRobot] = []
        robot_data = self._settings.data.get("robots", {})

        # Turtles — one TurtleRobot per entry, each aggregating its nodes
        for turtle_cfg in robot_data.get("turtles", []):
            nodes = turtle_cfg.get("nodes", [])
            if nodes:
                robots.append(
                    TurtleRobot(turtle_cfg.get("id", "turtle"), self._gateway, nodes)
                )

        # Trees — one TreeRobot per entry, each aggregating its nodes
        for tree_cfg in robot_data.get("trees", []):
            nodes = tree_cfg.get("nodes", [])
            if nodes:
                robots.append(
                    TreeRobot(tree_cfg.get("id", "tree"), self._gateway, nodes)
                )

        # Thymios — one ThymioRobot per entry
        thymio_host = self._settings.data.get("thymio", {}).get("host", "localhost")
        thymio_port = self._settings.data.get("thymio", {}).get("port", 8596)
        for thymio_cfg in robot_data.get("thymios", []):
            robots.append(
                ThymioRobot(thymio_cfg["thymio_id"], thymio_host, thymio_port)
            )

        return robots

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

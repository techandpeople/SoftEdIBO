"""Basic GUI smoke tests — verify panels and dialogs load correctly."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.activities.group_touch import GroupTouchActivity
from src.data.database import Database
from src.data.models import SessionRecord
from src.gui.data_panel import DataPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel
from src.gui.session_setup_dialog import SessionSetupDialog
from src.robots.base_robot import RobotStatus
from src.robots.turtle.turtle_robot import TurtleRobot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """In-memory-like database backed by a temporary file."""
    database = Database(str(tmp_path / "test.db"))
    database.connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_turtle(name: str = "Turtle-1") -> MagicMock:
    robot = MagicMock(spec=TurtleRobot)
    robot.name = name
    robot.status = RobotStatus.CONNECTED
    return robot


# ---------------------------------------------------------------------------
# SessionPanel
# ---------------------------------------------------------------------------

class TestSessionPanel:
    def test_creates(self, qtbot, db):
        panel = SessionPanel(db)
        qtbot.addWidget(panel)

    def test_initial_labels(self, qtbot, db):
        panel = SessionPanel(db)
        qtbot.addWidget(panel)
        assert panel.session_id_label.text() == "—"
        assert panel.activity_label.text() == "—"
        assert panel.robots_label.text() == "—"

    def test_buttons_initial_state(self, qtbot, db):
        panel = SessionPanel(db)
        qtbot.addWidget(panel)
        assert panel.new_session_btn.isEnabled()
        assert not panel.pause_btn.isEnabled()
        assert not panel.stop_btn.isEnabled()

    def test_status_label(self, qtbot, db):
        panel = SessionPanel(db)
        qtbot.addWidget(panel)
        assert "No active session" in panel.status_label.text()


# ---------------------------------------------------------------------------
# SessionSetupDialog
# ---------------------------------------------------------------------------

class TestSessionSetupDialog:
    def test_creates_empty(self, qtbot):
        dlg = SessionSetupDialog(robots=[])
        qtbot.addWidget(dlg)

    def test_activity_combo_populated(self, qtbot):
        dlg = SessionSetupDialog(robots=[])
        qtbot.addWidget(dlg)
        assert dlg.activity_combo.count() > 0
        assert dlg.activity_combo.itemText(0) == "Group Touch"

    def test_robot_type_label_set_on_open(self, qtbot):
        dlg = SessionSetupDialog(robots=[])
        qtbot.addWidget(dlg)
        assert dlg.robot_type_label.text() == TurtleRobot.__name__

    def test_no_robots_label_shown_when_empty(self, qtbot):
        dlg = SessionSetupDialog(robots=[])
        qtbot.addWidget(dlg)
        assert not dlg.no_robots_label.isHidden()
        assert dlg.robots_list.isHidden()

    def test_robots_list_shown_when_robots_present(self, qtbot):
        dlg = SessionSetupDialog(robots=[_mock_turtle()])
        qtbot.addWidget(dlg)
        assert dlg.no_robots_label.isHidden()
        assert not dlg.robots_list.isHidden()
        assert dlg.robots_list.count() == 1

    def test_robots_filtered_by_activity_type(self, qtbot):
        from src.robots.thymio.thymio_robot import ThymioRobot
        thymio = MagicMock(spec=ThymioRobot)
        thymio.name = "Thymio-1"
        thymio.status = RobotStatus.CONNECTED
        dlg = SessionSetupDialog(robots=[_mock_turtle(), thymio])
        qtbot.addWidget(dlg)
        assert dlg.robots_list.count() == 1

    def test_selected_activity_returns_instance(self, qtbot):
        dlg = SessionSetupDialog(robots=[])
        qtbot.addWidget(dlg)
        assert isinstance(dlg.selected_activity, GroupTouchActivity)


# ---------------------------------------------------------------------------
# RobotPanel
# ---------------------------------------------------------------------------

class TestRobotPanel:
    def test_creates(self, qtbot):
        panel = RobotPanel()
        qtbot.addWidget(panel)

    def test_lists_start_empty(self, qtbot):
        panel = RobotPanel()
        qtbot.addWidget(panel)
        assert panel.turtle_list.count() == 0
        assert panel.thymio_list.count() == 0
        assert panel.tree_list.count() == 0

    def test_refresh_populates_turtle_list(self, qtbot):
        panel = RobotPanel()
        qtbot.addWidget(panel)
        panel.refresh([_mock_turtle("Turtle-1"), _mock_turtle("Turtle-2")])
        assert panel.turtle_list.count() == 2
        assert panel.thymio_list.count() == 0

    def test_gateway_connect_btn_present(self, qtbot):
        panel = RobotPanel()
        qtbot.addWidget(panel)
        assert panel.gateway_connect_btn.text() == "Connect"


# ---------------------------------------------------------------------------
# DataPanel
# ---------------------------------------------------------------------------

class TestDataPanel:
    def test_creates(self, qtbot, db):
        panel = DataPanel(db)
        qtbot.addWidget(panel)

    def test_sessions_table_columns(self, qtbot, db):
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.sessions_table.columnCount() == 4

    def test_events_table_columns(self, qtbot, db):
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.events_table.columnCount() == 6

    def test_export_button(self, qtbot, db):
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.export_btn.text() == "Export to CSV"

    def test_refresh_loads_sessions_from_db(self, qtbot, db):
        db.save_session(SessionRecord(
            session_id="s01",
            activity_name="Group Touch",
            start_time=datetime(2026, 1, 1, 10, 0),
            end_time=datetime(2026, 1, 1, 10, 30),
        ))
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.sessions_table.rowCount() == 1
        assert panel.sessions_table.item(0, 0).text() == "s01"

    def test_refresh_shows_sessions_ordered_newest_first(self, qtbot, db):
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        db.save_session(SessionRecord("s02", "Group Touch", datetime(2026, 1, 1, 10, 0)))
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.sessions_table.item(0, 0).text() == "s02"

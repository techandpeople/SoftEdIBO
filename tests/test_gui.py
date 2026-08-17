"""Basic GUI smoke tests - verify panels and dialogs load correctly."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.activities.scripted_activity import ScriptedActivity
from src.activities.seed_behaviors import SEED_CONDITIONS
from src.core.session_trash import SessionTrash
from src.data.database import Database
from src.data.models import DeclarativeActivity, SessionRecord
from src.gui.data_panel import DataPanel
from src.gui.trash_dialog import TrashDialog
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel
from src.gui.session_setup_dialog import SessionSetupDialog
from src.hardware.gateway import Gateway
from src.robots.base_robot import BaseRobot, RobotStatus
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
    robot.robot_id = name.lower().replace(" ", "-")
    robot.robot_kind = "turtle"
    robot.status = RobotStatus.CONNECTED
    robot.skins = {}
    robot.nodes_seen_since.return_value = (0, 0)
    robot.gateway = None
    return robot


def _mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.gateway_baud = 115200
    settings.gateway_port = "/dev/ttyUSB0"
    settings.data = {"robots": {"turtles": [], "trees": [], "thymios": []}, "gateway": {}}
    return settings


def _seed_behaviour(db, name: str = "Example Behaviour") -> str:
    """Insert one declarative behaviour so the session dropdown has an entry.

    No code-defined activities ship anymore - all behaviours come from the DB
    (block editor / imported JSON), so the dialog tests must seed their own."""
    da = DeclarativeActivity(
        activity_id=db.next_declarative_activity_id(),
        name=name, spec=SEED_CONDITIONS[0][2], created_at=datetime.now())
    db.save_declarative_activity(da)
    return name


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
        assert panel.session_id_label.text() == "-"
        assert panel.activity_label.text() == "-"
        assert panel.robots_label.text() == "-"

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
    def test_creates_empty(self, qtbot, db):
        dlg = SessionSetupDialog(robots=[], db=db)
        qtbot.addWidget(dlg)

    def test_activity_combo_populated(self, qtbot, db):
        name = _seed_behaviour(db)
        dlg = SessionSetupDialog(robots=[], db=db)
        qtbot.addWidget(dlg)
        assert dlg.activity_combo.count() > 0
        assert dlg.activity_combo.itemText(0) == name

    def test_target_skin_label_set_on_open(self, qtbot, db):
        _seed_behaviour(db)
        dlg = SessionSetupDialog(robots=[], db=db)
        qtbot.addWidget(dlg)
        # The seed behaviour has no skin target, so the label shows "Any".
        assert dlg.target_skin_label.text() == "Any"

    def test_no_robots_label_shown_when_empty(self, qtbot, db):
        dlg = SessionSetupDialog(robots=[], db=db)
        qtbot.addWidget(dlg)
        assert not dlg.no_robots_label.isHidden()
        assert dlg.robots_list.isHidden()

    def test_robots_list_shown_when_robots_present(self, qtbot, db):
        _seed_behaviour(db)
        dlg = SessionSetupDialog(robots=[_mock_turtle()], db=db)
        qtbot.addWidget(dlg)
        assert dlg.no_robots_label.isHidden()
        assert not dlg.robots_list.isHidden()
        assert dlg.robots_list.count() == 1

    def test_all_robots_shown_for_scripted_activity(self, qtbot, db):
        # A scripted behaviour's robot_type is BaseRobot, so every robot kind
        # is compatible - no type filtering trims the list.
        _seed_behaviour(db)
        from src.robots.thymio.thymio_robot import ThymioRobot
        thymio = MagicMock(spec=ThymioRobot)
        thymio.name = "Thymio-1"
        thymio.robot_id = "thymio-1"
        thymio.robot_kind = "thymio"
        thymio.status = RobotStatus.CONNECTED
        thymio.skins = {}
        thymio.nodes_seen_since.return_value = (0, 0)
        thymio.gateway = None
        dlg = SessionSetupDialog(robots=[_mock_turtle(), thymio], db=db)
        qtbot.addWidget(dlg)
        assert dlg.robots_list.count() == 2

    def test_selected_activity_returns_instance(self, qtbot, db):
        _seed_behaviour(db)
        dlg = SessionSetupDialog(robots=[], db=db)
        qtbot.addWidget(dlg)
        assert isinstance(dlg.selected_activity, ScriptedActivity)


# ---------------------------------------------------------------------------
# RobotPanel
# ---------------------------------------------------------------------------

class TestRobotPanel:
    def test_creates(self, qtbot):
        panel = RobotPanel(Gateway("/dev/null"), _mock_settings())
        qtbot.addWidget(panel)

    def test_lists_start_empty(self, qtbot):
        panel = RobotPanel(Gateway("/dev/null"), _mock_settings())
        qtbot.addWidget(panel)
        assert panel.turtles_tree.topLevelItemCount() == 0
        assert panel.trees_tree.topLevelItemCount() == 0
        assert panel.thymio_tree.topLevelItemCount() == 0

    def test_refresh_populates_turtle_list(self, qtbot):
        panel = RobotPanel(Gateway("/dev/null"), _mock_settings())
        qtbot.addWidget(panel)
        panel.refresh([_mock_turtle("Turtle-1"), _mock_turtle("Turtle-2")])
        assert panel.turtles_tree.topLevelItemCount() == 0
        assert panel.trees_tree.topLevelItemCount() == 0
        assert panel.thymio_tree.topLevelItemCount() == 0

    def test_gateway_connect_btn_present(self, qtbot):
        panel = RobotPanel(Gateway("/dev/null"), _mock_settings())
        qtbot.addWidget(panel)
        assert panel.connect_btn.text() == "Connect"


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
        # Export now supports multi-select; with nothing selected the button shows
        # its default "Export Selected to CSV" (relabelled with a count once rows are
        # picked). A separate "Export All" button covers the whole roster.
        assert panel.export_btn.text() == "Export Selected to CSV"

    def test_delete_button_disabled_until_selection(self, qtbot, db):
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.delete_btn.text() == "Delete Session"
        assert not panel.delete_btn.isEnabled()
        panel.sessions_table.selectRow(0)
        assert panel.delete_btn.isEnabled()

    def test_trash_button_present(self, qtbot, db):
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        assert panel.trash_btn.text() == "Trash..."

    def test_delete_moves_selected_session_to_trash(self, qtbot, db, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        panel.sessions_table.selectRow(0)
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
        panel._on_delete()
        assert panel.sessions_table.rowCount() == 0
        assert db.get_all_sessions() == []
        assert [t.session_id for t in db.list_trashed_sessions()] == ["s01"]


# ---------------------------------------------------------------------------
# TrashDialog
# ---------------------------------------------------------------------------

class TestTrashDialog:
    def test_lists_trashed_sessions(self, qtbot, db, tmp_path):
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        trash = SessionTrash(db, tmp_path)
        trash.trash("s01")
        dlg = TrashDialog(trash)
        qtbot.addWidget(dlg)
        assert dlg.trash_table.rowCount() == 1
        item = dlg.trash_table.item(0, 0)
        assert item is not None
        assert item.text() == "s01"

    def test_restore_puts_session_back(self, qtbot, db, tmp_path):
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        trash = SessionTrash(db, tmp_path)
        trash.trash("s01")
        dlg = TrashDialog(trash)
        qtbot.addWidget(dlg)
        dlg.trash_table.selectRow(0)
        dlg._on_restore()
        assert dlg.trash_table.rowCount() == 0
        assert [s.session_id for s in db.get_all_sessions()] == ["s01"]

    def test_empty_trash_purges_permanently(self, qtbot, db, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        trash = SessionTrash(db, tmp_path)
        trash.trash("s01")
        dlg = TrashDialog(trash)
        qtbot.addWidget(dlg)
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
        dlg._on_empty()
        assert dlg.trash_table.rowCount() == 0
        assert db.list_trashed_sessions() == []

    def test_empty_button_disabled_when_trash_empty(self, qtbot, db, tmp_path):
        dlg = TrashDialog(SessionTrash(db, tmp_path))
        qtbot.addWidget(dlg)
        assert not dlg.empty_trash_btn.isEnabled()
        assert not dlg.restore_btn.isEnabled()

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
        item = panel.sessions_table.item(0, 0)
        assert item is not None
        assert item.text() == "s01"

    def test_refresh_shows_sessions_ordered_newest_first(self, qtbot, db):
        db.save_session(SessionRecord("s01", "Group Touch", datetime(2026, 1, 1, 9, 0)))
        db.save_session(SessionRecord("s02", "Group Touch", datetime(2026, 1, 1, 10, 0)))
        panel = DataPanel(db)
        qtbot.addWidget(panel)
        item = panel.sessions_table.item(0, 0)
        assert item is not None
        assert item.text() == "s01"


# ---------------------------------------------------------------------------
# ObserverPanel
# ---------------------------------------------------------------------------

class TestObserverPanel:
    def test_emits_observer_event_per_participant(self, qtbot):
        from src.data.models import ParticipantRecord
        from src.gui.observer_panel import ObserverPanel

        participants = [ParticipantRecord("P1", "Ana"), ParticipantRecord("P2", "Bea")]
        panel = ObserverPanel(participants)
        qtbot.addWidget(panel)

        captured = []
        panel.observed.connect(lambda *a: captured.append(a))
        panel._emit_observation("P1", "watches")
        assert captured == [("observer", "watches", "P1", "")]


# ---------------------------------------------------------------------------
# TestActuatorsDialog - one-shot inflate/deflate respects configured limits
# ---------------------------------------------------------------------------

class _RecordingGateway:
    """Captures every (cmd, kwargs) the dialog sends to the node."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def send(self, _mac, cmd, **kwargs):
        self.sent.append((cmd, kwargs))
        return True

    def on_message(self, _cb):
        pass


class TestActuatorsDialogActuation:
    MAC = "AA:BB:CC:DD:EE:FF"

    def _dialog(self, qtbot, gateway, *, fill_mode, fill_time_ms=None):
        from src.gui.test_actuators_dialog import TestActuatorsDialog

        skin_cfgs = [{
            "skin_id": "s",
            "chambers": [{
                "slot": 0, "max_pressure": 6.0, "min_pressure": -2.0,
                "fill_mode": fill_mode, "fill_time_ms": fill_time_ms,
            }],
        }]
        dlg = TestActuatorsDialog(self.MAC, skin_cfgs, gateway, led_count=0)
        qtbot.addWidget(dlg)
        gateway.sent.clear()   # drop anything sent during construction
        return dlg

    def test_inflate_pushes_configured_limits_before_actuating(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw, fill_mode="pressure")
        dlg._inflate_slot(0)
        cmds = [c for c, _ in gw.sent]
        # Limits go out (max then min) before the inflate so the node targets the
        # configured caps, not its boot defaults.
        assert cmds[:2] == ["set_max_pressure", "set_min_pressure"]
        assert gw.sent[0][1] == {"chamber": 0, "value": 6.0}
        assert gw.sent[1][1] == {"chamber": 0, "value": -2.0}
        assert "inflate" in cmds

    def test_inflate_pressure_mode_closes_the_loop_no_time_window(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw, fill_mode="pressure", fill_time_ms=3000)
        dlg._inflate_slot(0)
        inflate = [kw for c, kw in gw.sent if c == "inflate"]
        assert inflate == [{"chamber": 0, "delta": 100}]

    def test_inflate_time_mode_uses_calibrated_time_window(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw, fill_mode="time", fill_time_ms=3000)
        dlg._inflate_slot(0)
        inflate = [kw for c, kw in gw.sent if c == "inflate"]
        assert inflate == [{"chamber": 0, "delta": 100, "ms": 3000}]

    def test_inflate_time_mode_without_calibration_falls_back_to_pressure(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw, fill_mode="time", fill_time_ms=None)
        dlg._inflate_slot(0)
        inflate = [kw for c, kw in gw.sent if c == "inflate"]
        assert inflate == [{"chamber": 0, "delta": 100}]

    def test_deflate_pushes_limits_and_runs_to_configured_min(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw, fill_mode="time", fill_time_ms=3000)
        dlg._deflate_slot(0)
        cmds = [c for c, _ in gw.sent]
        assert cmds[:2] == ["set_max_pressure", "set_min_pressure"]
        deflate = [kw for c, kw in gw.sent if c == "deflate"]
        assert deflate == [{"chamber": 0, "delta": 100}]


class TestActuatorsDialogSensors:
    """The live magnet-sensor readout embedded in the Test Actuators dialog."""

    MAC = "AA:BB:CC:DD:EE:FF"

    def _dialog(self, qtbot, gateway):
        from src.gui.test_actuators_dialog import TestActuatorsDialog

        dlg = TestActuatorsDialog(self.MAC, [], gateway, led_count=0)
        qtbot.addWidget(dlg)
        gateway.sent.clear()
        return dlg

    def test_sensor_tester_built_lazily_on_first_magnet_frame(self, qtbot):
        dlg = self._dialog(qtbot, _RecordingGateway())
        assert dlg._sensor_tester is None            # nothing until a frame arrives
        dlg._on_magnet_data([10.0, 200.0, 0.0, 0.0])
        assert dlg._sensor_tester is not None
        assert dlg._sensor_tester._count == 4        # count follows the frame length

    def test_active_highlight_follows_threshold(self, qtbot):
        dlg = self._dialog(qtbot, _RecordingGateway())
        dlg._on_magnet_data([10.0, 200.0, 0.0, 0.0])
        tester = dlg._sensor_tester
        assert tester is not None
        tester.threshold_spin.setValue(100.0)
        tester.update_values([10.0, 200.0, 0.0, 0.0])
        # Only S1 (200 uT) clears the 100 uT threshold.
        assert tester._active_shown == [False, True, False, False]
        # Dropping the threshold below S0 lights it too; the two 0 uT sensors stay off.
        tester.threshold_spin.setValue(5.0)
        assert tester._active_shown == [True, True, False, False]

    def test_rezero_sends_rebaseline(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw)
        dlg._on_magnet_data([0.0, 0.0, 0.0, 0.0])
        tester = dlg._sensor_tester
        assert tester is not None
        tester.rezero_btn.click()
        assert ("rebaseline", {}) in gw.sent

    def test_push_sends_threshold_uT_to_node(self, qtbot):
        gw = _RecordingGateway()
        dlg = self._dialog(qtbot, gw)
        dlg._on_magnet_data([0.0, 0.0, 0.0, 0.0])
        tester = dlg._sensor_tester
        assert tester is not None
        tester.threshold_spin.setValue(120.0)
        tester.push_btn.click()
        cfg = [kw for c, kw in gw.sent if c == "configure"]
        assert len(cfg) == 1
        # The uT threshold goes straight to the node - it flips a sensor active at
        # exactly 120 uT (no fullscale/fraction gymnastics).
        assert cfg[0]["act_threshold_ut"] == pytest.approx(120.0)

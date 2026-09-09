"""Native crash log: harvest-on-start semantics and the Qt fatal bridge."""

from __future__ import annotations

import logging

from PySide6.QtCore import QMessageLogContext, QtMsgType

from src import crash_handler
from src.crash_handler import NativeCrashLog, QtMessageBridge, format_qt_fatal


def _log(tmp_path, monkeypatch) -> NativeCrashLog:
    """A crash log whose timestamped copies land in tmp_path/state."""
    monkeypatch.setattr(crash_handler, "app_state_dir", lambda _name="x": tmp_path / "state")
    return NativeCrashLog(tmp_path / "native-crash.log", "TestApp")


def test_take_previous_nothing_when_no_file(tmp_path, monkeypatch):
    assert _log(tmp_path, monkeypatch).take_previous() is None


def test_take_previous_ignores_empty_file_and_removes_it(tmp_path, monkeypatch):
    log = _log(tmp_path, monkeypatch)
    log.path.write_text("   \n")
    assert log.take_previous() is None
    assert not log.path.exists()


def test_take_previous_returns_trace_persists_copy_and_clears(tmp_path, monkeypatch):
    log = _log(tmp_path, monkeypatch)
    log.path.write_text("Fatal Python error: Aborted\n  File x.py line 1\n")

    previous = log.take_previous()

    assert previous is not None
    assert "Fatal Python error: Aborted" in previous.text
    assert previous.saved_to is not None and previous.saved_to.read_text() == previous.text
    assert previous.saved_to.parent == tmp_path / "state"
    assert not log.path.exists()          # next run starts from a clean slate
    assert log.take_previous() is None


def test_open_then_write_appends_and_flushes(tmp_path, monkeypatch):
    log = _log(tmp_path, monkeypatch)
    stream = log.open()
    assert stream is not None
    log.write("first")
    log.write("second\n")
    assert log.path.read_text() == "first\nsecond\n"
    assert log.open() is stream           # opened once


def test_write_without_open_creates_file(tmp_path, monkeypatch):
    log = _log(tmp_path / "nested", monkeypatch)
    log.write("boom")
    assert log.path.read_text() == "boom\n"


def test_format_qt_fatal_has_message_and_python_stack():
    text = format_qt_fatal("Could not initialize GLX")
    assert text.startswith("Qt fatal error: Could not initialize GLX")
    assert "test_format_qt_fatal_has_message_and_python_stack" in text


def test_bridge_routes_non_fatal_messages_to_logging(tmp_path, monkeypatch, caplog):
    bridge = QtMessageBridge(_log(tmp_path, monkeypatch), "TestApp")
    with caplog.at_level(logging.DEBUG, logger="qt"):
        bridge._handle(QtMsgType.QtWarningMsg, QMessageLogContext(), "qt.glx: no FBConfig")
        bridge._handle(QtMsgType.QtCriticalMsg, QMessageLogContext(), "bad things")
    levels = {r.getMessage(): r.levelno for r in caplog.records if r.name == "qt"}
    assert levels["qt.glx: no FBConfig"] == logging.WARNING
    assert levels["bad things"] == logging.ERROR


def test_bridge_fatal_writes_crash_log_and_shows_dialog(tmp_path, monkeypatch):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(crash_handler, "_show_crash_dialog",
                        lambda trace, path, message="", **_kw: shown.append((trace, message)))
    log = _log(tmp_path, monkeypatch)
    bridge = QtMessageBridge(log, "TestApp")

    bridge._handle(QtMsgType.QtFatalMsg, QMessageLogContext(), "Could not initialize GLX")

    assert "Qt fatal error: Could not initialize GLX" in log.path.read_text()
    assert len(shown) == 1
    trace, message = shown[0]
    assert "Could not initialize GLX" in trace and "Could not initialize GLX" in message
    assert list((tmp_path / "state").glob("crash-*.log"))   # timestamped copy
    # The next start harvests exactly this crash.
    previous = log.take_previous()
    assert previous is not None and "Could not initialize GLX" in previous.text


def test_bridge_demotes_expected_warnings_to_debug(tmp_path, monkeypatch, caplog):
    bridge = QtMessageBridge(_log(tmp_path, monkeypatch), "TestApp",
                             expected=("QXcbIntegration: Cannot create platform OpenGL",))
    with caplog.at_level(logging.DEBUG, logger="qt"):
        bridge._handle(QtMsgType.QtWarningMsg, QMessageLogContext(),
                       "QXcbIntegration: Cannot create platform OpenGL context, neither GLX nor EGL")
        bridge._handle(QtMsgType.QtWarningMsg, QMessageLogContext(), "something else")
    levels = {r.getMessage(): r.levelno for r in caplog.records if r.name == "qt"}
    assert levels["something else"] == logging.WARNING
    assert [lvl for msg, lvl in levels.items() if msg.startswith("QXcbIntegration")] == [logging.DEBUG]

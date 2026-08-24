"""App-wide emergency stop: red button, panic keys, and the latch controller.

Everything emergency-stop related lives in this module, split by
responsibility:

* :class:`EmergencyStopButton` - the always-visible red button. Pure view:
  it reflects the latch state it is told about and emits intent as signals.
* :class:`PanicKeyFilter` - application-wide key filter. Pure input
  decoding: a bare ``0`` asks to stop, a bare ``1`` asks to re-arm (only
  consumed while stopped). Suppressed while a text-entry widget has focus
  so typing digits into a value field still works.
* :class:`EmergencyStopController` - the single owner of the latch state.
  It wires the button and the key filter to the actual stop/re-arm
  orchestration over the robots and the session panel, and asks for
  confirmation before re-arming.

Semantics are a HALT, not a vent: all pumps off and all valves closed,
latched until explicitly re-armed. The panic key only ever stops; ``1``
and the button click go through the same confirmed re-arm path.

The main window only builds the pieces::

    self._estop_button = EmergencyStopButton(self)
    self.menubar.setCornerWidget(self._estop_button, Qt.Corner.TopLeftCorner)
    self._estop = EmergencyStopController(
        button=self._estop_button,
        robots=lambda: self._robots,
        session_panel=self._session_panel,
        window=self,
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterable

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

if TYPE_CHECKING:
    from src.gui.session_panel import SessionPanel
    from src.robots.base_robot import BaseRobot

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Button (view)
# ----------------------------------------------------------------------

_ARMED_STYLE = (
    "QPushButton { background-color: #D32F2F; color: white; font-weight: bold; "
    "border: 2px solid #8E0000; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:hover { background-color: #E53935; }"
)
_STOPPED_STYLE = (
    "QPushButton { background-color: #FFC107; color: #4E342E; font-weight: bold; "
    "border: 2px solid #FF8F00; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:hover { background-color: #FFD54F; }"
)

_ARMED_TEXT = "EMERGENCY STOP (0)"
_STOPPED_TEXT = "STOPPED - re-arm (1)"

_HELP = (
    "Emergency stop. Immediately turns off every pump and closes every valve on "
    "all connected nodes, and freezes the running session. Also triggered by the "
    "'0' key anywhere in the app. Click again while stopped - or press the '1' "
    "key - to re-arm (a confirmation is asked first)."
)


class EmergencyStopButton(QPushButton):
    """Red latch button that requests an app-wide emergency stop / re-arm.

    Pure view: it only tracks the latch state it is told about via
    :meth:`set_stopped` (to pick its look and which intent a click means)
    and emits :attr:`stop_requested` / :attr:`rearm_requested`. The
    controller decides what stopping and re-arming actually do.
    """

    stop_requested = Signal()
    rearm_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stopped = False
        self.setWhatsThis(_HELP)
        self.setToolTip(_HELP)
        self.clicked.connect(self._on_clicked)
        self._refresh()

    def set_stopped(self, stopped: bool) -> None:
        """Reflect the latch state (called by the controller once it acted)."""
        self._stopped = stopped
        self._refresh()

    def _on_clicked(self) -> None:
        if self._stopped:
            self.rearm_requested.emit()
        else:
            self.stop_requested.emit()

    def _refresh(self) -> None:
        if self._stopped:
            self.setText(_STOPPED_TEXT)
            self.setStyleSheet(_STOPPED_STYLE)
        else:
            self.setText(_ARMED_TEXT)
            self.setStyleSheet(_ARMED_STYLE)


# ----------------------------------------------------------------------
# Panic keys (input)
# ----------------------------------------------------------------------

class PanicKeyFilter(QObject):
    """Application-wide panic keys: bare ``0`` = stop, bare ``1`` = re-arm.

    Install on the QApplication so the keys work from any tab or dialog.
    Both keys are ignored while a text-entry widget (or an editable combo
    box) has focus, so typing digits into a value field still works. ``1``
    is only consumed while the injected ``is_stopped`` predicate is true -
    it never re-arms an already-armed system, mirroring how ``0`` never
    re-arms.
    """

    stop_pressed = Signal()
    rearm_pressed = Signal()

    _TEXT_INPUT_TYPES = (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)

    def __init__(self, is_stopped: Callable[[], bool],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._is_stopped = is_stopped

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        if (event.isAutoRepeat()
                or event.modifiers() != Qt.KeyboardModifier.NoModifier
                or self._focus_is_text_input()):
            return False
        if event.key() == Qt.Key.Key_0:
            self.stop_pressed.emit()
            return True
        if event.key() == Qt.Key.Key_1 and self._is_stopped():
            self.rearm_pressed.emit()
            return True
        return False

    def _focus_is_text_input(self) -> bool:
        w = QApplication.focusWidget()
        if w is None:
            return False
        if isinstance(w, QComboBox):
            return w.isEditable()
        return isinstance(w, self._TEXT_INPUT_TYPES)


# ----------------------------------------------------------------------
# Controller (latch + orchestration)
# ----------------------------------------------------------------------

class EmergencyStopController(QObject):
    """Owns the emergency-stop latch and orchestrates stop / re-arm.

    Collaborators are injected: the button (view), a ``robots`` provider
    (called on every action - the robot list is rebuilt at runtime), the
    session panel (to freeze/resume the running activity) and the main
    window (confirmation-dialog parent + status bar). The panic-key filter
    is created here and installed on the QApplication.
    """

    def __init__(self, *, button: EmergencyStopButton,
                 robots: Callable[[], Iterable[BaseRobot]],
                 session_panel: SessionPanel,
                 window: QMainWindow) -> None:
        super().__init__(window)
        self._button = button
        self._robots = robots
        self._session_panel = session_panel
        self._window = window
        self._stopped = False
        self._confirming = False

        button.stop_requested.connect(self.stop)
        button.rearm_requested.connect(self.rearm)

        self._key_filter = PanicKeyFilter(is_stopped=lambda: self._stopped,
                                          parent=self)
        self._key_filter.stop_pressed.connect(self.stop)
        self._key_filter.rearm_pressed.connect(self.rearm)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._key_filter)

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        """Halt everything: pumps off + valves closed on all nodes, session frozen.

        Idempotent - pressing the panic key repeatedly just re-issues the stop.
        """
        for robot in self._robots():
            try:
                robot.emergency_stop()
            except Exception:
                logger.exception("emergency_stop failed for %s", robot.robot_id)
        self._session_panel.emergency_stop()
        self._stopped = True
        self._button.set_stopped(True)
        self._window.statusBar().showMessage(
            "EMERGENCY STOP - all pumps off, valves closed. "
            "Press 1 or click the button to re-arm.")
        logger.warning("EMERGENCY STOP triggered")

    def rearm(self) -> None:
        """Re-enable actuation after an emergency stop (asks for confirmation)."""
        if not self._stopped:
            return
        # The '1' key reaches here through the app-wide key filter, so guard
        # against re-entry while the confirmation dialog is already open.
        if self._confirming:
            return
        self._confirming = True
        try:
            answer = QMessageBox.question(
                self._window, "Re-arm robots",
                "Re-arm all robots? Pumps and valves will be allowed to run again.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        finally:
            self._confirming = False
        if answer != QMessageBox.StandardButton.Yes:
            return
        for robot in self._robots():
            try:
                robot.rearm()
            except Exception:
                logger.exception("rearm failed for %s", robot.robot_id)
        self._session_panel.emergency_rearm()
        self._stopped = False
        self._button.set_stopped(False)
        self._window.statusBar().showMessage("Robots re-armed.", 4000)
        logger.warning("Robots re-armed after emergency stop")

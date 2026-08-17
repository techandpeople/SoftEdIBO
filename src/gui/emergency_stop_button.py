"""Always-visible emergency-stop button for the main window.

A single red control that latches all robot actuation off - pumps and valves -
the instant it is pressed (or the panic key is hit). It owns only its own visual
state and emits intent as signals; the window decides what stopping and re-arming
actually do (see :class:`~src.gui.main_window.MainWindow`).

Two states:

* **armed** (default): a big red "EMERGENCY STOP" button. Pressing it - or the
  app-wide panic key - emits :attr:`stop_requested`.
* **stopped**: it turns into a "STOPPED - click to re-arm" button. Clicking it
  emits :attr:`rearm_requested` (the window confirms before acting). The panic
  key never re-arms, so a second panic press only re-stops.

Drop it into a menu-bar corner like :class:`~src.gui.help_mode.HelpButton`::

    self._estop_button = EmergencyStopButton(self)
    self.menubar.setCornerWidget(self._estop_button, Qt.TopLeftCorner)
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton, QWidget

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
_STOPPED_TEXT = "STOPPED - click to re-arm"

_HELP = (
    "Emergency stop. Immediately turns off every pump and closes every valve on "
    "all connected nodes, and freezes the running session. Also triggered by the "
    "'0' key anywhere in the app. Click again while stopped to re-arm (a "
    "confirmation is asked first)."
)


class EmergencyStopButton(QPushButton):
    """Red latch button that requests an app-wide emergency stop / re-arm."""

    stop_requested = Signal()
    rearm_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stopped = False
        self.setWhatsThis(_HELP)
        self.setToolTip(_HELP)
        self.clicked.connect(self._on_clicked)
        self._refresh()

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def set_stopped(self, stopped: bool) -> None:
        """Reflect the current latch state (called by the window once it has acted)."""
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

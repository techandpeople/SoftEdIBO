"""BaseDialog — a QDialog that the window manager can maximize and minimize.

A plain :class:`QDialog` is created with the ``Qt.Dialog`` window type, which on
most Linux window managers yields a title bar with only a close button: the
window stays freely resizable, but there is no maximize/minimize button. Every
dialog in the app subclasses this instead of :class:`QDialog` so the title bar
carries the usual maximize and minimize buttons.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QWidget


class BaseDialog(QDialog):
    """A :class:`QDialog` whose title bar exposes maximize and minimize buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )

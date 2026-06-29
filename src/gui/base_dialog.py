"""BaseDialog — a QDialog that the window manager can maximize and minimize.

A plain :class:`QDialog` is created with the ``Qt.Dialog`` window type. On GNOME
(mutter) the window manager *deliberately* omits the maximize/minimize buttons
for "dialog" windows and ignores ``WindowMaximizeButtonHint`` — so a normal
QDialog can never be maximized there, even though it stays freely resizable.

To get a title bar with working maximize/minimize buttons across window managers
(GNOME included, both native Wayland and XWayland), this declares the window as a
normal top-level ``Qt.Window`` instead of a dialog. Modality is unaffected:
dialogs shown with ``exec()`` still run their own modal event loop regardless of
the window type. Every dialog in the app subclasses this instead of
:class:`QDialog`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QWidget


class BaseDialog(QDialog):
    """A :class:`QDialog` that presents as a maximizable/minimizable window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Declare a normal top-level window (not Qt.Dialog) so mutter and other
        # window managers expose the maximize and minimize buttons.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )

"""BaseDialog — a QDialog tied to its parent window, with maximize/minimize hints.

Every secondary window in the app subclasses this instead of :class:`QDialog`.

A previous version declared these as a plain top-level ``Qt.Window`` (not a
dialog) so that GNOME/mutter would draw the maximize/minimize title-bar buttons,
which it deliberately omits for "dialog" windows. That backfired: for a
``Qt.Window`` the Qt xcb backend never writes the ``WM_TRANSIENT_FOR`` hint, so
the window manager treated each dialog as an *independent* top-level window —
decoupled from the main window, with its own task-bar entry. Opening one let the
main window fall behind it (and behind other windows), so the user had to hunt
for it.

Declaring the window as ``Qt.Dialog`` restores ``WM_TRANSIENT_FOR`` (the dialog
stays bound to and above the main window) and ``_NET_WM_STATE_SKIP_TASKBAR`` (no
stray task-bar entry), which is what actually keeps the main window reachable.
The maximize/minimize button hints are kept as a best effort — window managers
that honour them (most non-GNOME ones) still show the buttons; GNOME ignores
them, but the dialog stays freely resizable (drag the edges, or double-click the
title bar to maximize) and, crucially, no longer hides the main window. Modality
is unaffected: ``exec()`` runs its own modal loop regardless of the window type.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QWidget


class BaseDialog(QDialog):
    """A :class:`QDialog` bound to its parent window (transient), with max/min hints."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Keep the Qt.Dialog type so the WM sets WM_TRANSIENT_FOR / SKIP_TASKBAR
        # and the dialog stays tied to the main window; still request the
        # maximize/minimize buttons for window managers that honour them.
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )

"""Application icon.

Single source of truth for the window / taskbar icon so every top-level window
(main window, setup wizard, dialogs) shows the same artwork. The artwork lives
at the repo root as ``softedibo.png`` and is bundled next to the executable by
softedibo.spec, so ``Settings.BUNDLE`` resolves it both in development and in a
frozen build.

The Windows *file* icon is a separate thing - that comes from softedibo.ico,
baked into the exe resource at build time (see softedibo.spec).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

from src.config.settings import Settings

ICON_NAME = "softedibo.png"


def icon_path() -> Path:
    """Return the bundled icon file (may not exist in a partial checkout)."""
    return Settings.BUNDLE / ICON_NAME


def app_icon() -> QIcon:
    """Return the application icon, or a null QIcon if the file is missing."""
    path = icon_path()
    return QIcon(str(path)) if path.exists() else QIcon()


def apply_app_icon(app) -> None:
    """Set the icon on ``app`` so all its windows inherit it.

    A missing icon file is not worth failing a launch over, so this is a no-op
    when the artwork cannot be found.
    """
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

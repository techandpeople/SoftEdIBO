"""ActivityEditorDialog — the Activity Editor (Tools => Activity Editor…).

A thin frame around :class:`~src.gui.behavior_editor_panel.BehaviorEditorPanel`,
the Scratch-like block editor that compiles to declarative behaviour specs run
by ScriptedActivity. Behaviours authored here are the activities the session
list offers — there is no separate per-activity preset surface any more.

The panel is dropped into the layout authored in ``ui/activity_editor_dialog.ui``;
the dialog only adds the Close button and, when robots are connected, lets the
panel preview picked LED colours live on them (cleared again on close).
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from src.data.database import Database
from src.gui.behavior_editor_panel import BehaviorEditorPanel
from src.gui.base_dialog import BaseDialog
from src.gui.ui_activity_editor_dialog import Ui_ActivityEditorDialog

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.robots.base_robot import BaseRobot


class ActivityEditorDialog(BaseDialog, Ui_ActivityEditorDialog):
    """Author block-based behaviours; preview LED colours on connected robots."""

    def __init__(self, db: Database, parent: QWidget | None = None, *,
                 robots: "list[BaseRobot] | None" = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._db = db

        # The Visual Editor pulls in QtWebEngine, so build it as part of opening
        # this dialog (never during a session).
        self.visual_panel = BehaviorEditorPanel(db, self, robots=robots)
        self.visualLayout.addWidget(self.visual_panel)

        self.closeButton.clicked.connect(self.accept)
        self.finished.connect(lambda _result: self.visual_panel.clear_preview())

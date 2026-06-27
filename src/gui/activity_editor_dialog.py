"""ActivityEditorDialog — the unified Activity Editor (Tools => Activity Editor…).

Merges the two former tools into one dialog with two tabs:

- **Visual Editor** — :class:`~src.gui.behavior_editor_panel.BehaviorEditorPanel`,
  the Scratch-like block editor that compiles to declarative behaviour specs run
  by ScriptedActivity.
- **Presets** — :class:`~src.gui.activity_preset_panel.ActivityPresetPanel`,
  the per-activity parameter-preset manager.

Both panels are plain widgets dropped into the tab layouts authored in
``ui/activity_editor_dialog.ui``. The dialog only adds a shared Close button and
the cross-tab plumbing (which tab to open on, applying a chosen preset on close).
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QWidget

from src.activities.base_activity import BaseActivity
from src.data.database import Database
from src.data.models import ActivityPreset
from src.gui.activity_preset_panel import ActivityPresetPanel
from src.gui.behavior_editor_panel import BehaviorEditorPanel
from src.gui.ui_activity_editor_dialog import Ui_ActivityEditorDialog


class ActivityEditorDialog(QDialog, Ui_ActivityEditorDialog):
    """Author block-based behaviours and tune activity presets in one place."""

    def __init__(self, db: Database, parent: QWidget | None = None, *,
                 initial_tab: str = "visual",
                 initial_activity: BaseActivity | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._db = db

        self.preset_panel = ActivityPresetPanel(
            db, self.tab_presets, initial_activity=initial_activity)
        self.presetsLayout.addWidget(self.preset_panel)

        # The Visual Editor pulls in QtWebEngine, so build it last and only as
        # part of opening this dialog (never during a session).
        self.visual_panel = BehaviorEditorPanel(db, self.tab_visual)
        self.visualLayout.addWidget(self.visual_panel)

        self.tabs.setCurrentWidget(
            self.tab_presets if initial_tab == "presets" else self.tab_visual)
        self.closeButton.clicked.connect(self.accept)

    def selected_preset(self) -> ActivityPreset | None:
        """The preset currently selected on the Presets tab (apply-on-close)."""
        return self.preset_panel.selected_preset()

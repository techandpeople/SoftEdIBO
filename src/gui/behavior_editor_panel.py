"""BehaviorEditorPanel — block-based activity authoring (Visual Editor tab).

A Scratch-like block editor for authoring activity behaviours without writing
Python. The blocks are hosted by Blockly inside a ``QWebEngineView``; on Save
they are compiled to the declarative spec interpreted by
:class:`~src.activities.scripted_activity.ScriptedActivity` and stored in the
``declarative_activities`` table. The saved behaviour then appears in the
session activity list and runs **without** this editor — Blockly / QtWebEngine
are only ever imported here, never during a session.

This is a plain :class:`QWidget` so it can be embedded as the "Visual Editor"
tab of :class:`~src.gui.activity_editor_dialog.ActivityEditorDialog`. Python
drives the page through three JS globals (see ``blockly/editor.html``):
``getSpec()``, ``loadSpec(json)`` and ``newWorkspace()``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QMessageBox, QWidget

from src.activities.catalog import SpecError, validate_spec
from src.config.settings import Settings
from src.data.database import Database
from src.data.models import DeclarativeActivity
from src.gui.ui_behavior_editor_panel import Ui_BehaviorEditorPanel

logger = logging.getLogger(__name__)

_NEW_LABEL = "(New behaviour…)"
# Resolve from BUNDLE so it works both in the repo and inside a frozen bundle
# (where the blockly/ assets are shipped under _MEIPASS via softedibo.spec).
_EDITOR_HTML = Settings.BUNDLE / "src" / "gui" / "blockly" / "editor.html"


class BehaviorEditorPanel(QWidget, Ui_BehaviorEditorPanel):
    """Author and persist declarative behaviours via Blockly blocks."""

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._db = db
        self._records: list[DeclarativeActivity] = []
        self._ready = False

        # QtWebEngine is heavy — import it only now (the dialog hosting this
        # panel is itself lazy-imported from the Tools menu), so a session
        # never pays for it.
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self._web = QWebEngineView(self.webContainer)
        self.webLayout.addWidget(self._web)
        self._web.loadFinished.connect(self._on_load_finished)
        self._web.load(QUrl.fromLocalFile(str(_EDITOR_HTML)))

        self._set_buttons_enabled(False)
        self.newButton.clicked.connect(self._on_new)
        self.deleteButton.clicked.connect(self._on_delete)
        self.saveButton.clicked.connect(self._on_save)
        self.behaviorCombo.currentIndexChanged.connect(self._on_select)

        self._reload_combo(select_id=None)

    # ------------------------------------------------------------------
    # Page lifecycle
    # ------------------------------------------------------------------

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = bool(ok)
        self._set_buttons_enabled(ok)
        if not ok:
            self._status("Editor failed to load — see the editor message.")

    def _set_buttons_enabled(self, on: bool) -> None:
        for w in (self.newButton, self.deleteButton, self.saveButton,
                  self.behaviorCombo):
            w.setEnabled(on)

    # ------------------------------------------------------------------
    # Combo / selection
    # ------------------------------------------------------------------

    def _reload_combo(self, select_id: str | None) -> None:
        self._records = self._db.get_declarative_activities()
        self.behaviorCombo.blockSignals(True)
        self.behaviorCombo.clear()
        self.behaviorCombo.addItem(_NEW_LABEL, userData=None)
        for rec in self._records:
            self.behaviorCombo.addItem(f"{rec.name} [{rec.activity_id}]",
                                       userData=rec.activity_id)
        idx = 0
        if select_id is not None:
            found = self.behaviorCombo.findData(select_id)
            if found >= 0:
                idx = found
        self.behaviorCombo.setCurrentIndex(idx)
        self.behaviorCombo.blockSignals(False)

    def _current_record(self) -> DeclarativeActivity | None:
        activity_id = self.behaviorCombo.currentData()
        if activity_id is None:
            return None
        return next((r for r in self._records if r.activity_id == activity_id),
                    None)

    def _on_select(self) -> None:
        if not self._ready:
            return
        rec = self._current_record()
        if rec is None:
            self.nameEdit.clear()
            self._web.page().runJavaScript("newWorkspace()")
        else:
            self.nameEdit.setText(rec.name)
            payload = json.dumps(json.dumps(rec.spec))   # → a JS string literal
            self._web.page().runJavaScript(f"loadSpec({payload})")

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def _on_new(self) -> None:
        self.behaviorCombo.setCurrentIndex(0)   # fires _on_select → newWorkspace
        if self.behaviorCombo.currentIndex() == 0:
            self._on_select()
        self.nameEdit.setFocus()

    def _on_delete(self) -> None:
        rec = self._current_record()
        if rec is None:
            return
        if QMessageBox.question(
                self, "Delete behaviour",
                f"Delete '{rec.name}' [{rec.activity_id}]? This cannot be undone."
        ) != QMessageBox.StandardButton.Yes:
            return
        self._db.delete_declarative_activity(rec.activity_id)
        self._reload_combo(select_id=None)
        self._on_select()
        self._status(f"Deleted {rec.activity_id}.")

    def _on_save(self) -> None:
        name = self.nameEdit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required",
                                "Give the behaviour a name before saving.")
            return
        # Pull the compiled spec out of the page, then finish in the callback.
        self._web.page().runJavaScript("getSpec()", self._save_with_spec)

    def _save_with_spec(self, spec_json: str | None) -> None:
        if not spec_json:
            QMessageBox.warning(self, "Nothing to save",
                                "The editor returned no behaviour.")
            return
        try:
            spec = json.loads(spec_json)
            validate_spec(spec)
        except (json.JSONDecodeError, SpecError) as exc:
            QMessageBox.critical(
                self, "Invalid behaviour",
                f"The blocks don't form a valid behaviour:\n\n{exc}\n\n"
                "Every behaviour needs at least one phase, and each transition "
                "must point at a phase that exists.")
            return

        name = self.nameEdit.text().strip()
        rec = self._current_record()
        if rec is None:
            rec = DeclarativeActivity(
                activity_id=self._db.next_declarative_activity_id(),
                created_at=datetime.now())
        rec.name = name
        rec.description = "Authored in the block editor."
        rec.spec = spec
        self._db.save_declarative_activity(rec)
        self._reload_combo(select_id=rec.activity_id)
        self._status(f"Saved {rec.activity_id} — '{name}'.")

    def _status(self, text: str) -> None:
        self.statusLabel.setText(text)

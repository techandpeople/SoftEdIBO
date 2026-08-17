"""BehaviorEditorPanel - block-based activity authoring (Visual Editor tab).

A Scratch-like block editor for authoring activity behaviours without writing
Python. The blocks are hosted by Blockly inside a ``QWebEngineView``; on Save
they are compiled to the declarative spec interpreted by
:class:`~src.activities.scripted_activity.ScriptedActivity` and stored in the
``declarative_activities`` table. The saved behaviour then appears in the
session activity list and runs **without** this editor - Blockly / QtWebEngine
are only ever imported here, never during a session.

This is a plain :class:`QWidget` so it can be embedded as the "Visual Editor"
tab of :class:`~src.gui.activity_editor_dialog.ActivityEditorDialog`. Python
drives the page through three JS globals (see ``blockly/editor.html``):
``getSpec()``, ``loadSpec(json)`` and ``newWorkspace()``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QFileDialog, QMessageBox, QPushButton, QWidget)

from src.activities.activity_io import (
    ActivityFileError, deserialize_activity, serialize_activity)
from src.activities.catalog import SpecError, validate_spec
from src.config.settings import Settings
from src.data.database import Database
from src.data.models import DeclarativeActivity
from src.gui.ui_behavior_editor_panel import Ui_BehaviorEditorPanel

if TYPE_CHECKING:
    from src.robots.base_robot import BaseRobot

logger = logging.getLogger(__name__)

_NEW_LABEL = "(New behaviour...)"
_FILE_FILTER = "SoftEdIBO activity (*.json);;All files (*)"
_DEFAULT_DESCRIPTION = "Authored in the block editor."
_DEFAULT_COLOUR = "#8e44ad"
# Quick-pick swatches for the Colour helper (study purple/yellow first).
_PALETTE = (
    "#8e44ad", "#9b59b6", "#2980b9", "#3498db", "#1abc9c", "#16a085",
    "#27ae60", "#2ecc71", "#f1c40f", "#f39c12", "#e67e22", "#d35400",
    "#e74c3c", "#c0392b", "#ec407a", "#fd79a8", "#ffffff", "#bdc3c7",
    "#95a5a6", "#7f8c8d", "#34495e", "#2c3e50", "#5d4037", "#000000",
)
# Resolve from BUNDLE so it works both in the repo and inside a frozen bundle
# (where the blockly/ assets are shipped under _MEIPASS via softedibo.spec).
_EDITOR_HTML = Settings.BUNDLE / "src" / "gui" / "blockly" / "editor.html"


def _safe_filename(name: str) -> str:
    """Slugify a behaviour name into a safe default file stem."""
    stem = re.sub(r"[^\w\- ]+", "", name).strip()
    stem = re.sub(r"\s+", "_", stem)
    return stem or "activity"


class _LedPreview:
    """Pushes a colour onto the LED rings of all connected robots so the
    operator can preview a colour while authoring (driven by the Colour helper
    panel). Best-effort: it drives no chambers, only the LEDs, and only while
    the editor dialog is open."""

    def __init__(self, robots: "list[BaseRobot]") -> None:
        self._robots = robots
        self._on = False

    def preview(self, color: str) -> None:
        for ctrl in self._controllers():
            try:
                ctrl.set_led(color, pattern="solid", period_ms=0)
                self._on = True
            except Exception:   # noqa: BLE001 - preview is best-effort
                logger.exception("LED preview set_led failed")

    def clear(self) -> None:
        """Turn the rings off again - called when the editor closes so robots
        are not left lit on the last previewed colour."""
        if not self._on:
            return
        for ctrl in self._controllers():
            try:
                ctrl.set_led("#000000", pattern="off", period_ms=0)
            except Exception:   # noqa: BLE001
                logger.exception("LED preview clear failed")
        self._on = False

    def _controllers(self) -> list:
        """Unique LED-capable controllers across all connected robots."""
        seen: set[int] = set()
        out: list = []
        for robot in self._robots:
            for skin in getattr(robot, "skins", {}).values():
                ctrl = getattr(skin, "_ctrl", None)
                if (ctrl is not None and id(ctrl) not in seen
                        and hasattr(ctrl, "set_led")):
                    seen.add(id(ctrl))
                    out.append(ctrl)
        return out


class BehaviorEditorPanel(QWidget, Ui_BehaviorEditorPanel):
    """Author and persist declarative behaviours via Blockly blocks."""

    def __init__(self, db: Database, parent: QWidget | None = None, *,
                 robots: "list[BaseRobot] | None" = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._db = db
        self._records: list[DeclarativeActivity] = []
        self._ready = False
        # Description to attach on the next Save, set when a behaviour is
        # imported so its description survives the round-trip. Cleared whenever
        # the selection changes (New / pick another behaviour).
        self._pending_description: str | None = None

        # QtWebEngine is heavy - import it only now (the dialog hosting this
        # panel is itself lazy-imported from the Tools menu), so a session
        # never pays for it.
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self._web = QWebEngineView(self.webContainer)
        self.webLayout.addWidget(self._web)
        self._web.loadFinished.connect(self._on_load_finished)
        self._web.load(QUrl.fromLocalFile(str(_EDITOR_HTML)))

        # Colour helper side panel: pick a colour visually, copy its #tag to
        # paste into a colour block, and preview it live on connected robots.
        self._preview = _LedPreview(robots or [])
        self._current_colour = _DEFAULT_COLOUR
        self._build_swatches()
        self.pickColourButton.clicked.connect(self._on_pick_custom)
        self.copyHexButton.clicked.connect(self._on_copy_hex)
        self._set_colour(_DEFAULT_COLOUR, preview=False)

        self._set_buttons_enabled(False)
        self.newButton.clicked.connect(self._on_new)
        self.deleteButton.clicked.connect(self._on_delete)
        self.saveButton.clicked.connect(self._on_save)
        self.importButton.clicked.connect(self._on_import)
        self.exportButton.clicked.connect(self._on_export)
        self.behaviorCombo.currentIndexChanged.connect(self._on_select)

        self._reload_combo(select_id=None)

    # ------------------------------------------------------------------
    # Page lifecycle
    # ------------------------------------------------------------------

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = bool(ok)
        self._set_buttons_enabled(ok)
        if not ok:
            self._status("Editor failed to load - see the editor message.")

    def _set_buttons_enabled(self, on: bool) -> None:
        for w in (self.newButton, self.deleteButton, self.saveButton,
                  self.importButton, self.exportButton, self.behaviorCombo):
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
        self._pending_description = None
        rec = self._current_record()
        if rec is None:
            self.nameEdit.clear()
            self._web.page().runJavaScript("newWorkspace()")
        else:
            self.nameEdit.setText(rec.name)
            payload = json.dumps(json.dumps(rec.spec))   # -> a JS string literal
            self._web.page().runJavaScript(f"loadSpec({payload})")

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def _on_new(self) -> None:
        self.behaviorCombo.setCurrentIndex(0)   # fires _on_select -> newWorkspace
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
        description = self._description_for(rec)
        if rec is None:
            rec = DeclarativeActivity(
                activity_id=self._db.next_declarative_activity_id(),
                created_at=datetime.now())
        rec.name = name
        rec.description = description
        rec.spec = spec
        self._db.save_declarative_activity(rec)
        self._pending_description = None
        self._reload_combo(select_id=rec.activity_id)
        self._status(f"Saved {rec.activity_id} - '{name}'.")

    # ------------------------------------------------------------------
    # Import / export (portable .json files)
    # ------------------------------------------------------------------

    def _description_for(self, rec: DeclarativeActivity | None) -> str:
        """Description to persist/export: a pending (imported) one wins, else
        the loaded record's, else the editor default."""
        if self._pending_description is not None:
            return self._pending_description or _DEFAULT_DESCRIPTION
        return rec.description if rec is not None else _DEFAULT_DESCRIPTION

    def _on_export(self) -> None:
        if not self.nameEdit.text().strip():
            QMessageBox.warning(self, "Name required",
                                "Give the behaviour a name before exporting.")
            return
        # Pull the compiled spec out of the page, then finish in the callback.
        self._web.page().runJavaScript("getSpec()", self._export_with_spec)

    def _export_with_spec(self, spec_json: str | None) -> None:
        name = self.nameEdit.text().strip()
        if not spec_json:
            QMessageBox.warning(self, "Nothing to export",
                                "The editor returned no behaviour.")
            return
        try:
            spec = json.loads(spec_json)
            text = serialize_activity(
                name, self._description_for(self._current_record()), spec)
        except (json.JSONDecodeError, ActivityFileError, SpecError) as exc:
            QMessageBox.critical(
                self, "Can't export",
                f"This behaviour can't be exported yet:\n\n{exc}\n\n"
                "Every behaviour needs at least one phase, and each transition "
                "must point at a phase that exists.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export behaviour", f"{_safe_filename(name)}.json",
            _FILE_FILTER)
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed",
                                 f"Could not write the file:\n\n{exc}")
            return
        self._status(f"Exported '{name}' -> {Path(path).name}.")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import behaviour", "", _FILE_FILTER)
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
            name, description, spec = deserialize_activity(text)
        except (OSError, ActivityFileError) as exc:
            QMessageBox.critical(self, "Import failed",
                                 f"Could not import this file:\n\n{exc}")
            return
        # Land it as a new, unsaved behaviour for review - Save then creates a
        # fresh record (new DA id), so importing never clobbers an existing one.
        self.behaviorCombo.blockSignals(True)
        self.behaviorCombo.setCurrentIndex(0)          # "(New behaviour...)"
        self.behaviorCombo.blockSignals(False)
        self.nameEdit.setText(name or Path(path).stem)
        self._pending_description = description or _DEFAULT_DESCRIPTION
        payload = json.dumps(json.dumps(spec))         # -> a JS string literal
        self._web.page().runJavaScript(f"loadSpec({payload})")
        self._status(f"Imported '{name or Path(path).stem}' - "
                     "review and Save to keep it.")

    def _status(self, text: str) -> None:
        self.statusLabel.setText(text)

    # ------------------------------------------------------------------
    # Colour helper panel
    # ------------------------------------------------------------------

    def _build_swatches(self) -> None:
        """Lay out the quick-pick colour swatches in a grid."""
        cols = 6
        for i, hex_colour in enumerate(_PALETTE):
            btn = QPushButton()
            btn.setFixedSize(26, 26)
            btn.setToolTip(hex_colour)
            btn.setStyleSheet(
                f"background-color: {hex_colour}; "
                "border: 1px solid #888; border-radius: 4px;")
            btn.clicked.connect(
                lambda _checked=False, c=hex_colour: self._set_colour(c))
            self.swatchLayout.addWidget(btn, i // cols, i % cols)

    def _set_colour(self, hex_colour: str, *, preview: bool = True) -> None:
        """Make ``hex_colour`` the helper's current colour: update the preview
        swatch and tag field, and (optionally) light the connected robots."""
        self._current_colour = hex_colour
        self.hexEdit.setText(hex_colour)
        self.previewSwatch.setStyleSheet(
            f"background-color: {hex_colour}; border: 1px solid #888;")
        if preview:
            self._preview.preview(hex_colour)

    def _on_pick_custom(self) -> None:
        colour = QColorDialog.getColor(
            QColor(self._current_colour), self, "Pick a colour")
        if colour.isValid():
            self._set_colour(colour.name())

    def _on_copy_hex(self) -> None:
        QApplication.clipboard().setText(self._current_colour)
        self._status(f"Copied {self._current_colour} - "
                     "paste it into a block's colour field.")

    def clear_preview(self) -> None:
        """Turn off any LED colour left lit by the live preview. Call when the
        hosting dialog closes."""
        self._preview.clear()

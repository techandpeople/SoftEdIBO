"""ActivityProgressView - the running activity's progress on one robot card.

Shows each unit's (skin's) current phase and the readable progress lines the
activity reports via :meth:`BaseActivity.progress`. Knows nothing about any
particular activity. Layout: ``src/gui/ui/activity_progress_view.ui``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget

from src.activities.base_activity import ActivityProgress
from src.gui.ui_activity_progress_view import Ui_ActivityProgressView

_DONE_STYLE = "color: #16803c;"


class ActivityProgressView(QFrame, Ui_ActivityProgressView):
    """Plain-text progress readout, hidden when there is nothing to show."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._text = ""
        self.hide()

    def show_progress(self, progress: list[ActivityProgress]) -> None:
        """Render ``progress``; an empty list hides the view."""
        text = "\n".join(self._describe(item) for item in progress)
        if text != self._text:
            self._text = text
            self.body_label.setText(text)
            finished = bool(progress) and all(item.finished for item in progress)
            self.body_label.setStyleSheet(_DONE_STYLE if finished else "")
        self.setVisible(bool(progress))

    @staticmethod
    def _describe(item: ActivityProgress) -> str:
        head = f"{item.unit}: {item.state}" + (" (done)" if item.finished else "")
        return "\n".join([head, *(f"  - {line}" for line in item.lines if line)])

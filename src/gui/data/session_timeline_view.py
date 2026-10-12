"""SessionTimelineView - the timeline canvas with its zoom controls."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from src.data.timeline import Timeline
from src.gui.ui_session_timeline_view import Ui_SessionTimelineView

_ZOOM_STEP = 1.5


class SessionTimelineView(QWidget, Ui_SessionTimelineView):
    """Shows one session's :class:`Timeline`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self.fit_btn.clicked.connect(self.canvas.fit)
        self.zoom_in_btn.clicked.connect(lambda: self.canvas.zoom(_ZOOM_STEP))
        self.zoom_out_btn.clicked.connect(
            lambda: self.canvas.zoom(1.0 / _ZOOM_STEP))

    def show_timeline(self, timeline: Timeline | None) -> None:
        """Show ``timeline`` (an empty canvas when no session is selected)."""
        self.canvas.set_timeline(timeline or Timeline(0.0))

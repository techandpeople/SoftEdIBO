"""A small circular CPR-status light for the live robot monitor."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class CprLedIndicator(QWidget):
    """Circular UI LED showing whether a CPR activity is still listening or won.

    This is deliberately a *monitor* indicator: it mirrors the activity state
    in the desktop UI and does not replace the physical ``set_led`` commands in
    the behaviour.  It remains hidden outside CPR activities so ordinary robot
    sessions keep their existing layout.
    """

    _OFF = QColor("#b8b8b8")
    _LISTENING = QColor("#f39c12")
    _SUCCESS = QColor("#2ecc71")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = ""
        self.setFixedSize(82, 106)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setToolTip("CPR synchronization indicator")
        self.hide()

    def set_activity_state(self, state: str | None, *, visible: bool) -> None:
        """Set state from the CPR behaviour's per-skin state machine."""
        self.setVisible(visible)
        normalised = str(state or "").strip().lower()
        if normalised != self._state:
            self._state = normalised
            self.update()
        if visible:
            if normalised == "complete":
                self.setToolTip("CPR synchronized: success")
            else:
                self.setToolTip("CPR synchronization: waiting for group rounds")

    def _colour(self) -> QColor:
        if self._state in {"complete", "success", "done"}:
            return self._SUCCESS
        if self._state:
            return self._LISTENING
        return self._OFF

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().window())

        circle = self.rect().adjusted(13, 8, -13, -30)
        colour = self._colour()
        painter.setBrush(colour)
        painter.setPen(QPen(QColor("#303030"), 2))
        painter.drawEllipse(circle)

        # A small highlight makes the status read as a physical LED rather
        # than a flat colour swatch, even in the app's light theme.
        highlight = circle.adjusted(10, 8, -circle.width() // 2, -circle.height() // 2)
        painter.setBrush(QColor(255, 255, 255, 115))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(highlight)

        painter.setPen(QColor("#202020"))
        painter.drawText(self.rect().adjusted(0, 74, 0, 0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         "CPR LED")
        painter.end()

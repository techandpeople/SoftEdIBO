"""Reusable "?" help mode - Windows "What's This?", but better.

Drop a :class:`HelpButton` into any window (toolbar, menu-bar corner, layout).
When the user toggles it on, the whole window enters *help mode*:

* the cursor becomes the "?" what's-this cursor,
* every field that carries help text gets a soft accent highlight, and
* hovering any of them shows that help immediately in a popup (no tooltip
  delay), persistent until the pointer moves away.

Toggling the button again (or pressing Escape) leaves help mode. Interaction
with the underlying widgets is suppressed while help mode is active, so the user
can sweep the mouse around purely to discover what each control does.

The three classes split cleanly by responsibility:

* :class:`HelpProvider` - *model/policy*: where help text comes from and which
  widgets carry it. Default policy is ``whatsThis`` first, then ``toolTip``;
  subclass and override :meth:`HelpProvider.text_for` to source it elsewhere.
* :class:`_HelpOverlay` - *view*: renders highlights and shows the help popup.
  It depends only on the :class:`HelpProvider` abstraction, never on the button.
* :class:`HelpButton` - *controller*: owns the overlay and toggles help mode.

Help text being read from ``whatsThis``/``toolTip`` means it lives in the
``.ui`` files alongside the layout (see the project's "GUI must be .ui files"
rule), so every existing tooltip already works with no extra authoring.

Usage::

    from src.gui.help_mode import HelpButton

    help_btn = HelpButton(self)          # target defaults to the button's window
    self.menubar.setCornerWidget(help_btn, Qt.Corner.TopRightCorner)
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QToolButton, QWidget


class HelpProvider:
    """Resolves help text for widgets and finds which ones carry it.

    Encapsulates the help-sourcing policy so callers never reach into widget
    properties directly. The default policy prefers a widget's ``whatsThis``
    text and falls back to its ``toolTip``. Override :meth:`text_for` in a
    subclass to source help from a glossary, translations, etc.
    """

    def text_for(self, widget: QWidget) -> str:
        """Help string for *widget* (empty if it has none)."""
        return widget.whatsThis().strip() or widget.toolTip().strip()

    def has_help(self, widget: QWidget) -> bool:
        return bool(self.text_for(widget))

    def help_widgets(self, root: QWidget) -> list[QWidget]:
        """Visible descendants of *root* that carry help text.

        Restricted to widgets belonging to *root*'s own top-level window, so
        separate child windows (e.g. a floating panel parented to the main
        window) keep their help to themselves instead of bleeding into this one.
        """
        return [w for w in root.findChildren(QWidget)
                if w.window() is root and w.isVisible() and self.has_help(w)]


class _HelpOverlay(QWidget):
    """Transparent overlay that highlights help fields and shows their text.

    Lives as a child of the target top-level window, covering it entirely. It
    grabs mouse-move events (so interaction with the controls underneath is
    paused) and, on each move, asks its :class:`HelpProvider` which widget sits
    under the cursor and what its help text is. It owns no knowledge of what
    toggled it on - when the user dismisses it (Escape) it emits
    :attr:`dismissed` and lets the owner react.
    """

    #: Emitted when the user leaves help mode from inside the overlay (Escape).
    dismissed = Signal()

    HIGHLIGHT = QColor(0x29, 0x9E, 0xFF, 120)  # accent blue, translucent
    DIM = QColor(0, 0, 0, 40)                   # gentle dim of the rest

    BUBBLE_BG = QColor(0x20, 0x20, 0x20, 240)   # near-opaque dark tooltip
    BUBBLE_FG = QColor(0xF0, 0xF0, 0xF0)
    BUBBLE_MAX_WIDTH = 280
    BUBBLE_PAD = 8

    def __init__(self, target: QWidget, provider: HelpProvider) -> None:
        super().__init__(target)
        self._target = target
        self._provider = provider
        self._hover_pos: QPoint | None = None
        self._hover_text = ""
        self.setObjectName("_HelpOverlay")
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setGeometry(target.rect())
        target.installEventFilter(self)

    # -- lifecycle ------------------------------------------------------

    def show_over(self) -> None:
        self.setGeometry(self._target.rect())
        self.raise_()
        self.show()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt signature)
        # Observe the target so the overlay stays glued to it as it resizes.
        if obj is self._target and event.type() == QEvent.Type.Resize:
            self.setGeometry(self._target.rect())
        return False

    # -- geometry -------------------------------------------------------

    def _rect_of(self, widget: QWidget) -> QRect:
        """*widget*'s rectangle in this overlay's coordinates."""
        return QRect(widget.mapTo(self._target, QPoint(0, 0)), widget.size())

    def _help_widget_at(self, pos: QPoint) -> QWidget | None:
        """Innermost help-bearing widget at overlay-local *pos*.

        Hit-tests by geometry against the help widgets (not ``childAt``), so the
        overlay itself - which sits on top of everything - is never picked.
        """
        best: QWidget | None = None
        best_area = 0
        for widget in self._provider.help_widgets(self._target):
            rect = self._rect_of(widget)
            if not rect.contains(pos):
                continue
            area = rect.width() * rect.height()
            if best is None or area < best_area:  # smaller = more specific
                best, best_area = widget, area
        return best

    # -- painting -------------------------------------------------------

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.DIM)
        pen = QPen(self.HIGHLIGHT.darker(110))
        pen.setWidth(2)
        painter.setPen(pen)
        for widget in self._provider.help_widgets(self._target):
            rect = self._rect_of(widget).adjusted(1, 1, -1, -1)
            painter.fillRect(rect, self.HIGHLIGHT)
            painter.drawRoundedRect(rect, 4, 4)
        if self._hover_text and self._hover_pos is not None:
            self._paint_bubble(painter, self._hover_pos, self._hover_text)
        painter.end()

    def _paint_bubble(self, painter: QPainter, anchor: QPoint, text: str) -> None:
        """Draw the help text in a tooltip-like bubble near *anchor*.

        Painted directly onto the overlay (the top-most widget) so it can never
        be hidden behind the window the way a separate QToolTip popup can.
        """
        metrics = painter.fontMetrics()
        flags = Qt.TextFlag.TextWordWrap
        text_rect = metrics.boundingRect(
            QRect(0, 0, self.BUBBLE_MAX_WIDTH, 1000), flags, text)
        pad = self.BUBBLE_PAD
        w = text_rect.width() + 2 * pad
        h = text_rect.height() + 2 * pad
        # Prefer below-right of the cursor; flip to stay inside the overlay.
        x = anchor.x() + 16
        y = anchor.y() + 20
        if x + w > self.width() - 4:
            x = max(4, anchor.x() - w - 8)
        if y + h > self.height() - 4:
            y = max(4, anchor.y() - h - 8)
        bubble = QRect(x, y, w, h)
        painter.setPen(QPen(self.BUBBLE_BG.lighter(140)))
        painter.setBrush(self.BUBBLE_BG)
        painter.drawRoundedRect(bubble, 6, 6)
        painter.setPen(QPen(self.BUBBLE_FG))
        painter.drawText(bubble.adjusted(pad, pad, -pad, -pad), flags, text)

    # -- interaction ----------------------------------------------------

    def mouseMoveEvent(self, event):  # noqa: N802
        pos = event.position().toPoint()
        widget = self._help_widget_at(pos)
        text = self._provider.text_for(widget) if widget is not None else ""
        if text != self._hover_text or pos != self._hover_pos:
            self._hover_text = text
            self._hover_pos = pos
            self.update()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.dismissed.emit()
        else:
            super().keyPressEvent(event)


class HelpButton(QToolButton):
    """Checkable "?" button that toggles help mode on a target window.

    *target* is the window whose widgets get help; when omitted it resolves to
    the button's own top-level window the first time it is toggled (handy when
    the button is added before being parented into the final window). A custom
    :class:`HelpProvider` can be injected to change where help text comes from.
    """

    def __init__(self, target: QWidget | None = None, parent: QWidget | None = None,
                 provider: HelpProvider | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self._provider = provider or HelpProvider()
        self._overlay: _HelpOverlay | None = None
        self.setText("?")
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Help mode - hover any field to see what it does")
        self.setWhatsThis(
            'Toggle help mode. While on, the pointer becomes a "?" and '
            "hovering any highlighted control shows what it does."
        )
        self.toggled.connect(self._on_toggled)

    def _resolve_target(self) -> QWidget:
        # Always cover the whole top-level window. When promoted in a .ui,
        # pyside6-uic passes the form as the constructor's first arg (our
        # ``target``); .window() normalises any widget to its top-level.
        return (self._target or self).window()

    def _ensure_overlay(self, target: QWidget) -> _HelpOverlay:
        if self._overlay is None or self._overlay.parent() is not target:
            self._overlay = _HelpOverlay(target, self._provider)
            # The overlay drives its own dismissal; we just mirror it on the button.
            self._overlay.dismissed.connect(lambda: self.setChecked(False))
        return self._overlay

    def _on_toggled(self, checked: bool) -> None:
        if checked:
            overlay = self._ensure_overlay(self._resolve_target())
            overlay.show_over()
            overlay.setFocus()
        elif self._overlay is not None:
            self._overlay.hide()

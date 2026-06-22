"""Shared skin-outline geometry for the GUI (editor + activity view).

Single source for the clip/outline path of each skin shape, so the config
editor and the activity-time view draw identical outlines. Shapes match the
``skin_geometry`` registry: ``rect`` (no mask), ``round``, ``triangle`` and
``thymio`` (a 'D' rotated −90°: flat top, semicircular bottom).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath

SHAPES = ("rect", "round", "triangle", "thymio")

# Fraction of the Thymio 'D' height taken by the straight (bottom) part. Higher
# → the top belly starts higher up and is flatter (a shallower elliptical arc).
_THYMIO_STRAIGHT_FRAC = 0.62


def shape_path(shape: str, rect: QRectF) -> QPainterPath | None:
    """Outline path of ``shape`` inside ``rect``; None for ``rect`` (no mask)."""
    if shape == "round":
        path = QPainterPath()
        path.addEllipse(rect)
        return path
    if shape == "triangle":
        # Apex at top-centre, base along the bottom (Turtle corner skin).
        path = QPainterPath()
        path.moveTo(rect.center().x(), rect.top())
        path.lineTo(rect.right(), rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
        return path
    if shape == "thymio":
        # 'D' rotated -90°: semicircular bulge on TOP, flat bottom edge.
        path = QPainterPath()
        straight = rect.height() * _THYMIO_STRAIGHT_FRAC   # straight (bottom) part
        radius_h = rect.height() - straight    # height of the top belly (flatter when small)
        arc_y = rect.top() + radius_h          # where the straight sides meet the arc
        path.moveTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), arc_y)        # up the left side
        ell = QRectF(rect.left(), rect.top(), rect.width(), 2 * radius_h)
        path.arcTo(ell, 180, -180)             # left → top → right (bulge up)
        path.lineTo(rect.right(), rect.bottom())   # down the right side
        path.closeSubpath()                    # flat bottom edge
        return path
    return None


def aspect_box(size_mm: tuple[float, float] | None,
               width: int, height: int) -> tuple[int, int, int, int]:
    """Largest box of the skin's physical aspect ratio that fits ``width×height``,
    centred. Returns ``(x, y, w, h)``. Full area when ``size_mm`` is None."""
    if size_mm and size_mm[0] > 0 and size_mm[1] > 0:
        ar = size_mm[0] / size_mm[1]
        if width / height > ar:            # area wider than skin → fit height
            box_h = float(height)
            box_w = height * ar
        else:                              # fit width
            box_w = float(width)
            box_h = width / ar
        return (int((width - box_w) / 2), int((height - box_h) / 2),
                int(box_w), int(box_h))
    return 0, 0, width, height

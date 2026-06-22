"""Tests for shared skin-outline geometry (editor + activity view)."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF  # noqa: E402

from src.gui.skin_shapes import aspect_box, shape_path  # noqa: E402


def test_rect_has_no_mask():
    assert shape_path("rect", QRectF(0, 0, 100, 100)) is None


@pytest.mark.parametrize("shape", ["round", "triangle", "thymio"])
def test_non_rect_shapes_have_a_path(shape):
    path = shape_path(shape, QRectF(0, 0, 100, 100))
    assert path is not None
    assert not path.isEmpty()


def test_aspect_box_square_fills_when_square_area():
    # Square skin in a square area → full area.
    assert aspect_box((125, 125), 300, 300) == (0, 0, 300, 300)


def test_aspect_box_tall_rectangle_is_narrower():
    # 75×125 (taller than wide) in a square area → width reduced, full height.
    x, y, w, h = aspect_box((75.0, 125.0), 300, 300)
    assert h == 300
    assert w < 300
    assert w == pytest.approx(300 * 75 / 125, abs=1)
    assert x > 0 and y == 0


def test_aspect_box_wide_d_uses_full_width():
    # 160×80 (2:1) in a square area → full width, half height.
    x, y, w, h = aspect_box((160.0, 80.0), 320, 320)
    assert w == 320
    assert h == pytest.approx(160, abs=1)


def test_aspect_box_none_fills_area():
    assert aspect_box(None, 200, 150) == (0, 0, 200, 150)

"""Tests for the RGB/RGBW firmware-variant choice used by the OTA dialog."""

from src.core.led_variant import resolve_rgbw


def test_forced_choice_overrides_what_the_node_reports():
    # A node flashed with the wrong variant reports that wrong variant.
    assert resolve_rgbw(False, True, True) is False
    assert resolve_rgbw(True, False, False) is True


def test_automatic_follows_the_reported_variant():
    assert resolve_rgbw(None, True, False) is True
    assert resolve_rgbw(None, False, True) is False


def test_unknown_node_uses_the_fallback():
    assert resolve_rgbw(None, None, True) is True
    assert resolve_rgbw(None, None, False) is False

"""Tests for the RGB/RGBW firmware-variant choice used by the OTA dialog."""

from src.core.led_variant import resolve_rgbw


def test_forced_choice_overrides_what_the_node_reports():
    # A node flashed with the wrong variant reports that wrong variant.
    assert resolve_rgbw(False, True) is False
    assert resolve_rgbw(True, False) is True
    assert resolve_rgbw(True, None) is True


def test_automatic_follows_the_reported_variant():
    assert resolve_rgbw(None, True) is True
    assert resolve_rgbw(None, False) is False


def test_unknown_node_gets_the_rgb_build():
    assert resolve_rgbw(None, None) is False

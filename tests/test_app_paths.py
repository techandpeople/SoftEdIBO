"""Tests for the bundle-location marker written by frozen builds."""

from pathlib import Path

from src.app_paths import BUNDLE_MARKER, record_bundle_dir


def test_record_bundle_dir_writes_the_marker(tmp_path):
    state = tmp_path / "state"
    bundle = tmp_path / "app" / "_internal"
    record_bundle_dir(bundle, state)
    assert Path((state / BUNDLE_MARKER).read_text(encoding="utf-8")) == bundle


def test_record_bundle_dir_ignores_an_unwritable_state_dir(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    record_bundle_dir(tmp_path / "app", blocker)   # must not raise

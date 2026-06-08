"""Unit tests for NodeOTAUpdater image handling.

These cover the pure-logic ``_app_image`` helper, which decides what bytes to
stream to a node. The bundled node ``.bin`` files are *merged* flash images
(bootloader + partition table + app); the node's ``Update.h`` wants only the
app partition, so the updater must extract it. See se_ota.h / build-firmware.sh.
"""

from src.hardware.node_ota_updater import (
    NodeOTAUpdater,
    _APP_OFFSET,
    _ESP_APP_MAGIC,
)


def _make_updater():
    # _app_image only needs the constructed object; the gateway is never touched.
    return NodeOTAUpdater(gateway=object(), mac="AA:BB:CC:DD:EE:FF", firmware_path="x")


def test_bare_app_image_passes_through():
    app = bytes([_ESP_APP_MAGIC]) + b"\x05\x02\x20" + b"\xab" * 100
    assert _make_updater()._app_image(app) == app


def test_merged_image_extracts_app_partition():
    app = bytes([_ESP_APP_MAGIC]) + b"the-real-app-bytes" * 10
    # Merged layout: 0xFF gap, then app at the standard 0x10000 offset.
    merged = b"\xff" * _APP_OFFSET + app
    assert _make_updater()._app_image(merged) == app


def test_invalid_image_returns_empty():
    garbage = b"\xff" * (_APP_OFFSET + 16)  # no app magic anywhere expected
    assert _make_updater()._app_image(garbage) == b""

"""Regenerate softedibo.ico from softedibo.png (dev tool, run by hand).

The Windows exe icon must be a real multi-size .ico resource (PyInstaller only
converts a .png if Pillow is installed, which is not a build dependency), so
the .ico is committed alongside the artwork. Run this after changing
softedibo.png:

    python scripts/make_icon.py

Qt's own .ico writer emits a single-size BMP icon, so the container is assembled
here with one PNG-compressed entry per size - what Windows 10/11 expects.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QImage

# Explorer, taskbar, alt-tab and the 256px "extra large icons" view all pick a
# different entry; shipping them all avoids Windows' blurry on-the-fly scaling.
SIZES = (16, 24, 32, 48, 64, 128, 256)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "softedibo.png"
TARGET = ROOT / "softedibo.ico"


def _png_bytes(image: QImage, size: int) -> bytes:
    """Return ``image`` scaled to ``size`` x ``size`` as PNG bytes."""
    scaled = image.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    # QBuffer does not own the QByteArray - keep a reference alive while writing.
    store = QByteArray()
    buffer = QBuffer(store)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    # The stub types `format` as bytes, but PySide6 rejects b"PNG" at runtime
    # ("called with wrong argument values") and wants the str - trust runtime.
    scaled.save(buffer, "PNG")  # pyright: ignore[reportCallIssue, reportArgumentType]
    buffer.close()
    return bytes(store.data())


def build_ico(source: Path, target: Path) -> None:
    """Write a multi-size, PNG-compressed .ico built from ``source``."""
    image = QImage(str(source))
    if image.isNull():
        raise SystemExit(f"cannot read {source}")
    image = image.convertToFormat(QImage.Format.Format_ARGB32)

    entries = [(size, _png_bytes(image, size)) for size in SIZES]

    # ICONDIR: reserved, type 1 (icon), image count.
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)       # header + one 16-byte ICONDIRENTRY each
    for size, data in entries:
        dim = 0 if size >= 256 else size    # 0 means 256 in the ICO format
        # width, height, palette, reserved, planes, bpp, byte count, offset
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in entries:
        out += data

    target.write_bytes(bytes(out))
    print(f"wrote {target} ({len(out)} bytes, sizes {list(SIZES)})")


if __name__ == "__main__":
    # No QApplication needed: QImage scaling and the PNG writer are non-GUI.
    sys.exit(build_ico(SOURCE, TARGET))

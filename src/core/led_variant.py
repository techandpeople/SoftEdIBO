"""LED pixel-format variant (RGB vs RGBW) to flash onto a node.

The actuator firmware is built once per pixel format (see ``-DLED_RGBW`` in
``firmware/node_actuator/platformio.ini``). A node reports the format of the
build it is *running*, which is not necessarily the format of the LEDs wired to
it: a board flashed with the wrong variant keeps reporting that wrong variant.
So the reported value is only a default, and an explicit choice always wins.
"""

from __future__ import annotations


def resolve_rgbw(forced: bool | None, reported: bool | None) -> bool:
    """Return True to flash the RGBW build, False for the RGB one.

    ``forced`` is the user's explicit per-node choice (None = automatic) and
    ``reported`` what the node's running firmware announced (None = unknown).
    A node that is neither forced nor reporting gets the plain RGB build.
    """
    if forced is not None:
        return forced
    return bool(reported)

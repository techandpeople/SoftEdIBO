"""Halt-and-rearm: turn every actuator on one node off, then leave it usable.

Used when a bench tool (Test Actuators) hands a node back to the rest of the
app: pumps off, every valve closed, holds and manual overrides dropped - but
NOT left latched, so a session or another tool can drive the node right away.

The firmware ``stop`` does the work (emergencyStopAll: pumps off, valves
closed, chambers idle, holds aborted, manual overrides and test runs
cleared); ``resume`` only releases the latch afterwards. ``stop`` is sent a
few times because ESP-NOW is best-effort and ``stop`` is idempotent.
"""

from __future__ import annotations

from typing import Any, Callable

STOP_REPEATS = 3


def halt_and_rearm(send: Callable[[str], Any], repeats: int = STOP_REPEATS) -> None:
    """Send ``stop`` ``repeats`` times, then one ``resume``.

    Args:
        send: Sends one target-less node command by name (e.g. a bound
            ``ESP32Controller.send_command`` or a gateway send for one MAC).
        repeats: How many ``stop`` frames to send.
    """
    for _ in range(max(1, repeats)):
        send("stop")
    send("resume")

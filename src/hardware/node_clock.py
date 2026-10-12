"""Places a node's samples on the PC clock, without the transport jitter.

A touch node stamps each stream sample with its own ``millis()`` (``t``) and a
running sample counter (``n``). The PC only sees the frame after ESP-NOW, the
gateway and USB, a delay that varies from frame to frame, so stamping samples
on arrival blurs press intervals. The node's own clock has no such jitter, but
an unknown offset to the PC clock.

:class:`NodeClock` estimates that offset as the SMALLEST ``arrival - node``
difference seen recently (the frame that travelled fastest), then places every
sample at ``node time + offset``. The counter exposes dropped frames as gaps.

Pure Python; one instance per node stream.
"""

from __future__ import annotations

from collections import deque

# How many recent frames the offset is the minimum of. At 10-28 Hz this spans
# several seconds: long enough to contain a fast frame, short enough to follow
# the slow drift between the two crystal clocks.
DEFAULT_WINDOW = 200
# A node time this far behind the previous one means the node rebooted (its
# millis() restarted), not a late frame.
_REBOOT_BACKSTEP_MS = 1000.0


class NodeClock:
    """Maps node timestamps to PC monotonic time and counts dropped frames."""

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._offsets: deque[float] = deque(maxlen=max(1, int(window)))
        self._last_node_ms: float | None = None
        self._last_count: int | None = None
        self._lost = 0

    @property
    def frames_lost(self) -> int:
        """Stream frames that never arrived (gaps in the sample counter)."""
        return self._lost

    def reset(self) -> None:
        self._offsets.clear()
        self._last_node_ms = None
        self._last_count = None

    def observe(self, node_ms: float, arrival_ms: float,
                count: int | None = None) -> float:
        """Record one frame; return when its sample was taken, on the PC clock.

        ``node_ms`` is the node's stamp, ``arrival_ms`` the PC monotonic time
        the frame was received, ``count`` the node's sample counter."""
        node_ms = float(node_ms)
        arrival_ms = float(arrival_ms)
        if (self._last_node_ms is not None
                and node_ms < self._last_node_ms - _REBOOT_BACKSTEP_MS):
            self.reset()                     # node rebooted: new time base
        self._last_node_ms = node_ms
        self._count_gap(count)
        self._offsets.append(arrival_ms - node_ms)
        # A sample cannot have been taken after its frame arrived.
        return min(arrival_ms, node_ms + min(self._offsets))

    def _count_gap(self, count: int | None) -> None:
        if count is None:
            return
        count = int(count)
        if self._last_count is not None and count > self._last_count + 1:
            self._lost += count - self._last_count - 1
        self._last_count = count

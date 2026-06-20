"""Shared-pump fill-time scaling.

Time-based inflation uses a per-chamber **base** fill time, measured by the
calibrator with the chamber inflating *alone* (see
:mod:`src.hardware.fill_calibration`). On real hardware the pumps — or, on a
reservoir node, the shared tank that the pumps maintain — are shared by every
chamber on a node *per robot*. So when several chambers inflate at once each one
fills proportionally slower. The effective fill window therefore scales with the
number of concurrently-active chambers and (down to a floor) inversely with the
pump count::

    effective_ms = base_ms * fill_fraction * max(1.0, active_chambers / pumps)

The ``max(1.0, …)`` floor matters: the base time was measured with a single
chamber, so a single chamber (or any count up to the pump count, where each
chamber effectively gets its own pump) must reproduce the measured time and
never *less* — otherwise a lone chamber would under-inflate. Above the pump
count the chambers share flow and slow down.

The firmware keeps its own independent backstops — a hard 5 s fill ceiling and
the per-chamber HARD_MAX pressure cutoff — so this estimate only has to be
roughly right: it can never drive a chamber past its pressure limit.
"""

from __future__ import annotations

import time
from typing import Callable


def effective_fill_ms(base_ms: float, value_pct: float,
                      active_chambers: int, pump_count: int) -> int:
    """Effective time-based fill duration in ms (always >= 1).

    ``base_ms``: calibrated fill time for this chamber alone.
    ``value_pct``: requested fill, 0-100 % of the chamber max.
    ``active_chambers``: chambers inflating concurrently on the node (incl. this
    one, so >= 1).
    ``pump_count``: pressure pumps shared on the node (>= 1).
    """
    frac = max(0.0, min(100.0, float(value_pct))) / 100.0
    n = max(1, int(active_chambers))
    p = max(1, int(pump_count))
    load = max(1.0, n / p)
    return max(1, int(round(float(base_ms) * frac * load)))


class FillLoadTracker:
    """Tracks concurrently-inflating chambers on one node, to scale fill times.

    A chamber started with :meth:`note_inflate` counts as active until its fill
    window elapses; :meth:`active_count` prunes expired windows. One tracker is
    shared by every :class:`~src.hardware.skin.Skin` on the same node (it lives
    on the node's controller), so chambers across different skins on the same
    physical node correctly count against each other.

    ``clock`` returns seconds (monotonic); inject a fake one in tests.
    """

    def __init__(self, pump_count: int = 1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pump_count = max(1, int(pump_count))
        self._clock = clock
        self._until: dict[int, float] = {}   # slot -> monotonic end time (s)

    def _prune(self, now: float) -> None:
        for slot in [s for s, end in self._until.items() if end <= now]:
            del self._until[slot]

    def active_count(self) -> int:
        """Number of chambers whose fill window is still open."""
        now = self._clock()
        self._prune(now)
        return len(self._until)

    def note_inflate(self, slot: int, ms: float) -> None:
        """Record that ``slot`` is inflating for ``ms`` from now."""
        now = self._clock()
        self._prune(now)
        self._until[int(slot)] = now + max(0.0, float(ms)) / 1000.0

    def note_stop(self, slot: int) -> None:
        """Drop ``slot`` from the active set (deflate / hold / set lower)."""
        self._until.pop(int(slot), None)

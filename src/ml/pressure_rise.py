"""How much a skin's chamber pressure rose during a touch.

Squeezing an inflated skin compresses its chambers, so the pressure inside
goes up while the hand is on it - evidence the magnet sensors alone cannot
give. :class:`PressureRiseMeter` is told the chamber pressures when a touch
begins and again while it lasts, and reports the largest rise.

Only chambers the robot is NOT driving may be fed in: a chamber being
inflated rises by itself. Pure Python.
"""

from __future__ import annotations

import math


class PressureRiseMeter:
    """Tracks the largest chamber pressure rise since a touch began."""

    def __init__(self) -> None:
        self._start: dict[int, float] = {}
        self._peak: dict[int, float] = {}
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def begin(self, kpa_by_chamber: dict[int, float]) -> None:
        """Start measuring from these pressures (a touch just began)."""
        self._start = self._known(kpa_by_chamber)
        self._peak = dict(self._start)
        self._open = True

    def sample(self, kpa_by_chamber: dict[int, float]) -> None:
        """Feed the current pressures while the touch lasts. A chamber first
        seen now starts from this reading."""
        if not self._open:
            return
        for chamber, kpa in self._known(kpa_by_chamber).items():
            self._start.setdefault(chamber, kpa)
            self._peak[chamber] = max(kpa, self._peak.get(chamber, kpa))

    def end(self) -> float | None:
        """Stop measuring; return the largest rise in kPa (``None`` when no
        chamber pressure was available)."""
        self._open = False
        rises = [self._peak[c] - start for c, start in self._start.items()]
        self._start, self._peak = {}, {}
        return max(rises) if rises else None

    @staticmethod
    def _known(kpa_by_chamber: dict[int, float]) -> dict[int, float]:
        return {int(c): float(v) for c, v in kpa_by_chamber.items()
                if isinstance(v, (int, float)) and not math.isnan(float(v))}

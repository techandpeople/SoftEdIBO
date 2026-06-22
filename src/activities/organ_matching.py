"""OrganMatcher — decides whether a measured resistance means "cured".

Pure domain logic, no hardware or Qt: given the activity preset's parameters
(mode, target, tolerance, catalogue) and a total resistance reading, answer
``is_cured``. Extracted from ``OrganSwapActivity`` so the matching rules are
testable in isolation and reusable (e.g. by a GUI organ-catalogue tool).
"""

from __future__ import annotations

import math
from itertools import combinations

MODE_AGGREGATE = "aggregate"
MODE_PER_ORGAN = "per_organ"


class OrganMatcher:
    """Matches a total organ-network resistance against the cure condition.

    Args:
        mode: ``"aggregate"`` (compare total against ``target_ohm``) or
            ``"per_organ"`` (decompose against the catalogue via
            1/Rtot = Σ 1/Ri and require exactly the ``*_good`` organs).
        target_ohm: Expected total resistance when cured (aggregate mode).
        tolerance_ohm: Acceptable drift around the match in both modes.
        catalogue: ``{organ_id: resistance_ohm}`` of every organ the operator
            might plug in. IDs ending in ``_good`` are required; anything else
            is forbidden. Empty/no-good catalogues fall back to aggregate.
    """

    def __init__(self, mode: str, target_ohm: float, tolerance_ohm: float,
                 catalogue: dict[str, float] | None = None):
        self._mode = mode
        self._target = float(target_ohm)
        self._tolerance = float(tolerance_ohm)
        self._catalogue = dict(catalogue or {})

    @classmethod
    def from_params(cls, params: dict) -> "OrganMatcher":
        """Build from an OrganSwap preset's ``param_values`` dict."""
        return cls(
            mode=params.get("organ_readout_mode", MODE_AGGREGATE),
            target_ohm=params.get("cured_total_resistance_ohm", 0.0),
            tolerance_ohm=params.get("cured_tolerance_ohm", 0.0),
            catalogue=params.get("organ_catalogue") or {},
        )

    def is_cured(self, resistance_ohm: float) -> bool:
        """True when the reading satisfies the cure condition. ``inf`` (cover
        off / open circuit) is never cured."""
        if math.isinf(resistance_ohm):
            return False
        if self._mode == MODE_PER_ORGAN:
            return self._matches_per_organ(resistance_ohm)
        return self._matches_aggregate(resistance_ohm)

    # ------------------------------------------------------------------
    # Matching strategies
    # ------------------------------------------------------------------

    def _matches_aggregate(self, resistance_ohm: float) -> bool:
        return abs(resistance_ohm - self._target) <= self._tolerance

    def _matches_per_organ(self, resistance_ohm: float) -> bool:
        """Find the catalogue subset whose parallel resistance best matches
        the reading; cured only when that subset is exactly the good organs.
        Falls back to the aggregate check when the catalogue can't decide."""
        if not self._catalogue:
            return self._matches_aggregate(resistance_ohm)
        required = {k for k in self._catalogue if k.endswith("_good")}
        if not required:
            return self._matches_aggregate(resistance_ohm)
        best_subset: set[str] | None = None
        best_diff = float("inf")
        keys = list(self._catalogue.keys())
        for size in range(1, len(keys) + 1):
            for combo in combinations(keys, size):
                r_total = self.parallel_resistance(
                    [self._catalogue[k] for k in combo]
                )
                diff = abs(r_total - resistance_ohm)
                if diff < best_diff:
                    best_diff = diff
                    best_subset = set(combo)
        if best_subset is None or best_diff > self._tolerance:
            return False
        return best_subset == required

    @staticmethod
    def parallel_resistance(values: list[float]) -> float:
        """1 / Rtot = Σ 1 / Ri (parallel circuit). Ignores non-positive
        values; returns +inf for an empty (or all-zero) input."""
        inv_sum = sum(1.0 / v for v in values if v > 0)
        return 1.0 / inv_sum if inv_sum > 0 else float("inf")

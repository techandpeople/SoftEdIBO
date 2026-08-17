"""OrganResolver - decide each organ's status from one resistance reading.

Pure domain logic, no hardware or Qt. The Turtle's organs share a single
sensing circuit, so the firmware reports **one** total resistance for the whole
network (organs in parallel, cover in series - see ``docs/STUDY_PLAN.md``). To
colour each organ shape individually in the GUI we must infer, per organ,
whether the **good** organ, the **bad** organ, or **nothing** is plugged in.

Each organ is described by an :class:`OrganSpec` carrying the resistance of its
good and bad variants. :meth:`OrganResolver.resolve` enumerates every
combination of per-organ states (``good`` / ``bad`` / ``absent``), computes the
parallel resistance of the present ones, and picks the combination whose total
best matches the measured reading (within ``tolerance_ohm``). An open circuit
(``inf``, cover off) means every organ is absent.

**Limitation (honest).** The inference is only unambiguous when the per-organ
good/bad resistances are chosen so the parallel totals of different combinations
differ by more than ``tolerance_ohm``. Pick the physical resistor values with
that in mind.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product

from src.activities.organ_matching import OrganMatcher

# Per-organ verdicts.
GOOD = "good"
BAD = "bad"
ABSENT = "absent"


@dataclass(frozen=True)
class OrganSpec:
    """One pluggable organ: its id plus the resistance of each variant.

    ``good_ohm`` / ``bad_ohm`` are the resistances measured when the good /
    bad version of this organ is plugged in (with the cover closed). A
    non-positive value means that variant is not usable for matching.
    """
    id: str
    good_ohm: float
    bad_ohm: float


class OrganResolver:
    """Infers per-organ status from a single (parallel) resistance reading.

    Args:
        organs: The organs present on this circuit, in display order.
        tolerance_ohm: How far the best-matching combination's parallel total
            may sit from the measured reading before the result is rejected
            (every organ falls back to ``absent``).
    """

    def __init__(self, organs: list[OrganSpec], tolerance_ohm: float):
        self._organs = list(organs)
        self._tolerance = float(tolerance_ohm)

    @property
    def organs(self) -> list[OrganSpec]:
        return list(self._organs)

    def resolve(self, total_ohm: float) -> dict[str, str]:
        """Return ``{organ_id: GOOD|BAD|ABSENT}`` for the reading.

        ``inf`` (open circuit / cover off) -> every organ absent. Otherwise the
        best-matching combination is returned; if even the closest combination
        is further than ``tolerance_ohm`` from the reading, all organs are
        reported absent (we cannot trust the decomposition)."""
        if not self._organs:
            return {}
        if math.isinf(total_ohm):
            return {o.id: ABSENT for o in self._organs}

        best: dict[str, str] | None = None
        best_diff = float("inf")
        # Per organ, the candidate (state, resistance) options. A variant with a
        # non-positive resistance is dropped (can't be plugged / measured).
        options: list[list[tuple[str, float | None]]] = []
        for o in self._organs:
            opts: list[tuple[str, float | None]] = [(ABSENT, None)]
            if o.good_ohm > 0:
                opts.append((GOOD, o.good_ohm))
            if o.bad_ohm > 0:
                opts.append((BAD, o.bad_ohm))
            options.append(opts)

        for combo in product(*options):
            present = [r for _, r in combo if r is not None]
            total = OrganMatcher.parallel_resistance(present)
            # All-absent gives inf, which can't match a finite reading.
            diff = abs(total - total_ohm)
            if diff < best_diff:
                best_diff = diff
                best = {o.id: state for o, (state, _) in zip(self._organs, combo)}

        if best is None or best_diff > self._tolerance:
            return {o.id: ABSENT for o in self._organs}
        return best

    # ------------------------------------------------------------------
    # Verdict helpers
    # ------------------------------------------------------------------

    @staticmethod
    def good_count(verdicts: dict[str, str]) -> int:
        """Number of organs resolved as good."""
        return sum(1 for v in verdicts.values() if v == GOOD)

    @staticmethod
    def any_bad(verdicts: dict[str, str]) -> bool:
        """True if any organ is resolved as bad."""
        return any(v == BAD for v in verdicts.values())

    # ------------------------------------------------------------------
    # Construction from config
    # ------------------------------------------------------------------

    @classmethod
    def from_organ_configs(cls, organs: list[dict], tolerance_ohm: float
                           ) -> "OrganResolver":
        """Build from a skin's ``organs`` config list (see Skin.organs).

        Each entry needs ``id``, ``good_ohm`` and ``bad_ohm``; missing
        resistances default to 0 (that variant is then ignored for matching)."""
        specs = [
            OrganSpec(
                id=str(o.get("id", i)),
                good_ohm=float(o.get("good_ohm", 0) or 0),
                bad_ohm=float(o.get("bad_ohm", 0) or 0),
            )
            for i, o in enumerate(organs or [])
        ]
        return cls(specs, tolerance_ohm)

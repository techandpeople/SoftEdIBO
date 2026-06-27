"""kPa <-> percentage conversion over a per-chamber [min_kpa, max_kpa] range.

Python mirror of ``firmware/common/units.h`` so the PC reports the same
percentage the firmware would for a given kPa reading. Keeping one
implementation here means the live session view and the Test Actuators dialog
agree on how a measured kPa maps to a 0-100 % of the *configured* range,
instead of each trusting the firmware's own ``pressure`` field (computed
against whatever limits the node currently holds, which lag the PC config).

    0%   -> min_kpa
    100% -> max_kpa

With ``min_kpa = 0`` (the default) this is just ``kpa / max_kpa``. A chamber
fed by a vacuum reservoir uses a negative ``min_kpa`` so 0 % is its deepest
vacuum point.
"""

from __future__ import annotations


def kpa_to_pct(kpa: float, min_kpa: float, max_kpa: float) -> int:
    """Percent (0-100) of the ``[min_kpa, max_kpa]`` range a kPa reading sits at.

    Mirrors ``units::kpaToPct`` exactly (same round-half-up and clamping).
    Returns 0 for a non-positive span (a collapsed / unconfigured range).
    """
    span = max_kpa - min_kpa
    if span <= 0.0:
        return 0
    pct = int((kpa - min_kpa) * 100.0 / span + 0.5)
    return max(0, min(100, pct))


def pct_to_kpa(pct: int, min_kpa: float, max_kpa: float) -> float:
    """kPa a 0-100 % maps to over ``[min_kpa, max_kpa]`` (mirrors ``units::pctToKpa``)."""
    p = max(0, min(100, pct))
    return min_kpa + (max_kpa - min_kpa) * p / 100.0

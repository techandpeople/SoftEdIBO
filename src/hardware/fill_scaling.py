"""Shared-pump fill-time scaling.

Time-based inflation uses a per-chamber **base** fill time, measured by the
calibrator with the chamber inflating *alone* (see
:mod:`src.hardware.fill_calibration`). On real hardware the pumps are shared by
every chamber on a node *per robot*. So when several chambers inflate at once each one
fills proportionally slower. The effective fill window therefore scales with the
number of concurrently-active chambers and (down to a floor) inversely with the
pump count::

    effective_ms = base_ms * fill_fraction * max(1.0, active_chambers / pumps)

The ``max(1.0, ...)`` floor matters: the base time was measured with a single
chamber, so a single chamber (or any count up to the pump count, where each
chamber effectively gets its own pump) must reproduce the measured time and
never *less* - otherwise a lone chamber would under-inflate. Above the pump
count the chambers share flow and slow down.

The firmware keeps its own independent backstops - a hard 5 s fill ceiling and
the per-chamber HARD_MAX pressure cutoff - so this estimate only has to be
roughly right: it can never drive a chamber past its pressure limit.
"""

from __future__ import annotations

import time
from typing import Any, Callable


def scale_fill_ms(base_ms: float, active_chambers: int, pump_count: int) -> int:
    """Apply the shared-pump load factor to an already-computed fill time.

    ``base_ms`` is the open-valve time this chamber needs *alone* (e.g. the time
    a :class:`~src.hardware.fill_profile.FillProfile` says it takes to climb from
    the current level to the target). The result scales it up by the concurrent
    fill load, never below ``base_ms`` (the floor - see module docstring), and is
    always >= 1."""
    n = max(1, int(active_chambers))
    p = max(1, int(pump_count))
    load = max(1.0, n / p)
    return max(1, int(round(max(0.0, float(base_ms)) * load)))


# PWM duty (8-bit) the inflate/deflate pumps run at full speed, and a safe floor
# below which a diaphragm pump tends to stall and move no air. The floor keeps a
# requested "slow" fill from picking a duty so low the chamber never reaches its
# target (the pressure cutoff is still the real backstop). A future
# calibrate-at-several-dutys sweep would replace this rough linear model with a
# measured duty->flow curve per chamber.
#
# The diaphragm pumps barely move air until well over half duty - below ~190 the
# motor stalls and the fill never completes - so the floor sits there, not near 0.
FULL_DUTY = 255
MIN_PUMP_DUTY = 190

# The activity editor exposes pump strength as a friendly 1-5 "power" scale
# instead of a raw 0-255 PWM (see :func:`duty_for_power`): level 1 = the gentlest
# usable stroke, level 5 = full power.
POWER_MIN_LEVEL = 1
POWER_MAX_LEVEL = 5


def duty_for_power(level: int, min_duty: int = MIN_PUMP_DUTY,
                   max_duty: int = FULL_DUTY) -> int:
    """Map a 1-5 pump-power ``level`` onto a PWM duty over ``[min_duty, max_duty]``.

    Level 1 is the gentlest usable stroke - ``min_duty``, the calibrated stall
    floor below which a diaphragm pump moves no air - and level 5 is full power
    (``max_duty``, normally :data:`FULL_DUTY` = 255). Intermediate levels
    interpolate linearly, so the 1-5 dial spans exactly the useful part of the
    duty range. ``min_duty`` is configurable per skin type (a stiffer silicone
    stalls higher). Levels are clamped to 1-5 and the result to ``[1, FULL_DUTY]``
    so a mis-set floor can never produce a stalled pump. The firmware pressure
    cutoff remains the real backstop, so a rough duty is safe."""
    lo = max(1, min(FULL_DUTY, int(min_duty)))
    hi = max(lo, min(FULL_DUTY, int(max_duty)))
    lvl = max(POWER_MIN_LEVEL, min(POWER_MAX_LEVEL, int(level)))
    frac = (lvl - POWER_MIN_LEVEL) / (POWER_MAX_LEVEL - POWER_MIN_LEVEL)
    return int(round(lo + frac * (hi - lo)))


def duty_sweep(count: int = 4, top: int = FULL_DUTY,
               floor: int = MIN_PUMP_DUTY) -> tuple[int, ...]:
    """Geometrically-spaced PWM duties (fastest -> slowest) for the duty-curve sweep.

    The pump's air-flow vs duty response is roughly exponential - most of the
    useful range sits near full duty and it collapses toward the stall floor - so
    linear steps waste samples on the dead low end. Geometric spacing between
    ``top`` and ``floor`` puts the points closer together as duty drops, sampling
    the steep part of the curve where it matters. ``count`` distinct duties,
    de-duplicated and clamped to ``[1, FULL_DUTY]``."""
    top = max(1, min(FULL_DUTY, int(top)))
    floor = max(1, min(top, int(floor)))
    n = max(2, int(count))
    ratio = (floor / top) ** (1.0 / (n - 1))
    seen: dict[int, None] = {}
    for i in range(n):
        seen.setdefault(int(round(top * ratio ** i)), None)
    return tuple(seen)


def duty_for_period(natural_ms: float, period_ms: float,
                    min_duty: int = MIN_PUMP_DUTY) -> int:
    """Approximate the pump PWM duty (1-255) that stretches a fill to ``period_ms``.

    ``natural_ms`` is how long this actuation takes at full duty (e.g. from a
    measured :class:`~src.hardware.fill_profile.FillProfile`). Air moved per ms is
    assumed roughly proportional to duty, so to make a fill that naturally takes
    ``natural_ms`` last ``period_ms`` instead, run the pump at
    ``FULL_DUTY * natural_ms / period_ms`` - clamped to ``[min_duty, FULL_DUTY]``.
    A ``period_ms`` at or below ``natural_ms`` (can't go faster than full) or a
    non-positive ``natural_ms`` returns full duty. The mapping is deliberately
    approximate; the firmware's pressure cutoff decides the final level."""
    if period_ms <= 0 or natural_ms <= 0 or period_ms <= natural_ms:
        return FULL_DUTY
    duty = int(round(FULL_DUTY * float(natural_ms) / float(period_ms)))
    return max(int(min_duty), min(FULL_DUTY, duty))


class DutyModel:
    """Measured pump-duty -> fill-speed model - replaces the rough linear
    :func:`duty_for_period` when a chamber has a **duty-curve sweep**.

    The sweep records the full-fill time at several PWM duties (each swept from
    empty). A lower duty moves less air per ms, so the whole fill takes longer; the
    **slowdown factor** at duty ``d`` is ``T(d) / T_fastest`` (>= 1). To make a fill
    that naturally takes ``natural_ms`` at full duty last ``period_ms`` instead,
    pick the duty whose slowdown factor ~= ``period_ms / natural_ms``.

    Samples are ``(duty, full_time_ms)``. Times are forced monotone non-increasing
    over ascending duty (higher duty is never slower - sweep noise can invert two
    neighbours), and the factor lookup interpolates only within the measured duty
    range, clamping outside it. Pure / Qt-free, so it unit-tests trivially. The
    firmware pressure cutoff remains the real backstop, so a rough duty is safe.
    """

    __slots__ = ("_pts",)

    def __init__(self, samples: Any) -> None:
        # (duty, factor) sorted by ascending duty, factor non-increasing, factor>=1.
        self._pts: list[tuple[int, float]] = self._normalise(samples)

    @staticmethod
    def _normalise(samples: Any) -> list[tuple[int, float]]:
        by_duty: dict[int, float] = {}
        for item in samples or []:
            try:
                d = int(item[0])
                t = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 1 <= d <= FULL_DUTY and t > 0:
                by_duty[d] = t                       # last write per duty wins
        ordered = sorted(by_duty.items())            # ascending duty
        if len(ordered) < 2:
            return []
        # Force time non-increasing as duty rises, so the slowdown factor is a clean
        # monotone function of duty (invertible for the period->duty lookup).
        times: list[float] = []
        running = float("inf")
        for _, t in ordered:
            running = min(running, t)
            times.append(running)
        t_fast = times[-1]                           # highest duty = fastest = ref
        return [(d, ordered_t / t_fast)
                for (d, _), ordered_t in zip(ordered, times)]

    @classmethod
    def from_list(cls, raw: Any) -> "DutyModel | None":
        """Build from the stored ``[[duty, full_time_ms], ...]`` form, or ``None``
        when absent/degenerate."""
        model = cls(raw if isinstance(raw, (list, tuple)) else [])
        return model if not model.is_empty else None

    @property
    def is_empty(self) -> bool:
        return len(self._pts) < 2

    def duty_for_period(self, natural_ms: float, period_ms: float) -> int:
        """Duty (1-255) whose measured slowdown stretches ``natural_ms`` to
        ``period_ms``. Full duty when no stretch is needed or the model is empty."""
        if self.is_empty or period_ms <= 0 or natural_ms <= 0 or period_ms <= natural_ms:
            return FULL_DUTY
        ratio = float(period_ms) / float(natural_ms)
        pts = self._pts                              # ascending duty; factor descending
        if ratio >= pts[0][1]:
            return pts[0][0]                         # slower than slowest measured duty
        if ratio <= pts[-1][1]:
            return pts[-1][0]                        # at/above fastest -> full-ish
        for i in range(len(pts) - 1):
            d_lo, f_lo = pts[i]                      # lower duty, higher factor
            d_hi, f_hi = pts[i + 1]                  # higher duty, lower factor
            if f_hi <= ratio <= f_lo:
                if f_lo == f_hi:
                    return d_lo
                frac = (f_lo - ratio) / (f_lo - f_hi)
                return int(round(d_lo + frac * (d_hi - d_lo)))
        return pts[-1][0]


def effective_fill_ms(base_ms: float, value_pct: float,
                      active_chambers: int, pump_count: int) -> int:
    """Effective time-based fill duration in ms (always >= 1), assuming a
    **linear** fill (legacy scalar ``fill_time_ms`` path).

    ``base_ms``: calibrated full fill time for this chamber alone.
    ``value_pct``: requested fill, 0-100 % of the chamber max.
    ``active_chambers``: chambers inflating concurrently on the node (incl. this
    one, so >= 1).
    ``pump_count``: pressure pumps shared on the node (>= 1).

    Prefer a :class:`~src.hardware.fill_profile.FillProfile` + :func:`scale_fill_ms`
    where a measured curve exists; this linear form is the fallback."""
    frac = max(0.0, min(100.0, float(value_pct))) / 100.0
    return scale_fill_ms(float(base_ms) * frac, active_chambers, pump_count)


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

    def active_slots(self) -> set[int]:
        """Slots whose fill window is still open - the live co-active set.

        Used to look up a chamber's fill curve measured under exactly this
        concurrent set (see :mod:`src.hardware.fill_calibration` combinations);
        falls back to :func:`scale_fill_ms` when no such curve exists."""
        now = self._clock()
        self._prune(now)
        return set(self._until)

    def note_inflate(self, slot: int, ms: float) -> None:
        """Record that ``slot`` is inflating for ``ms`` from now."""
        now = self._clock()
        self._prune(now)
        self._until[int(slot)] = now + max(0.0, float(ms)) / 1000.0

    def note_stop(self, slot: int) -> None:
        """Drop ``slot`` from the active set (deflate / hold / set lower)."""
        self._until.pop(int(slot), None)

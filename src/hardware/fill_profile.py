"""Calibrated time→pressure fill curve for one chamber.

A chamber does not fill linearly: pressure climbs fast at first and then creeps
asymptotically toward the chamber max. A single ``fill_time_ms`` (time to reach
full) therefore can't say how long to inflate to a *partial* target — assuming
linearity over-/under-shoots. :class:`FillProfile` stores the measured curve as a
list of ``(cumulative_ms, pct_of_max)`` samples, taken from the ambient (empty)
state up toward full, and interpolates between them.

Used on both sides:
  * calibration (:mod:`src.hardware.fill_calibration`) builds one from the
    discrete-step sweep and stores it on the chamber as ``fill_profile``;
  * runtime (:mod:`src.hardware.skin`) reads it back to convert an inflate
    target into an open-valve time, so the firmware never has to close the loop
    on the laggy pressure sensor.

Pure / Qt-free / no clock, so it unit-tests trivially.
"""

from __future__ import annotations

from typing import Any, Sequence


class FillProfile:
    """A monotone ``(ms, pct)`` fill curve from ambient toward the chamber max.

    Points are normalised on construction: sorted by time, pressures clamped to
    0–100 and forced monotone non-decreasing (sensor noise can make a later
    reading dip), an ``(0, 0)`` ambient anchor prepended, and duplicate
    timestamps collapsed. An empty/degenerate input yields an empty profile
    (:attr:`is_empty`)."""

    __slots__ = ("_pts",)

    def __init__(self, points: Sequence[tuple[float, float]]) -> None:
        self._pts: list[tuple[float, float]] = self._normalise(points)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        clean: list[tuple[float, float]] = []
        for ms, pct in points:
            try:
                ms_f = float(ms)
                pct_f = float(pct)
            except (TypeError, ValueError):
                continue
            if ms_f < 0:
                continue
            clean.append((ms_f, max(0.0, min(100.0, pct_f))))
        if not clean:
            return []
        clean.sort(key=lambda p: p[0])
        # Collapse duplicate timestamps (keep the highest pressure seen at that
        # time) and force pressure monotone non-decreasing over time.
        out: list[tuple[float, float]] = []
        running = 0.0
        for ms_f, pct_f in clean:
            pct_f = max(running, pct_f)
            running = pct_f
            if out and out[-1][0] == ms_f:
                out[-1] = (ms_f, pct_f)
            else:
                out.append((ms_f, pct_f))
        # Anchor at ambient: time 0 == 0 %.
        if out[0][0] > 0.0:
            out.insert(0, (0.0, 0.0))
        elif out[0][1] != 0.0:
            out[0] = (0.0, 0.0)
        return out

    @classmethod
    def from_list(cls, raw: Any) -> "FillProfile | None":
        """Build from the stored ``[[ms, pct], ...]`` JSON form, or ``None`` if
        absent/empty/malformed."""
        if not raw or not isinstance(raw, (list, tuple)):
            return None
        pts: list[tuple[float, float]] = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pts.append((item[0], item[1]))
        prof = cls(pts)
        return prof if not prof.is_empty else None

    @classmethod
    def linear(cls, fill_time_ms: float | None) -> "FillProfile | None":
        """Back-compat curve for a legacy scalar ``fill_time_ms``: a straight
        line from ambient to full. Reproduces the old linear assumption."""
        if not fill_time_ms or float(fill_time_ms) <= 0:
            return None
        return cls([(0.0, 0.0), (float(fill_time_ms), 100.0)])

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        # A lone ambient anchor carries no measured rise.
        return len(self._pts) < 2

    @property
    def points(self) -> list[tuple[float, float]]:
        return list(self._pts)

    @property
    def full_time_ms(self) -> float:
        """Time of the last (highest-pressure) sample — the measured fill time."""
        return self._pts[-1][0] if self._pts else 0.0

    @property
    def top_pct(self) -> float:
        """Highest pressure the sweep reached (may be < 100 if it timed out)."""
        return self._pts[-1][1] if self._pts else 0.0

    def time_for_pct(self, pct: float) -> float:
        """Open-valve time (ms) to reach ``pct`` % of max from ambient.

        Linearly interpolates the curve. Clamped: at or below 0 % → 0 ms; at or
        above the highest measured pressure → the full measured time (we never
        extrapolate past what was measured)."""
        if self.is_empty:
            return 0.0
        target = max(0.0, min(float(pct), self._pts[-1][1]))
        if target <= 0.0:
            return 0.0
        prev_t, prev_p = self._pts[0]
        for t, p in self._pts[1:]:
            if p >= target:
                if p == prev_p:          # flat segment — reached at its start
                    return prev_t
                frac = (target - prev_p) / (p - prev_p)
                return prev_t + frac * (t - prev_t)
            prev_t, prev_p = t, p
        return self._pts[-1][0]

    def to_list(self) -> list[list[float]]:
        """Serialise to the stored JSON form: ``[[ms, pct], ...]`` (ms int,
        pct rounded to 1 decimal)."""
        return [[int(round(ms)), round(pct, 1)] for ms, pct in self._pts]

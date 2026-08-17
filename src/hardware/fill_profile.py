"""Calibrated time->pressure fill curve for one chamber.

A chamber does not fill linearly: pressure climbs fast at first and then creeps
asymptotically toward the chamber max. A single ``fill_time_ms`` (time to reach
full) therefore can't say how long to inflate to a *partial* target - assuming
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
    0-100 and forced monotone non-decreasing (sensor noise can make a later
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
        """Time of the last (highest-pressure) sample - the measured fill time."""
        return self._pts[-1][0] if self._pts else 0.0

    @property
    def top_pct(self) -> float:
        """Highest pressure the sweep reached (may be < 100 if it timed out)."""
        return self._pts[-1][1] if self._pts else 0.0

    def time_for_pct(self, pct: float) -> float:
        """Open-valve time (ms) to reach ``pct`` % of max from ambient.

        Linearly interpolates the curve. Clamped: at or below 0 % -> 0 ms; at or
        above the highest measured pressure -> the full measured time (we never
        extrapolate past what was measured)."""
        if self.is_empty:
            return 0.0
        target = max(0.0, min(float(pct), self._pts[-1][1]))
        if target <= 0.0:
            return 0.0
        prev_t, prev_p = self._pts[0]
        for t, p in self._pts[1:]:
            if p >= target:
                if p == prev_p:          # flat segment - reached at its start
                    return prev_t
                frac = (target - prev_p) / (p - prev_p)
                return prev_t + frac * (t - prev_t)
            prev_t, prev_p = t, p
        return self._pts[-1][0]

    def to_list(self) -> list[list[float]]:
        """Serialise to the stored JSON form: ``[[ms, pct], ...]`` (ms int,
        pct rounded to 1 decimal)."""
        return [[int(round(ms)), round(pct, 1)] for ms, pct in self._pts]


# Hard ceiling for a single deflate window (mirrors the firmware
# fill_control.h MAX_DEFLATE_MS): the vacuum pump is active and the gauge may be
# blind below its floor, so any extrapolated open-loop time is capped here.
MAX_DEFLATE_MS = 5000.0


class DeflateProfile:
    """A monotone falling ``(ms, pct)`` deflate curve, from full toward the floor.

    Mirror of :class:`FillProfile` for the vacuum side: the calibration sweep
    holds the deflate valve open from a full chamber and timestamps the falling
    pressure until it plateaus at the lowest level the gauge can see (the sensor
    floor - ambient on today's 0..100 kPa sensors, real vacuum once the -40 kPa
    parts arrive; nothing here assumes where the floor is, it is **measured**).

    Points are normalised on construction: sorted by time, pressures clamped to
    0-100 and forced monotone non-increasing (sensor noise can make a later
    reading bounce up), a ``(0, start)`` anchor prepended, and duplicate
    timestamps collapsed. An empty/degenerate input yields an empty profile.

    Used to time a deflate the gauge cannot supervise: the time to move between
    two levels reads off the curve; a target *below* the measured floor
    extrapolates the tail slope, capped at :data:`MAX_DEFLATE_MS`.
    """

    __slots__ = ("_pts",)

    def __init__(self, points: Sequence[tuple[float, float]]) -> None:
        self._pts: list[tuple[float, float]] = self._normalise(points)

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
        # Collapse duplicate timestamps (keep the LOWEST pressure seen at that
        # time) and force pressure monotone non-increasing over time.
        out: list[tuple[float, float]] = []
        running = 100.0
        for ms_f, pct_f in clean:
            pct_f = min(running, pct_f)
            running = pct_f
            if out and out[-1][0] == ms_f:
                out[-1] = (ms_f, pct_f)
            else:
                out.append((ms_f, pct_f))
        # Anchor at the start level: time 0 == the level the sweep started from.
        if out[0][0] > 0.0:
            out.insert(0, (0.0, out[0][1]))
        return out

    @classmethod
    def from_list(cls, raw: Any) -> "DeflateProfile | None":
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

    @property
    def is_empty(self) -> bool:
        # A curve needs an actual fall to be usable.
        return len(self._pts) < 2 or self._pts[0][1] <= self._pts[-1][1]

    @property
    def points(self) -> list[tuple[float, float]]:
        return list(self._pts)

    @property
    def start_pct(self) -> float:
        """Level the sweep started from (typically ~full)."""
        return self._pts[0][1] if self._pts else 0.0

    @property
    def floor_pct(self) -> float:
        """Lowest level the sweep reached - the gauge's measured floor."""
        return self._pts[-1][1] if self._pts else 0.0

    @property
    def full_time_ms(self) -> float:
        """Time of the last sample - the measured full-deflate time."""
        return self._pts[-1][0] if self._pts else 0.0

    def _first_time_at(self, pct: float) -> float:
        """Earliest time the falling curve reaches ``pct`` (clamped to the span)."""
        p = max(self.floor_pct, min(float(pct), self.start_pct))
        prev_t, prev_p = self._pts[0]
        for t, q in self._pts[1:]:
            if q <= p:
                if prev_p == q:          # flat segment - reached at its start
                    return prev_t
                frac = (prev_p - p) / (prev_p - q)
                return prev_t + frac * (t - prev_t)
            prev_t, prev_p = t, q
        return self._pts[-1][0]

    def _last_time_at(self, pct: float) -> float:
        """Latest time the curve is still AT/ABOVE ``pct`` - where the fall
        below it begins. Differs from :meth:`_first_time_at` on flat segments
        (notably the ``(0, start)`` anchor): timing a deflate "from" a level
        must not count time spent sitting at that level."""
        p = max(self.floor_pct, min(float(pct), self.start_pct))
        last = 0
        for i, (_, q) in enumerate(self._pts):
            if q >= p:
                last = i
        t1, q1 = self._pts[last]
        if last == len(self._pts) - 1 or q1 == p:
            return t1
        t2, q2 = self._pts[last + 1]     # q1 > p > q2
        return t1 + (q1 - p) / (q1 - q2) * (t2 - t1)

    def time_from_to(self, hi_pct: float, lo_pct: float) -> float:
        """Open-valve time (ms) to fall from ``hi_pct`` to ``lo_pct``.

        Uses the tight bounds - from where the curve leaves ``hi_pct`` to where
        it first reaches ``lo_pct``. Both levels are clamped to the measured
        span (use :meth:`extrapolate_ms` for a target below the floor). A
        non-falling request returns 0."""
        if self.is_empty:
            return 0.0
        return max(0.0, self._first_time_at(lo_pct) - self._last_time_at(hi_pct))

    def extrapolate_ms(self, from_pct: float, target_pct: float,
                       cap_ms: float = MAX_DEFLATE_MS) -> float:
        """Open-valve time to fall from ``from_pct`` to a target possibly BELOW
        the measured floor (the gauge can't see past its floor, so the tail
        slope of the curve is extended linearly). Always capped at ``cap_ms`` -
        the sensor-independent safety bound for an unsupervised vacuum pull."""
        if self.is_empty:
            return 0.0
        target = float(target_pct)
        if target >= self.floor_pct:
            return min(float(cap_ms), self.time_from_to(from_pct, target))
        ms = self.time_from_to(from_pct, self.floor_pct)
        # Tail slope (ms per pct) from the last genuinely-falling segment.
        slope = self._tail_slope_ms_per_pct()
        if slope > 0:
            ms += (self.floor_pct - target) * slope
        else:
            ms = float(cap_ms)            # flat tail - no basis, use the cap
        return min(float(cap_ms), ms)

    def _tail_slope_ms_per_pct(self) -> float:
        """ms per % of the last falling segment (0 when the tail is flat)."""
        prev_t, prev_p = self._pts[-1]
        for t, p in reversed(self._pts[:-1]):
            if p > prev_p:
                return (prev_t - t) / (p - prev_p)
            prev_t, prev_p = t, p
        return 0.0

    def to_list(self) -> list[list[float]]:
        """Serialise to the stored JSON form: ``[[ms, pct], ...]``."""
        return [[int(round(ms)), round(pct, 1)] for ms, pct in self._pts]

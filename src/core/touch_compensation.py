"""Pressure-informed touch compensation — pure, Qt-free, testable.

A magnet sits in the silicone above each MLX90393 touch sensor; inflating a
chamber deforms the silicone and shifts the magnet, so chamber actuation can
masquerade as a touch (see :mod:`src.core.touch_coupling` and
``docs/TOUCH_COUPLING.md``). The geometry is irregular — one chamber can move
*two* sensors by different amounts — so the fix is a full ``[chamber x sensor]``
coupling model, not a 1:1 map.

The model is composed of three collaborators:

* :class:`ChamberCoupling` — one chamber's measured effect on every sensor, as a
  level→offset curve. A curve with several measured levels is interpolated
  piecewise-linearly (through the origin); the legacy single-point matrix is a
  one-point curve, which reproduces the old ``delta x level/ref_pct`` scaling
  exactly. Points may also carry per-sensor **3-axis** deltas (µT vectors, from
  the ``MAG_VECTOR`` firmware), enabling vector compensation.
* :class:`TransitionGuard` — marks a chamber "unsettled" for a window after its
  level moves. The calibration only measures steady state, so during transitions
  (pump running, level readings lagging) the expected offset is unreliable; the
  compensator hardens the threshold of the affected sensors for that window.
* :class:`TouchCompensator` — subtracts the expected actuation offset from each
  sensor's live reading and recomputes the active-sensor set from the residual.
  Scalar mode subtracts magnitudes; when both the message and the calibration
  carry 3-axis data it subtracts **vectors** and takes the residual's norm,
  which is physically correct (touch and actuation displace the magnet along
  different axes, so magnitudes alone under- or over-compensate). The activation
  threshold grows with the size of the correction (``margin_frac``) so large —
  hence less certain — corrections are trusted less. An optional
  ``suppress_pct`` implements the last-resort "ignore a sensor while its chamber
  is (near) fully actuated".

Scalar mode works in magnitude (uT) space, matching the PC detection path
(QuadrantDetector consumes raw ``mag`` in uT); the coupling must be measured in
the same units.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from math import sqrt
from typing import Any, Mapping

# Default activation threshold (uT) used to rederive the active-sensor set from
# the compensated magnitudes. Matches the firmware default act (act_threshold_ut
# 300), so enabling compensation with a zero matrix preserves the previous
# activation behaviour.
DEFAULT_THRESHOLD_UT = 300.0

# A chamber must couple a sensor by at least this many uT (at its strongest
# measured level) for the ``suppress_pct`` fallback to blank that sensor — so a
# faintly-coupled sensor is not killed just because some far chamber is inflated.
DEFAULT_SUPPRESS_COUPLING_UT = 50.0

# Transition-guard defaults: a chamber whose level moved by more than
# ``DEFAULT_GUARD_LEVEL_EPS`` (%) stays "unsettled" for ``DEFAULT_GUARD_MS``.
# 800 ms mirrors the calibration's steady-state window (touch_coupling.SETTLE_MS).
DEFAULT_GUARD_MS = 800.0
DEFAULT_GUARD_LEVEL_EPS = 3.0

Vec = list[float]           # one sensor's [dx, dy, dz] in uT
_ZERO_VEC: Vec = [0.0, 0.0, 0.0]


def _norm3(v: Vec) -> float:
    return sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _fit_row(row: Any, n: int) -> list[float]:
    """Pad/truncate a per-sensor scalar row to exactly ``n`` values."""
    row = list(row or [])
    return [float(row[s]) if s < len(row) else 0.0 for s in range(n)]


def _fit_vecs(vecs: Any, n: int) -> list[Vec] | None:
    """Pad/truncate a per-sensor list of 3-vectors to ``n`` entries, or None."""
    if not isinstance(vecs, (list, tuple)):
        return None
    out: list[Vec] = []
    for s in range(n):
        v = vecs[s] if s < len(vecs) else None
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            out.append([float(v[0]), float(v[1]), float(v[2])])
        else:
            out.append(list(_ZERO_VEC))
    return out


@dataclass(frozen=True)
class CouplingPoint:
    """One measured level of a chamber's coupling: per-sensor scalar offsets
    (uT) and, when calibrated with 3-axis data, per-sensor offset vectors."""
    level_pct: float
    mag: list[float]
    vec: list[Vec] | None = None


class ChamberCoupling:
    """One chamber's level→offset curve over every sensor.

    ``points`` are (level %, per-sensor offsets) samples from the calibration
    sweep. Offsets between points are interpolated linearly, with an implicit
    origin at level 0; above the top point the curve is extended proportionally
    (``top x level/top_level``), which for a single point reproduces the legacy
    ``delta x level/ref_pct`` scaling exactly.
    """

    def __init__(self, points: list[CouplingPoint], sensor_count: int) -> None:
        self.sensor_count = max(0, int(sensor_count))
        by_level: dict[float, CouplingPoint] = {}
        for p in points:
            level = float(p.level_pct)
            if level <= 0.0:
                continue
            by_level[level] = CouplingPoint(
                level, _fit_row(p.mag, self.sensor_count),
                _fit_vecs(p.vec, self.sensor_count))
        self._points = [by_level[lv] for lv in sorted(by_level)]
        self.has_vec = bool(self._points) and all(
            p.vec is not None for p in self._points)

    @classmethod
    def from_row(cls, row: list[float], sensor_count: int,
                 ref_pct: float = 100.0) -> "ChamberCoupling":
        """Legacy single-point curve: ``row`` measured at ``ref_pct``."""
        return cls([CouplingPoint(ref_pct or 100.0, list(row))], sensor_count)

    @property
    def points(self) -> list[CouplingPoint]:
        return list(self._points)

    @property
    def is_zero(self) -> bool:
        return not any(any(p.mag) for p in self._points)

    def max_abs(self, sensor: int) -> float:
        """Largest |offset| (uT) this chamber was measured to put on ``sensor``
        at any level — the worst-case error bound used by the transition guard
        and the suppression fallback."""
        return max((abs(p.mag[sensor]) for p in self._points), default=0.0)

    def offset_at(self, level: float) -> tuple[list[float], list[Vec] | None]:
        """Expected (scalar offsets, vector offsets) at ``level`` % inflation.

        Vector offsets are ``None`` unless every calibration point carries them.
        """
        zeros = [0.0] * self.sensor_count
        zero_vecs = ([list(_ZERO_VEC) for _ in range(self.sensor_count)]
                     if self.has_vec else None)
        if level <= 0.0 or not self._points:
            return zeros, zero_vecs

        prev_level, prev_mag, prev_vec = 0.0, zeros, zero_vecs
        for p in self._points:
            if level <= p.level_pct:
                t = (level - prev_level) / (p.level_pct - prev_level)
                mag = [prev_mag[s] + (p.mag[s] - prev_mag[s]) * t
                       for s in range(self.sensor_count)]
                vec = None
                if self.has_vec:
                    vec = [[prev_vec[s][i] + (p.vec[s][i] - prev_vec[s][i]) * t
                            for i in range(3)]
                           for s in range(self.sensor_count)]
                return mag, vec
            prev_level, prev_mag, prev_vec = p.level_pct, p.mag, p.vec

        top = self._points[-1]
        scale = level / top.level_pct
        mag = [v * scale for v in top.mag]
        vec = ([[c * scale for c in top.vec[s]]
                for s in range(self.sensor_count)] if self.has_vec else None)
        return mag, vec


class TransitionGuard:
    """Tracks per-chamber level changes; a chamber whose level moved by more
    than ``level_eps`` stays *unsettled* for ``settle_ms``.

    The calibration only measures steady state, so while a chamber transitions
    (pump running, status/telemetry lagging the true pressure) the expected
    offset is unreliable — the compensator hardens the affected sensors'
    thresholds for that window, mirroring the settle window the calibration
    itself uses. The reference level only re-anchors when it trips, so a slow
    creep still triggers once it accumulates past ``level_eps``.
    """

    def __init__(self, settle_ms: float = DEFAULT_GUARD_MS,
                 level_eps: float = DEFAULT_GUARD_LEVEL_EPS) -> None:
        self.settle_ms = float(settle_ms)
        self.level_eps = float(level_eps)
        self._ref: dict[int, float] = {}
        self._until: dict[int, float] = {}

    def update(self, levels: Mapping[int, float], now_ms: float) -> set[int]:
        """Feed the current levels; returns the set of unsettled chambers."""
        for chamber, level in levels.items():
            ref = self._ref.get(chamber)
            if ref is None:
                self._ref[chamber] = float(level)
                continue
            if abs(float(level) - ref) > self.level_eps:
                self._ref[chamber] = float(level)
                self._until[chamber] = now_ms + self.settle_ms
        return {c for c, until in self._until.items() if now_ms < until}


class TouchCompensator:
    """Removes the expected per-sensor actuation offset from a magnet reading.

    ``couplings`` maps a chamber id (the node slot, matching the chamber level
    keys fed to :meth:`compensate`) to its :class:`ChamberCoupling` curve — or,
    for the legacy config, to a plain per-sensor offset row measured at
    ``ref_pct``. Missing sensors/chambers count as 0.

    ``margin_frac`` raises a sensor's activation threshold by that fraction of
    the correction applied to it, so large corrections (where calibration error
    is proportionally larger) cannot flip a sensor active on their own. ``guard``
    hardens sensors coupled to a chamber whose level just changed (worst-case
    boost: that chamber's strongest measured offset on the sensor).
    """

    def __init__(
        self,
        couplings: Mapping[int, "ChamberCoupling | list[float]"],
        *,
        sensor_count: int,
        ref_pct: float = 100.0,
        threshold_ut: float = DEFAULT_THRESHOLD_UT,
        margin_frac: float = 0.0,
        guard: TransitionGuard | None = None,
        suppress_pct: float | None = None,
        suppress_coupling_ut: float = DEFAULT_SUPPRESS_COUPLING_UT,
    ) -> None:
        self.sensor_count = max(0, int(sensor_count))
        self._couplings: dict[int, ChamberCoupling] = {}
        for chamber, cup in couplings.items():
            if not isinstance(cup, ChamberCoupling):
                cup = ChamberCoupling.from_row(
                    cup, self.sensor_count, ref_pct=ref_pct)
            self._couplings[int(chamber)] = cup
        self.threshold_ut = float(threshold_ut)
        self.margin_frac = max(0.0, float(margin_frac))
        self.guard = guard
        self.suppress_pct = None if suppress_pct is None else float(suppress_pct)
        self.suppress_coupling_ut = float(suppress_coupling_ut)
        self.has_vector = bool(self._couplings) and all(
            c.has_vec for c in self._couplings.values())

    @property
    def is_empty(self) -> bool:
        """True when there is no measured coupling (compensation is a no-op)."""
        return all(c.is_zero for c in self._couplings.values())

    def _expected(self, levels: Mapping[int, float]
                  ) -> tuple[list[float], list[Vec] | None]:
        """Per-sensor expected (scalar, vector) offsets for the given levels."""
        mag = [0.0] * self.sensor_count
        vec = ([[0.0, 0.0, 0.0] for _ in range(self.sensor_count)]
               if self.has_vector else None)
        for chamber, cup in self._couplings.items():
            level = max(0.0, float(levels.get(chamber, 0.0)))
            if level <= 0.0:
                continue
            c_mag, c_vec = cup.offset_at(level)
            for s in range(self.sensor_count):
                mag[s] += c_mag[s]
                if vec is not None and c_vec is not None:
                    vec[s][0] += c_vec[s][0]
                    vec[s][1] += c_vec[s][1]
                    vec[s][2] += c_vec[s][2]
        return mag, vec

    def expected_offset(self, levels: Mapping[int, float]) -> list[float]:
        """Per-sensor expected scalar offset (uT) for the given ``levels`` (%)."""
        return self._expected(levels)[0]

    def _suppressed(self, levels: Mapping[int, float]) -> set[int]:
        """Sensors to blank entirely because a strongly-coupled chamber is at or
        above ``suppress_pct`` (the opt-in 'ignore while actuated' fallback)."""
        if self.suppress_pct is None:
            return set()
        out: set[int] = set()
        for chamber, cup in self._couplings.items():
            if float(levels.get(chamber, 0.0)) < self.suppress_pct:
                continue
            for s in range(self.sensor_count):
                if cup.max_abs(s) >= self.suppress_coupling_ut:
                    out.add(s)
        return out

    def _guard_boost(self, levels: Mapping[int, float],
                     now_ms: float) -> list[float]:
        """Per-sensor extra threshold while coupled chambers are unsettled."""
        boost = [0.0] * self.sensor_count
        if self.guard is None:
            return boost
        for chamber in self.guard.update(levels, now_ms):
            cup = self._couplings.get(chamber)
            if cup is None:
                continue
            for s in range(self.sensor_count):
                boost[s] += cup.max_abs(s)
        return boost

    def compensate(self, mag: list[float], levels: Mapping[int, float], *,
                   vec: list[Any] | None = None,
                   now_ms: float | None = None
                   ) -> tuple[list[float], list[int]]:
        """Return ``(compensated_mag, active_sensors)`` for one reading.

        ``compensated_mag`` is the residual after removing the expected
        actuation offset — vectorially when both ``vec`` (the message's 3-axis
        deltas) and a vector calibration are available, else scalar (clamped at
        0). ``active_sensors`` are the indices whose residual reaches the
        effective threshold (base + margin + transition-guard boost), minus any
        suppressed sensor."""
        if now_ms is None:
            now_ms = time.monotonic() * 1000.0
        off_mag, off_vec = self._expected(levels)
        boost = self._guard_boost(levels, now_ms)
        suppressed = self._suppressed(levels)
        use_vec = off_vec is not None and isinstance(vec, (list, tuple))

        comp: list[float] = []
        act: list[int] = []
        for s in range(self.sensor_count):
            if s in suppressed:
                comp.append(0.0)
                continue
            v = vec[s] if use_vec and s < len(vec) else None
            residual, applied = self._residual(s, mag, v, off_mag, off_vec)
            comp.append(residual)
            if residual >= (self.threshold_ut
                            + self.margin_frac * applied + boost[s]):
                act.append(s)
        return comp, act

    @staticmethod
    def _residual(s: int, mag: list[float], v: Any, off_mag: list[float],
                  off_vec: list[Vec] | None) -> tuple[float, float]:
        """One sensor's (residual, applied-offset size) — vectorial when the
        message sample ``v`` carries 3-axis data, scalar otherwise."""
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return (_norm3([float(v[0]) - off_vec[s][0],
                            float(v[1]) - off_vec[s][1],
                            float(v[2]) - off_vec[s][2]]),
                    _norm3(off_vec[s]))
        raw = float(mag[s]) if s < len(mag) else 0.0
        return max(0.0, raw - off_mag[s]), abs(off_mag[s])

    def apply(self, data: Mapping[str, Any], levels: Mapping[int, float], *,
              now_ms: float | None = None) -> dict[str, Any]:
        """Return a shallow copy of a ``type:"magnet"`` message with ``mag`` and
        ``act`` replaced by their compensated values (other fields — including
        the raw ``vec`` — preserved).

        When there is no coupling and no suppression, the message is returned
        unchanged so the stream is bit-for-bit identical to raw."""
        out = dict(data)
        mag = data.get("mag")
        if not isinstance(mag, (list, tuple)):
            return out
        if self.is_empty and self.suppress_pct is None:
            return out
        raw_vec = data.get("vec")
        comp, act = self.compensate(
            [float(v) for v in mag], levels,
            vec=raw_vec if isinstance(raw_vec, (list, tuple)) else None,
            now_ms=now_ms)
        out["mag"] = comp
        out["act"] = act
        out["compensated"] = True
        return out


# ---------------------------------------------------------------------------
# Config (de)serialisation — the stored ``touch.coupling`` / ``touch.compensation``
# ---------------------------------------------------------------------------

def coupling_to_config(deltas: Mapping[int, list[float]], sensor_count: int,
                       ref_pct: float = 100.0,
                       curves: Mapping[int, list[dict[str, Any]]] | None = None,
                       ) -> dict[str, Any]:
    """Build the stored ``touch.coupling`` dict from a measured coupling.

    ``deltas`` is the per-chamber offset row at ``ref_pct`` — kept so configs
    stay readable by older code. ``curves`` optionally adds the full multi-level
    measurement: per chamber, a list of ``{"pct", "mag", ["vec"]}`` points.
    Slots are stored as strings (JSON/YAML object keys); magnitudes rounded."""
    cfg: dict[str, Any] = {
        "unit": "uT",
        "sensor_count": int(sensor_count),
        "ref_pct": round(float(ref_pct), 1),
        "deltas": {str(int(c)): [round(float(v), 2) for v in row]
                   for c, row in deltas.items()},
    }
    if curves:
        out_curves: dict[str, list[dict[str, Any]]] = {}
        for chamber, points in curves.items():
            rows = []
            for p in points:
                row: dict[str, Any] = {
                    "pct": round(float(p["pct"]), 1),
                    "mag": [round(float(v), 2) for v in p["mag"]],
                }
                if p.get("vec") is not None:
                    row["vec"] = [[round(float(c), 1) for c in v]
                                  for v in p["vec"]]
                rows.append(row)
            out_curves[str(int(chamber))] = rows
        cfg["curves"] = out_curves
    return cfg


def _coupling_from_config(coupling: Mapping[str, Any],
                          sensor_count: int) -> dict[int, ChamberCoupling]:
    """Parse stored coupling into per-chamber curves (multi-level ``curves``
    preferred, legacy single-point ``deltas`` otherwise)."""
    ref_pct = float(coupling.get("ref_pct", 100.0))
    out: dict[int, ChamberCoupling] = {}
    for slot, rows in (coupling.get("curves") or {}).items():
        try:
            points = [CouplingPoint(float(r["pct"]),
                                    [float(v) for v in r["mag"]],
                                    r.get("vec"))
                      for r in rows]
            out[int(slot)] = ChamberCoupling(points, sensor_count)
        except (TypeError, ValueError, KeyError):
            continue
    for slot, row in (coupling.get("deltas") or {}).items():
        try:
            if int(slot) not in out:
                out[int(slot)] = ChamberCoupling.from_row(
                    [float(v) for v in row], sensor_count, ref_pct=ref_pct)
        except (TypeError, ValueError):
            continue
    return out


def compensator_from_config(touch: Mapping[str, Any] | None) -> TouchCompensator | None:
    """Build a :class:`TouchCompensator` from a skin's ``touch`` config, or
    ``None`` when compensation is absent or disabled.

    Reads ``touch.coupling`` (the matrix/curves) and the optional
    ``touch.compensation`` tuning block (``enabled``, ``threshold_ut``,
    ``margin_frac``, ``guard_ms``, ``guard_level_eps``, ``suppress_pct``).
    Absent tuning keys default to the pre-upgrade behaviour (no margin, no
    guard), so stored configs keep working unchanged."""
    touch = touch or {}
    coupling = touch.get("coupling")
    tuning = touch.get("compensation") or {}
    if not coupling or not tuning.get("enabled", False):
        return None
    sensor_count = int(coupling.get("sensor_count")
                       or touch.get("sensor_count", 0)
                       or max((len(r) for r in
                               (coupling.get("deltas") or {}).values()),
                              default=0))
    couplings = _coupling_from_config(coupling, sensor_count)
    if not couplings:
        return None
    guard_ms = float(tuning.get("guard_ms", 0.0) or 0.0)
    guard = TransitionGuard(
        settle_ms=guard_ms,
        level_eps=float(tuning.get("guard_level_eps", DEFAULT_GUARD_LEVEL_EPS)),
    ) if guard_ms > 0.0 else None
    suppress = tuning.get("suppress_pct")
    return TouchCompensator(
        couplings,
        sensor_count=sensor_count,
        threshold_ut=float(tuning.get("threshold_ut", DEFAULT_THRESHOLD_UT)),
        margin_frac=float(tuning.get("margin_frac", 0.0)),
        guard=guard,
        suppress_pct=None if suppress is None else float(suppress),
    )

"""Pressure-informed touch compensation — pure, Qt-free, testable.

A magnet sits in the silicone above each MLX90393 touch sensor; inflating a
chamber deforms the silicone and shifts the magnet, so chamber actuation can
masquerade as a touch (see :mod:`src.core.touch_coupling` and
``docs/TOUCH_COUPLING.md``). The geometry is irregular — one chamber can move
*two* sensors by different amounts, and two chambers inflated together deform the
silicone **non-additively** — so the fix is a full N-dimensional model measured
over combinations of chambers, not a per-chamber sum.

The model is composed of three collaborators:

* :class:`GridCompensation` — the measured actuation offset as an N-dimensional
  lookup table over chamber levels. The calibration sweep visits every chamber
  *subset* over a per-member level grid, which is exactly the full Cartesian grid
  over each chamber's axis ``[0, g1, .., 100]`` (a state with a member at 0 is a
  lower-order subset). At runtime the expected offset for arbitrary live levels
  is the **multilinear interpolation** of the surrounding measured corners —
  correct for co-inflation, and reducing to the old per-chamber curve when only
  one chamber is up. Corners may carry per-sensor **3-axis** deltas (µT vectors,
  from the ``MAG_VECTOR`` firmware), enabling vector compensation.
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
from itertools import product
from math import sqrt
from typing import Any, Mapping, Sequence

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

# Level-bin width (%) used to snap a measured state's per-member level to a grid
# index — must match the sweep/analysis bin (touch_coupling.BIN_PCT).
DEFAULT_BIN_PCT = 10.0

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
class CouplingState:
    """One measured operating point fed to :class:`GridCompensation`: the set of
    co-inflated chambers, each member's level (%), and the per-sensor offset (uT,
    already rest-subtracted) it produced — with optional 3-axis vectors."""
    chambers: frozenset[int]
    levels: dict[int, float]
    mag: list[float]
    vec: list[Vec] | None = None


class GridCompensation:
    """Measured actuation offset as an N-dimensional multilinear lookup.

    Each :class:`CouplingState` is a *corner* of the grid, keyed by its members'
    (chamber, level-bin). A query at arbitrary live levels brackets each involved
    chamber between its surrounding grid levels (with an implicit origin at 0)
    and blends the ``2**k`` surrounding corners by the multilinear weights — the
    exact interpolation for a regular grid, and a plain per-chamber curve when
    only one chamber is up. An unmeasured higher-order corner (an incomplete
    sweep) degrades gracefully to the additive sum of its members' single-chamber
    corners rather than vanishing.
    """

    def __init__(self, states: Sequence[Any], sensor_count: int, *,
                 bin_pct: float = DEFAULT_BIN_PCT) -> None:
        self.sensor_count = max(0, int(sensor_count))
        self.bin_pct = float(bin_pct) or DEFAULT_BIN_PCT
        self.has_vec = bool(states) and all(
            getattr(st, "vec", None) is not None for st in states)
        self._corners: dict[frozenset, tuple[list[float], list[Vec] | None]] = {}
        self._chamber_max: dict[int, list[float]] = {}
        axes: dict[int, dict[int, list[float]]] = {}   # chamber -> bin -> [sum, n]
        for st in states:
            self._ingest(st, axes)
        # Per-chamber axis: sorted (level, bin), prefixed with the origin.
        self._axes: dict[int, list[tuple[float, int]]] = {
            c: sorted([(0.0, 0)] + [(ssum / n, b) for b, (ssum, n) in bins.items()])
            for c, bins in axes.items()}

    def _ingest(self, st: Any, axes: dict[int, dict[int, list[float]]]) -> None:
        """Fold one measured state into the corner table, per-chamber axes and
        worst-case per-chamber offsets."""
        chambers = frozenset(int(c) for c in st.chambers)
        mag = _fit_row(st.mag, self.sensor_count)
        vec = _fit_vecs(st.vec, self.sensor_count) if self.has_vec else None
        self._corners[frozenset(
            (c, self._bin(st.levels[c])) for c in chambers)] = (mag, vec)
        for c in chambers:
            slot = axes.setdefault(c, {}).setdefault(self._bin(st.levels[c]),
                                                     [0.0, 0.0])
            slot[0] += float(st.levels[c])
            slot[1] += 1.0
            cmax = self._chamber_max.setdefault(c, [0.0] * self.sensor_count)
            for s in range(self.sensor_count):
                cmax[s] = max(cmax[s], abs(mag[s]))

    def _bin(self, level: float) -> int:
        return int(round(float(level) / self.bin_pct))

    @property
    def is_zero(self) -> bool:
        """True when no measured corner has any non-zero offset (a no-op)."""
        return not any(any(mag) for mag, _ in self._corners.values())

    @property
    def chambers(self) -> list[int]:
        return sorted(self._axes)

    def chamber_max_abs(self, chamber: int, sensor: int) -> float:
        """Largest |offset| (uT) ``chamber`` was measured to put on ``sensor`` at
        any level — the worst-case bound for the guard and suppression."""
        row = self._chamber_max.get(int(chamber))
        return row[sensor] if row and sensor < len(row) else 0.0

    def _corner(self, key: frozenset) -> tuple[list[float], list[Vec] | None]:
        """Offset at a grid corner, with an additive-over-singles fallback for an
        unmeasured higher-order corner. A missing *single-member* corner has no
        lower decomposition, so it degrades to zero (no data) — never recursing
        on itself, which is what a chamber measured only in combination hits."""
        zeros = [0.0] * self.sensor_count
        zvec = ([list(_ZERO_VEC) for _ in range(self.sensor_count)]
                if self.has_vec else None)
        hit = self._corners.get(key)
        if hit is not None:
            return hit
        if len(key) <= 1:                        # empty (rest) or missing single
            return zeros, zvec
        mag = list(zeros)
        vec = ([list(_ZERO_VEC) for _ in range(self.sensor_count)]
               if self.has_vec else None)
        for member in key:                       # decompose to single-chamber corners
            cmag, cvec = self._corner(frozenset({member}))
            for s in range(self.sensor_count):
                mag[s] += cmag[s]
                if vec is not None and cvec is not None:
                    vec[s] = [vec[s][i] + cvec[s][i] for i in range(3)]
        return mag, vec

    @staticmethod
    def _bracket(pts: list[tuple[float, int]], q: float) -> list[tuple[int, float]]:
        """The one or two grid bins surrounding level ``q`` with their weights."""
        if q >= pts[-1][0]:
            return [(pts[-1][1], 1.0)]
        for i in range(len(pts) - 1):
            lo_l, lo_b = pts[i]
            hi_l, hi_b = pts[i + 1]
            if lo_l <= q <= hi_l:
                span = hi_l - lo_l
                t = 0.0 if span <= 0.0 else (q - lo_l) / span
                return [(lo_b, 1.0 - t), (hi_b, t)]
        return [(pts[-1][1], 1.0)]

    def offset_at(self, levels: Mapping[int, float]
                  ) -> tuple[list[float], list[Vec] | None]:
        """Per-sensor expected (scalar, vector) offset at the given live levels."""
        mag = [0.0] * self.sensor_count
        vec = ([list(_ZERO_VEC) for _ in range(self.sensor_count)]
               if self.has_vec else None)
        brackets = []
        for c, pts in self._axes.items():
            q = max(0.0, float(levels.get(c, 0.0)))
            if q > 0.0:
                brackets.append((c, self._bracket(pts, q)))
        if not brackets:
            return mag, vec
        for choice in product(*[opts for _, opts in brackets]):
            weight = 1.0
            members = []
            for (c, _opts), (b, frac) in zip(brackets, choice):
                weight *= frac
                if b != 0:
                    members.append((c, b))
            if weight <= 0.0:
                continue
            cmag, cvec = self._corner(frozenset(members))
            for s in range(self.sensor_count):
                mag[s] += weight * cmag[s]
                if vec is not None and cvec is not None:
                    vec[s] = [vec[s][i] + weight * cvec[s][i] for i in range(3)]
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

    ``grid`` is the measured :class:`GridCompensation` whose chamber ids match the
    level keys fed to :meth:`compensate`. Missing sensors/chambers count as 0.

    ``margin_frac`` raises a sensor's activation threshold by that fraction of
    the correction applied to it, so large corrections (where calibration error
    is proportionally larger) cannot flip a sensor active on their own. ``guard``
    hardens sensors coupled to a chamber whose level just changed (worst-case
    boost: that chamber's strongest measured offset on the sensor).
    """

    def __init__(
        self,
        grid: GridCompensation,
        *,
        sensor_count: int | None = None,
        threshold_ut: float = DEFAULT_THRESHOLD_UT,
        margin_frac: float = 0.0,
        guard: TransitionGuard | None = None,
        suppress_pct: float | None = None,
        suppress_coupling_ut: float = DEFAULT_SUPPRESS_COUPLING_UT,
    ) -> None:
        self._grid = grid
        self.sensor_count = (grid.sensor_count if sensor_count is None
                             else max(0, int(sensor_count)))
        self.threshold_ut = float(threshold_ut)
        self.margin_frac = max(0.0, float(margin_frac))
        self.guard = guard
        self.suppress_pct = None if suppress_pct is None else float(suppress_pct)
        self.suppress_coupling_ut = float(suppress_coupling_ut)
        self.has_vector = grid.has_vec

    @property
    def is_empty(self) -> bool:
        """True when there is no measured coupling (compensation is a no-op)."""
        return self._grid.is_zero

    def _expected(self, levels: Mapping[int, float]
                  ) -> tuple[list[float], list[Vec] | None]:
        """Per-sensor expected (scalar, vector) offsets for the given levels."""
        return self._grid.offset_at(levels)

    def expected_offset(self, levels: Mapping[int, float]) -> list[float]:
        """Per-sensor expected scalar offset (uT) for the given ``levels`` (%)."""
        return self._expected(levels)[0]

    def _suppressed(self, levels: Mapping[int, float]) -> set[int]:
        """Sensors to blank entirely because a strongly-coupled chamber is at or
        above ``suppress_pct`` (the opt-in 'ignore while actuated' fallback)."""
        if self.suppress_pct is None:
            return set()
        out: set[int] = set()
        for chamber in self._grid.chambers:
            if float(levels.get(chamber, 0.0)) < self.suppress_pct:
                continue
            for s in range(self.sensor_count):
                if self._grid.chamber_max_abs(chamber, s) >= self.suppress_coupling_ut:
                    out.add(s)
        return out

    def _guard_boost(self, levels: Mapping[int, float],
                     now_ms: float) -> list[float]:
        """Per-sensor extra threshold while coupled chambers are unsettled."""
        boost = [0.0] * self.sensor_count
        if self.guard is None:
            return boost
        for chamber in self.guard.update(levels, now_ms):
            for s in range(self.sensor_count):
                boost[s] += self._grid.chamber_max_abs(chamber, s)
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

def coupling_to_config(model: Any, *, bin_pct: float = DEFAULT_BIN_PCT
                       ) -> dict[str, Any]:
    """Build the stored ``touch.coupling`` dict from a measured coupling model.

    ``model`` is a :class:`src.core.touch_coupling.CouplingModel`. The config
    stores each measured state (single chamber or combination) as
    ``{chambers, levels, mag, [vec]}`` plus the rest ``baseline`` and the grid
    ``bin_pct`` the runtime snaps levels to. Chambers/level keys are ints/strings
    for YAML/JSON friendliness; magnitudes are rounded."""
    cfg: dict[str, Any] = {
        "unit": "uT",
        "sensor_count": int(model.sensor_count),
        "bin_pct": round(float(bin_pct), 1),
        "baseline": [round(float(v), 2) for v in model.baseline],
        "states": model.states_for_config(),
    }
    if model.baseline_vec is not None:
        cfg["baseline_vec"] = [[round(float(c), 1) for c in v]
                               for v in model.baseline_vec]
    return cfg


def _grid_from_config(coupling: Mapping[str, Any],
                      sensor_count: int) -> GridCompensation:
    """Parse stored coupling states into a :class:`GridCompensation`."""
    states: list[CouplingState] = []
    for st in coupling.get("states") or []:
        try:
            chambers = frozenset(int(c) for c in st["chambers"])
            levels = {int(k): float(v) for k, v in (st.get("levels") or {}).items()}
            mag = [float(v) for v in st["mag"]]
            states.append(CouplingState(chambers, levels, mag, st.get("vec")))
        except (TypeError, ValueError, KeyError):
            continue
    return GridCompensation(
        states, sensor_count,
        bin_pct=float(coupling.get("bin_pct", DEFAULT_BIN_PCT)))


def compensator_from_config(touch: Mapping[str, Any] | None) -> TouchCompensator | None:
    """Build a :class:`TouchCompensator` from a skin's ``touch`` config, or
    ``None`` when compensation is absent or disabled.

    Reads ``touch.coupling`` (the measured grid states) and the optional
    ``touch.compensation`` tuning block (``enabled``, ``threshold_ut``,
    ``margin_frac``, ``guard_ms``, ``guard_level_eps``, ``suppress_pct``).
    Absent tuning keys default to the pre-upgrade behaviour (no margin, no
    guard).

    The activation threshold resolves ``compensation.threshold_ut`` →
    ``touch.act_threshold_ut`` (the sensitivity pushed to the node, e.g. the
    per-skin-type saved value) → :data:`DEFAULT_THRESHOLD_UT`, so the
    compensated ``act`` and the node's own detection agree by default."""
    touch = touch or {}
    coupling = touch.get("coupling")
    tuning = touch.get("compensation") or {}
    if not coupling or not tuning.get("enabled", False):
        return None
    sensor_count = int(coupling.get("sensor_count")
                       or touch.get("sensor_count", 0)
                       or max((len(st.get("mag") or []) for st in
                               (coupling.get("states") or [])), default=0))
    grid = _grid_from_config(coupling, sensor_count)
    if not grid.chambers:
        return None
    guard_ms = float(tuning.get("guard_ms", 0.0) or 0.0)
    guard = TransitionGuard(
        settle_ms=guard_ms,
        level_eps=float(tuning.get("guard_level_eps", DEFAULT_GUARD_LEVEL_EPS)),
    ) if guard_ms > 0.0 else None
    suppress = tuning.get("suppress_pct")
    return TouchCompensator(
        grid,
        sensor_count=sensor_count,
        threshold_ut=float(tuning.get(
            "threshold_ut",
            touch.get("act_threshold_ut") or DEFAULT_THRESHOLD_UT)),
        margin_frac=float(tuning.get("margin_frac", 0.0)),
        guard=guard,
        suppress_pct=None if suppress is None else float(suppress),
    )

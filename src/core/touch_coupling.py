"""Touch coupling — measure which chamber, when inflated, moves which sensor.

A magnet sits in the silicone above each touch sensor; inflating a nearby
chamber deforms the silicone and shifts the magnet, so chamber actuation can
masquerade as a touch. This module turns a *sweep* (inflate one chamber at a
time, no touching) into per-chamber **level→offset curves**: for each inflation
level held during the sweep, how much that chamber moves each sensor's reading
(raw µT, the ``mag`` field) — plus a derived chamber<->sensor map. A sweep that
holds several levels per chamber (e.g. 25/50/75/100 %) yields a multi-point
curve, capturing the (nonlinear) silicone response; a single-level sweep yields
the legacy one-point matrix. When the samples carry the firmware's 3-axis
``vec`` deltas (the ``MAG_VECTOR`` build), each curve point also records the
per-sensor offset *vectors*, enabling vector compensation.

Pure and Qt-free: it works on already-collected samples or a recording JSONL,
so it is fully unit-testable without hardware.

**Sensor-lag handling.** Chamber ``status`` broadcasts lag the real chamber
state (500 ms cadence, or faster with fast telemetry), while ``magnet`` samples
stream at ~28 Hz. Rather than guess the exact lag, we only measure at *steady
state*: samples within ``settle_ms`` of any change in the active-chamber/level
classification are discarded, so transitions never pollute the means. Collect
the sweep by holding each chamber at each level for a few seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# Default classification thresholds (percent of chamber max).
ACTIVE_MIN = 20.0   # a chamber at/above this is considered "inflated"
REST_MAX = 10.0     # every chamber at/below this means "rest" (baseline)
SETTLE_MS = 800.0   # guard window after a state change, dropped from the means
BIN_PCT = 10.0      # level-bin width: samples this close share one curve point

# A single sweep sample: timestamp (ms), per-chamber pressure %, per-sensor µT,
# and optionally the per-sensor 3-axis deltas ([[dx,dy,dz], ...], µT).
Sample = Sequence[Any]


def classify_state(pressures: dict[int, float], *,
                   active_min: float = ACTIVE_MIN) -> frozenset[int]:
    """The set of chambers that are *inflated* (>= ``active_min``) right now.

    The empty set means rest. One element is a single-chamber state; two or more
    is a co-inflation (a combination). A chamber merely off-rest but below
    ``active_min`` (e.g. a narrow-range chamber venting to a small residual) is
    treated as **not** part of the state — its faint coupling is folded into the
    active state's measured delta rather than dropping the whole sample. The
    time-based settle guard, not this classifier, rejects transitions.
    """
    return frozenset(c for c, p in pressures.items() if p >= active_min)


def classify_active(pressures: dict[int, float], *, active_min: float = ACTIVE_MIN,
                    rest_max: float = REST_MAX) -> int | None | str:
    """Single-chamber view of :func:`classify_state` (kept for callers/tests).

    Returns the chamber id when exactly one chamber is inflated; ``"rest"`` when
    none is inflated and every chamber is at rest (<= ``rest_max``); ``None`` when
    two chambers are up, or a lone chamber sits in the ``rest_max``..``active_min``
    dead zone (a transition to skip).
    """
    on = classify_state(pressures, active_min=active_min)
    if not on:
        return "rest" if all(p <= rest_max for p in pressures.values()) else None
    if len(on) == 1:
        return next(iter(on))
    return None


@dataclass
class MeasuredState:
    """One measured operating point: a set of co-inflated chambers held at
    specific levels, and the per-sensor offset it produced.

    ``chambers`` is the inflated set (size 1 = single chamber; >= 2 = a
    combination). ``levels`` is the mean measured level (%) of each member.
    ``mag`` is the per-sensor mean µT shift vs rest; ``vec`` the per-sensor mean
    3-axis shift when every contributing sample carried the firmware ``vec``
    field, else ``None``. ``n`` = samples averaged."""
    chambers: frozenset[int]
    levels: dict[int, float]
    mag: list[float]
    vec: list[list[float]] | None = None
    n: int = 0

    @property
    def order(self) -> int:
        return len(self.chambers)


@dataclass
class CouplingModel:
    """Every operating point a sweep measured — singles *and* combinations.

    Sweeping all chamber subsets over a per-member level grid is exactly the
    full Cartesian grid over each chamber's axis ``[0, g1, .., 100]`` (a state
    with a member at level 0 is simply a lower-order subset), so this list of
    :class:`MeasuredState` is an N-dimensional lookup table the runtime
    interpolates multilinearly. ``baseline`` is the rest mean per sensor — the
    ``mag`` offsets are already rest-subtracted."""
    sensor_count: int
    states: list[MeasuredState] = field(default_factory=list)
    baseline: list[float] = field(default_factory=list)
    baseline_vec: list[list[float]] | None = None
    n_rest: int = 0

    @property
    def chambers(self) -> list[int]:
        """Every chamber that appears inflated in some measured state."""
        return sorted({c for st in self.states for c in st.chambers})

    @property
    def has_vec(self) -> bool:
        """True when every measured state carries 3-axis deltas."""
        return bool(self.states) and all(st.vec is not None for st in self.states)

    @property
    def max_order(self) -> int:
        """Largest co-inflation order measured (1 = singles only)."""
        return max((st.order for st in self.states), default=0)

    def singles(self) -> dict[int, MeasuredState]:
        """The strongest-level single-chamber state per chamber (the classic
        per-chamber view — used for the mapping and the preview table)."""
        best: dict[int, MeasuredState] = {}
        for st in self.states:
            if st.order != 1:
                continue
            c = next(iter(st.chambers))
            cur = best.get(c)
            if cur is None or st.levels[c] > cur.levels[c]:
                best[c] = st
        return best

    def combos(self) -> list[MeasuredState]:
        """States with two or more co-inflated chambers, highest order first."""
        return sorted((st for st in self.states if st.order >= 2),
                      key=lambda st: (-st.order, tuple(sorted(st.chambers))))

    def deltas(self) -> dict[int, list[float]]:
        """Per-chamber offset at its strongest single-chamber level."""
        return {c: st.mag for c, st in self.singles().items()}

    def mapping(self, threshold: float) -> dict[int, list[int]]:
        """chamber -> sensors it moves by at least ``threshold`` (from singles)."""
        return {c: [s for s, d in enumerate(row) if d >= threshold]
                for c, row in self.deltas().items()}

    def sensor_primary_chamber(self, threshold: float) -> dict[int, int | None]:
        """sensor -> the single chamber that moves it most (None if below thr.)."""
        deltas = self.deltas()
        out: dict[int, int | None] = {}
        for sensor in range(self.sensor_count):
            best_chamber, best_delta = None, threshold
            for chamber, row in deltas.items():
                if sensor < len(row) and row[sensor] >= best_delta:
                    best_chamber, best_delta = chamber, row[sensor]
            out[sensor] = best_chamber
        return out

    def states_for_config(self) -> list[dict[str, Any]]:
        """Plain-data states for :mod:`touch_compensation` config serialisation."""
        rows: list[dict[str, Any]] = []
        for st in self.states:
            row: dict[str, Any] = {
                "chambers": sorted(st.chambers),
                "levels": {str(c): round(st.levels[c], 1)
                           for c in sorted(st.chambers)},
                "mag": [round(float(v), 2) for v in st.mag],
            }
            if st.vec is not None:
                row["vec"] = [[round(float(c), 1) for c in v] for v in st.vec]
            rows.append(row)
        return rows


class _StateAccum:
    """Running mean of mag (and, when consistently present, vec) plus the mean
    level of each co-inflated member of the state."""

    def __init__(self, sensor_count: int) -> None:
        self._count = sensor_count
        self.n = 0
        self.mag_sum = [0.0] * sensor_count
        self.vec_sum = [[0.0, 0.0, 0.0] for _ in range(sensor_count)]
        self.vec_n = 0
        self.level_sums: dict[int, float] = {}

    def add(self, mag: Sequence[float], vec: Any,
            levels: Mapping[int, float]) -> None:
        self.n += 1
        for s in range(self._count):
            self.mag_sum[s] += float(mag[s]) if s < len(mag) else 0.0
        for c, lv in levels.items():
            self.level_sums[c] = self.level_sums.get(c, 0.0) + float(lv)
        if isinstance(vec, (list, tuple)):
            self.vec_n += 1
            for s in range(self._count):
                v = vec[s] if s < len(vec) else None
                if isinstance(v, (list, tuple)) and len(v) >= 3:
                    self.vec_sum[s][0] += float(v[0])
                    self.vec_sum[s][1] += float(v[1])
                    self.vec_sum[s][2] += float(v[2])

    def mean_mag(self) -> list[float]:
        return ([v / self.n for v in self.mag_sum] if self.n
                else [0.0] * self._count)

    def mean_vec(self) -> list[list[float]] | None:
        """Per-sensor mean 3-axis reading — only when every sample carried it."""
        if not self.n or self.vec_n != self.n:
            return None
        return [[c / self.vec_n for c in row] for row in self.vec_sum]

    def mean_levels(self) -> dict[int, float]:
        return ({c: s / self.n for c, s in self.level_sums.items()}
                if self.n else {})


class _SettleGuard:
    """Labels each sample with its inflated-chamber *set* and per-member levels,
    and suppresses everything within ``settle_ms`` of any change to that set or
    to any member's level (by more than one bin), so transitions never pollute
    the means. Generalises the single-chamber staircase guard to combinations."""

    def __init__(self, active_min: float, settle_ms: float,
                 bin_pct: float) -> None:
        self._active_min = active_min
        self._settle_ms = settle_ms
        self._bin_pct = bin_pct
        self._subset: frozenset[int] | None = None
        self._anchors: dict[int, float] = {}
        self._last_change_ms = float("-inf")

    def settled(self, t_ms: float, pressures: dict[int, float]
                ) -> tuple[frozenset[int], dict[int, float]] | None:
        """(inflated set, member levels) for a steady sample, or None to skip."""
        subset = classify_state(pressures, active_min=self._active_min)
        levels = {c: float(pressures.get(c, 0.0)) for c in subset}
        moved = any(abs(levels[c] - self._anchors.get(c, -1e9)) > self._bin_pct
                    for c in subset)
        if subset != self._subset or moved:
            self._subset = subset
            self._anchors = levels
            self._last_change_ms = t_ms
        if t_ms - self._last_change_ms < self._settle_ms:
            return None
        return subset, dict(levels)


def build_coupling(samples: Iterable[Sample], sensor_count: int, *,
                   active_min: float = ACTIVE_MIN, rest_max: float = REST_MAX,
                   settle_ms: float = SETTLE_MS,
                   bin_pct: float = BIN_PCT) -> CouplingModel:
    """Build a :class:`CouplingModel` from time-ordered sweep samples.

    Samples must be sorted by timestamp; each is ``(t_ms, {slot: pct},
    mag_vector[, vec_rows])``. A :class:`_SettleGuard` labels each with its
    inflated-chamber set and member levels and drops transitions; samples that
    share a set *and* a per-member level bin are averaged into one
    :class:`MeasuredState`. ``rest_max`` is accepted for signature parity with
    :func:`classify_active` but the set classifier does not use it."""
    del rest_max
    rest = _StateAccum(sensor_count)
    bins: dict[frozenset, tuple[frozenset[int], _StateAccum]] = {}
    guard = _SettleGuard(active_min, settle_ms, bin_pct)

    for sample in samples:
        t_ms, pressures, mag = sample[0], sample[1], sample[2]
        vec = sample[3] if len(sample) > 3 else None
        settled = guard.settled(t_ms, pressures)
        if settled is None:
            continue
        subset, levels = settled
        if not subset:
            rest.add(mag, vec, {})
            continue
        key = frozenset((c, int(round(levels[c] / bin_pct))) for c in subset)
        bins.setdefault(key, (subset, _StateAccum(sensor_count)))[1].add(
            mag, vec, levels)

    baseline = rest.mean_mag()
    baseline_vec = rest.mean_vec()
    base_vec = baseline_vec or [[0.0, 0.0, 0.0] for _ in range(sensor_count)]

    states: list[MeasuredState] = []
    for key in sorted(bins, key=lambda k: (len(k), sorted(k))):
        subset, acc = bins[key]
        mean_vec = acc.mean_vec()
        states.append(MeasuredState(
            chambers=subset,
            levels=acc.mean_levels(),
            mag=[m - b for m, b in zip(acc.mean_mag(), baseline)],
            vec=([[v[i] - base_vec[s][i] for i in range(3)]
                  for s, v in enumerate(mean_vec)]
                 if mean_vec is not None else None),
            n=acc.n))

    return CouplingModel(sensor_count=sensor_count, states=states,
                         baseline=baseline, baseline_vec=baseline_vec,
                         n_rest=rest.n)


# ---------------------------------------------------------------------------
# Recording (JSONL) parsing
# ---------------------------------------------------------------------------

def _epoch_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


def _track_pressure(msg: dict, pressures: dict[int, float]) -> None:
    """Fold a ``status`` message into the last-known per-chamber pressures."""
    try:
        pressures[int(msg["chamber"])] = float(msg.get("pressure", 0.0))
    except (TypeError, ValueError, KeyError):
        pass


def _magnet_sample(t_iso: str, msg: dict, pressures: dict[int, float],
                   field: str) -> Sample | None:
    """Build one sample from a ``magnet`` message, or None if it lacks ``field``.

    Compensated messages (recorded alongside raw when compensation is on) are
    skipped: the coupling must be measured on the raw µT, otherwise it would be
    fitted against readings that already had a coupling subtracted."""
    if msg.get("compensated"):
        return None
    mag = msg.get(field) or []
    if not isinstance(mag, list):
        return None
    xyz = msg.get("vec")
    return (_epoch_ms(t_iso), dict(pressures), [float(v) for v in mag],
            xyz if isinstance(xyz, list) else None)


def samples_from_recording(path: str | Path, field: str = "mag") -> Iterator[Sample]:
    """Yield :data:`Sample` tuples from a stream recording JSONL.

    Tracks the latest per-chamber pressure from ``status`` messages and emits a
    sample at each ``magnet`` message (the fast stream), pairing the chosen
    magnet ``field`` vector — plus the 3-axis ``vec`` rows when the recording
    has them — with the last-known chamber pressures. Defaults to ``"mag"``
    (raw uT — what the live stream, the PC detection path and
    :mod:`src.core.touch_compensation` use). ``"adj"`` is only for older
    recordings that still carried the normalised field (no longer streamed).
    """
    pressures: dict[int, float] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get("msg")
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "status" and "chamber" in msg:
                _track_pressure(msg, pressures)
            elif msg.get("type") == "magnet":
                sample = _magnet_sample(obj["t"], msg, pressures, field)
                if sample is not None:
                    yield sample


def build_coupling_from_recording(path: str | Path, sensor_count: int,
                                  field: str = "mag", **kw) -> CouplingModel:
    """Convenience: parse a recording and build the coupling model.

    ``field`` selects the magnet vector ("mag" raw uT — the default; "adj" only
    for older recordings that carried the normalised field)."""
    return build_coupling(samples_from_recording(path, field), sensor_count, **kw)

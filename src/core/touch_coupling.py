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
from typing import Any, Iterable, Iterator, Sequence

# Default classification thresholds (percent of chamber max).
ACTIVE_MIN = 20.0   # a chamber at/above this is considered "inflated"
REST_MAX = 10.0     # every chamber at/below this means "rest" (baseline)
SETTLE_MS = 800.0   # guard window after a state change, dropped from the means
BIN_PCT = 10.0      # level-bin width: samples this close share one curve point

# A single sweep sample: timestamp (ms), per-chamber pressure %, per-sensor µT,
# and optionally the per-sensor 3-axis deltas ([[dx,dy,dz], ...], µT).
Sample = Sequence[Any]


def classify_active(pressures: dict[int, float], *, active_min: float = ACTIVE_MIN,
                    rest_max: float = REST_MAX) -> int | None | str:
    """Classify the instantaneous state from per-chamber pressures.

    Returns the chamber id when exactly one chamber is inflated (>= active_min)
    and all others are at rest (<= rest_max); ``"rest"`` when every chamber is at
    rest; ``None`` when the state is ambiguous (a transition, or two chambers up)
    and the sample should be skipped.
    """
    high = [c for c, p in pressures.items() if p >= active_min]
    if not high and all(p <= rest_max for p in pressures.values()):
        return "rest"
    if len(high) == 1 and all(p <= rest_max
                              for c, p in pressures.items() if c != high[0]):
        return high[0]
    return None


@dataclass
class LevelPoint:
    """One measured level of a chamber's coupling curve.

    ``mag`` is the per-sensor mean µT shift vs rest at ``level_pct``; ``vec`` is
    the per-sensor mean 3-axis shift when every contributing sample carried the
    firmware's ``vec`` field, else ``None``. ``n`` = samples averaged."""
    level_pct: float
    mag: list[float]
    vec: list[list[float]] | None = None
    n: int = 0


@dataclass
class CouplingMatrix:
    """Per-chamber level→offset curves measured by a sweep.

    ``curves`` maps a chamber to its :class:`LevelPoint` list (ascending level).
    ``deltas`` exposes the top (strongest-level) point per chamber — the legacy
    single-matrix view used by the chamber<->sensor mapping and old configs."""
    sensor_count: int
    curves: dict[int, list[LevelPoint]] = field(default_factory=dict)
    baseline: list[float] = field(default_factory=list)   # rest mean per sensor
    baseline_vec: list[list[float]] | None = None          # rest mean 3-axis
    n_rest: int = 0

    @property
    def chambers(self) -> list[int]:
        return sorted(self.curves)

    @property
    def deltas(self) -> dict[int, list[float]]:
        """Per-chamber offsets at the strongest measured level (legacy view)."""
        return {c: pts[-1].mag for c, pts in self.curves.items() if pts}

    @property
    def n_active(self) -> dict[int, int]:
        """Samples used per chamber, across every level."""
        return {c: sum(p.n for p in pts) for c, pts in self.curves.items()}

    @property
    def has_vec(self) -> bool:
        """True when every curve point carries 3-axis deltas."""
        return bool(self.curves) and all(
            p.vec is not None for pts in self.curves.values() for p in pts)

    def curves_for_config(self) -> dict[int, list[dict[str, Any]]]:
        """Plain-data curves for ``touch_compensation.coupling_to_config``."""
        return {c: [{"pct": p.level_pct, "mag": p.mag, "vec": p.vec}
                    for p in pts]
                for c, pts in self.curves.items()}

    def mapping(self, threshold: float) -> dict[int, list[int]]:
        """chamber -> sensors it moves by at least ``threshold`` (sorted)."""
        out: dict[int, list[int]] = {}
        for chamber, row in self.deltas.items():
            out[chamber] = [s for s, d in enumerate(row) if d >= threshold]
        return out

    def sensor_primary_chamber(self, threshold: float) -> dict[int, int | None]:
        """sensor -> the chamber that moves it most (None if below threshold)."""
        deltas = self.deltas
        out: dict[int, int | None] = {}
        for sensor in range(self.sensor_count):
            best_chamber, best_delta = None, threshold
            for chamber, row in deltas.items():
                if sensor < len(row) and row[sensor] >= best_delta:
                    best_chamber, best_delta = chamber, row[sensor]
            out[sensor] = best_chamber
        return out


class _Accum:
    """Running mean of mag (and, when consistently present, vec and level)."""

    def __init__(self, sensor_count: int) -> None:
        self._count = sensor_count
        self.n = 0
        self.mag_sum = [0.0] * sensor_count
        self.vec_sum = [[0.0, 0.0, 0.0] for _ in range(sensor_count)]
        self.vec_n = 0
        self.level_sum = 0.0

    def add(self, mag: Sequence[float], vec: Any, level: float = 0.0) -> None:
        self.n += 1
        self.level_sum += level
        for s in range(self._count):
            self.mag_sum[s] += float(mag[s]) if s < len(mag) else 0.0
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

    def mean_level(self) -> float:
        return self.level_sum / self.n if self.n else 0.0


class _SettleGuard:
    """Stateful classifier for a sweep stream: labels each sample (rest / one
    chamber+level / skip) and suppresses everything within ``settle_ms`` of a
    classification *or level* change, so transitions never pollute the means.
    A level step within the same chamber (a staircase sweep) counts as a change
    once it drifts a whole bin away from where the current plateau anchored."""

    def __init__(self, active_min: float, rest_max: float,
                 settle_ms: float, bin_pct: float) -> None:
        self._active_min = active_min
        self._rest_max = rest_max
        self._settle_ms = settle_ms
        self._bin_pct = bin_pct
        self._state: int | None | str = None
        self._anchor_level = 0.0
        self._last_change_ms = float("-inf")

    def settled(self, t_ms: float, pressures: dict[int, float]
                ) -> tuple[int | str, float] | None:
        """(state, level) for a steady sample, or ``None`` to skip it."""
        state = classify_active(pressures, active_min=self._active_min,
                                rest_max=self._rest_max)
        level = (float(pressures.get(state, 0.0))
                 if isinstance(state, int) else 0.0)
        if state != self._state or abs(level - self._anchor_level) > self._bin_pct:
            self._state = state
            self._anchor_level = level
            self._last_change_ms = t_ms
        if state is None or t_ms - self._last_change_ms < self._settle_ms:
            return None
        return state, level


def build_coupling(samples: Iterable[Sample], sensor_count: int, *,
                   active_min: float = ACTIVE_MIN, rest_max: float = REST_MAX,
                   settle_ms: float = SETTLE_MS,
                   bin_pct: float = BIN_PCT) -> CouplingMatrix:
    """Build a :class:`CouplingMatrix` from time-ordered sweep samples.

    Samples must be sorted by timestamp; each is ``(t_ms, {slot: pct},
    mag_vector[, vec_rows])``. Each is classified (rest / a chamber / skip) with
    a :class:`_SettleGuard`, so pressure-sensor lag and the coarse status
    cadence don't smear the means across transitions. Active samples of one
    chamber are grouped into ``bin_pct``-wide level bins — one curve point per
    bin, at the bin's mean measured level.
    """
    rest = _Accum(sensor_count)
    bins: dict[tuple[int, int], _Accum] = {}
    guard = _SettleGuard(active_min, rest_max, settle_ms, bin_pct)

    for sample in samples:
        t_ms, pressures, mag = sample[0], sample[1], sample[2]
        vec = sample[3] if len(sample) > 3 else None
        settled = guard.settled(t_ms, pressures)
        if settled is None:
            continue
        state, level = settled
        if state == "rest":
            rest.add(mag, vec)
        else:
            key = (state, int(round(level / bin_pct)))
            bins.setdefault(key, _Accum(sensor_count)).add(mag, vec, level)

    baseline = rest.mean_mag()
    baseline_vec = rest.mean_vec()
    base_vec = baseline_vec or [[0.0, 0.0, 0.0] for _ in range(sensor_count)]

    curves: dict[int, list[LevelPoint]] = {}
    for (chamber, _bin), acc in sorted(bins.items()):
        mean_vec = acc.mean_vec()
        point = LevelPoint(
            level_pct=acc.mean_level(),
            mag=[m - b for m, b in zip(acc.mean_mag(), baseline)],
            vec=([[v[i] - base_vec[s][i] for i in range(3)]
                  for s, v in enumerate(mean_vec)]
                 if mean_vec is not None else None),
            n=acc.n)
        curves.setdefault(chamber, []).append(point)
    for pts in curves.values():
        pts.sort(key=lambda p: p.level_pct)

    return CouplingMatrix(sensor_count=sensor_count, curves=curves,
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
                                  field: str = "mag", **kw) -> CouplingMatrix:
    """Convenience: parse a recording and build the coupling matrix.

    ``field`` selects the magnet vector ("mag" raw uT — the default; "adj" only
    for older recordings that carried the normalised field)."""
    return build_coupling(samples_from_recording(path, field), sensor_count, **kw)

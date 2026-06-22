"""Touch coupling — measure which chamber, when inflated, moves which sensor.

A magnet sits in the silicone above each touch sensor; inflating a nearby
chamber deforms the silicone and shifts the magnet, so chamber actuation can
masquerade as a touch. This module turns a *sweep* (inflate one chamber at a
time, no touching) into a coupling matrix ``[chamber x sensor]`` of how much
each chamber moves each sensor's normalised reading (``adj``), plus a derived
chamber<->sensor map.

Pure and Qt-free: it works on already-collected samples or a recording JSONL,
so it is fully unit-testable without hardware.

**Sensor-lag handling.** Chamber ``status`` broadcasts arrive every ~500 ms and
the pressure sensor lags the real chamber state, while ``magnet`` samples stream
at ~28 Hz. Rather than guess the exact lag, we only measure at *steady state*:
samples within ``settle_ms`` of any change in the active-chamber classification
are discarded, so transitions (where the two streams are misaligned) never
pollute the means. Collect the sweep by holding each chamber inflated for a few
seconds, then resting, before the next one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

# Default classification thresholds (percent of chamber max).
ACTIVE_MIN = 30.0   # a chamber at/above this is considered "inflated"
REST_MAX = 10.0     # every chamber at/below this means "rest" (baseline)
SETTLE_MS = 800.0   # guard window after a state change, dropped from the means

# A single sweep sample: timestamp (ms), per-chamber pressure %, per-sensor adj.
Sample = tuple[float, dict[int, float], list[float]]


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
class CouplingMatrix:
    """Per-chamber mean ``adj`` delta vs rest, for each sensor."""
    sensor_count: int
    deltas: dict[int, list[float]] = field(default_factory=dict)  # chamber -> [delta per sensor]
    baseline: list[float] = field(default_factory=list)           # rest mean per sensor
    n_rest: int = 0
    n_active: dict[int, int] = field(default_factory=dict)        # chamber -> samples used

    @property
    def chambers(self) -> list[int]:
        return sorted(self.deltas)

    def mapping(self, threshold: float) -> dict[int, list[int]]:
        """chamber -> sensors it moves by at least ``threshold`` (sorted)."""
        out: dict[int, list[int]] = {}
        for chamber, row in self.deltas.items():
            out[chamber] = [s for s, d in enumerate(row) if d >= threshold]
        return out

    def sensor_primary_chamber(self, threshold: float) -> dict[int, int | None]:
        """sensor -> the chamber that moves it most (None if below threshold)."""
        out: dict[int, int | None] = {}
        for sensor in range(self.sensor_count):
            best_chamber, best_delta = None, threshold
            for chamber, row in self.deltas.items():
                if sensor < len(row) and row[sensor] >= best_delta:
                    best_chamber, best_delta = chamber, row[sensor]
            out[sensor] = best_chamber
        return out


def build_coupling(samples: Iterable[Sample], sensor_count: int, *,
                   active_min: float = ACTIVE_MIN, rest_max: float = REST_MAX,
                   settle_ms: float = SETTLE_MS) -> CouplingMatrix:
    """Build a :class:`CouplingMatrix` from time-ordered sweep samples.

    Samples must be sorted by timestamp. Each is classified (rest / a chamber /
    skip); samples within ``settle_ms`` of the last classification *change* are
    dropped so pressure-sensor lag and the coarse status cadence don't smear the
    means across transitions.
    """
    rest_sum = [0.0] * sensor_count
    rest_n = 0
    active_sum: dict[int, list[float]] = {}
    active_n: dict[int, int] = {}

    prev_state: int | None | str = None
    last_change_ms: float = float("-inf")

    for t_ms, pressures, adj in samples:
        state = classify_active(pressures, active_min=active_min, rest_max=rest_max)
        if state != prev_state:
            last_change_ms = t_ms
            prev_state = state
        if state is None:
            continue
        if t_ms - last_change_ms < settle_ms:   # still settling — skip guard window
            continue
        vec = _fit(adj, sensor_count)
        if state == "rest":
            for s in range(sensor_count):
                rest_sum[s] += vec[s]
            rest_n += 1
        else:
            row = active_sum.setdefault(state, [0.0] * sensor_count)
            for s in range(sensor_count):
                row[s] += vec[s]
            active_n[state] = active_n.get(state, 0) + 1

    baseline = [rest_sum[s] / rest_n if rest_n else 0.0 for s in range(sensor_count)]
    deltas: dict[int, list[float]] = {}
    for chamber, row in active_sum.items():
        n = active_n[chamber]
        deltas[chamber] = [row[s] / n - baseline[s] for s in range(sensor_count)]
    return CouplingMatrix(sensor_count=sensor_count, deltas=deltas,
                          baseline=baseline, n_rest=rest_n, n_active=active_n)


def _fit(adj: list[float], n: int) -> list[float]:
    """Pad/truncate an adj vector to exactly ``n`` sensors."""
    return [float(adj[s]) if s < len(adj) else 0.0 for s in range(n)]


# ---------------------------------------------------------------------------
# Recording (JSONL) parsing
# ---------------------------------------------------------------------------

def _epoch_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


def samples_from_recording(path: str | Path) -> Iterator[Sample]:
    """Yield :data:`Sample` tuples from a stream recording JSONL.

    Tracks the latest per-chamber pressure from ``status`` messages and emits a
    sample at each ``magnet`` message (the fast stream), pairing the magnet
    ``adj`` with the last-known chamber pressures.
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
            mtype = msg.get("type")
            if mtype == "status" and "chamber" in msg:
                try:
                    pressures[int(msg["chamber"])] = float(msg.get("pressure", 0.0))
                except (TypeError, ValueError):
                    continue
            elif mtype == "magnet":
                adj = msg.get("adj") or []
                if isinstance(adj, list):
                    yield (_epoch_ms(obj["t"]), dict(pressures),
                           [float(v) for v in adj])


def build_coupling_from_recording(path: str | Path, sensor_count: int, **kw
                                  ) -> CouplingMatrix:
    """Convenience: parse a recording and build the coupling matrix."""
    return build_coupling(samples_from_recording(path), sensor_count, **kw)

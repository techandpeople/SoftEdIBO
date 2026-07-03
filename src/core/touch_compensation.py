"""Pressure-informed touch compensation — pure, Qt-free, testable.

A magnet sits in the silicone above each MLX90393 touch sensor; inflating a
chamber deforms the silicone and shifts the magnet, so chamber actuation can
masquerade as a touch (see :mod:`src.core.touch_coupling` and
``docs/TOUCH_COUPLING.md``). The geometry is irregular — one chamber can move
*two* sensors by different amounts — so the fix is a full ``[chamber x sensor]``
offset matrix, not a 1:1 map.

:class:`TouchCompensator` subtracts, from each sensor's live magnitude, the sum
over all chambers of ``coupling[chamber][sensor] x (level / ref_level)`` — the
expected magnetic offset for the current inflation levels — then recomputes the
active-sensor set from the *residual*, so a real press still stands out while the
actuation offset is removed. An optional ``suppress_pct`` implements the
last-resort "ignore a sensor while its chamber is (near) fully actuated".

Works in magnitude (uT) space, matching the PC detection path (QuadrantDetector
consumes raw ``mag`` in uT). The coupling matrix must therefore be measured in
the same ``mag`` units.
"""

from __future__ import annotations

from typing import Any, Mapping

# Default activation threshold (uT) used to rederive the active-sensor set from
# the compensated magnitudes. Matches the firmware default act (act_threshold_ut
# 300), so enabling compensation with a zero matrix preserves the previous
# activation behaviour.
DEFAULT_THRESHOLD_UT = 300.0

# A chamber must couple a sensor by at least this many uT (at ref level) for the
# ``suppress_pct`` fallback to blank that sensor — so a faintly-coupled sensor is
# not killed just because some far chamber is inflated.
DEFAULT_SUPPRESS_COUPLING_UT = 50.0


class TouchCompensator:
    """Removes per-sensor actuation offset from a magnet reading.

    ``deltas`` maps a chamber id (the node slot, matching the chamber level keys
    fed to :meth:`compensate`) to a per-sensor list of expected magnitude offsets
    (uT) measured at ``ref_pct`` inflation. Missing sensors/chambers count as 0.
    """

    def __init__(
        self,
        deltas: Mapping[int, list[float]],
        *,
        sensor_count: int,
        ref_pct: float = 100.0,
        threshold_ut: float = DEFAULT_THRESHOLD_UT,
        suppress_pct: float | None = None,
        suppress_coupling_ut: float = DEFAULT_SUPPRESS_COUPLING_UT,
    ) -> None:
        self.sensor_count = max(0, int(sensor_count))
        self._deltas: dict[int, list[float]] = {
            int(c): [float(v) for v in row] for c, row in deltas.items()}
        self.ref_pct = float(ref_pct) if ref_pct else 100.0
        self.threshold_ut = float(threshold_ut)
        self.suppress_pct = None if suppress_pct is None else float(suppress_pct)
        self.suppress_coupling_ut = float(suppress_coupling_ut)

    @property
    def is_empty(self) -> bool:
        """True when there is no measured coupling (compensation is a no-op)."""
        return not any(any(v != 0.0 for v in row) for row in self._deltas.values())

    def expected_offset(self, levels: Mapping[int, float]) -> list[float]:
        """Per-sensor expected offset (uT) for the given chamber ``levels`` (%)."""
        offset = [0.0] * self.sensor_count
        for chamber, row in self._deltas.items():
            level = max(0.0, float(levels.get(chamber, 0.0)))
            if level <= 0.0:
                continue
            scale = level / self.ref_pct
            for s in range(min(self.sensor_count, len(row))):
                offset[s] += row[s] * scale
        return offset

    def _suppressed(self, levels: Mapping[int, float]) -> set[int]:
        """Sensors to blank entirely because a strongly-coupled chamber is at or
        above ``suppress_pct`` (the opt-in 'ignore while actuated' fallback)."""
        if self.suppress_pct is None:
            return set()
        out: set[int] = set()
        for chamber, row in self._deltas.items():
            if float(levels.get(chamber, 0.0)) < self.suppress_pct:
                continue
            for s in range(min(self.sensor_count, len(row))):
                if abs(row[s]) >= self.suppress_coupling_ut:
                    out.add(s)
        return out

    def compensate(self, mag: list[float],
                   levels: Mapping[int, float]) -> tuple[list[float], list[int]]:
        """Return ``(compensated_mag, active_sensors)`` for one reading.

        ``compensated_mag`` is the residual after subtracting the expected
        actuation offset (clamped at 0); ``active_sensors`` are the indices whose
        residual reaches :attr:`threshold_ut` (minus any suppressed sensor)."""
        offset = self.expected_offset(levels)
        suppressed = self._suppressed(levels)
        comp: list[float] = []
        act: list[int] = []
        for s in range(self.sensor_count):
            raw = float(mag[s]) if s < len(mag) else 0.0
            residual = 0.0 if s in suppressed else max(0.0, raw - offset[s])
            comp.append(residual)
            if residual >= self.threshold_ut:
                act.append(s)
        return comp, act

    def apply(self, data: Mapping[str, Any],
              levels: Mapping[int, float]) -> dict[str, Any]:
        """Return a shallow copy of a ``type:"magnet"`` message with ``mag`` and
        ``act`` replaced by their compensated values (other fields preserved).

        When there is no coupling and no suppression, the message is returned
        unchanged so the stream is bit-for-bit identical to raw."""
        out = dict(data)
        mag = data.get("mag")
        if not isinstance(mag, (list, tuple)):
            return out
        if self.is_empty and self.suppress_pct is None:
            return out
        comp, act = self.compensate([float(v) for v in mag], levels)
        out["mag"] = comp
        out["act"] = act
        out["compensated"] = True
        return out


# ---------------------------------------------------------------------------
# Config (de)serialisation — the stored ``touch.coupling`` / ``touch.compensation``
# ---------------------------------------------------------------------------

def coupling_to_config(deltas: Mapping[int, list[float]], sensor_count: int,
                       ref_pct: float = 100.0) -> dict[str, Any]:
    """Build the stored ``touch.coupling`` dict from a measured matrix.

    Slots are stored as strings (JSON/YAML object keys); magnitudes rounded."""
    return {
        "unit": "uT",
        "sensor_count": int(sensor_count),
        "ref_pct": round(float(ref_pct), 1),
        "deltas": {str(int(c)): [round(float(v), 2) for v in row]
                   for c, row in deltas.items()},
    }


def compensator_from_config(touch: Mapping[str, Any] | None) -> TouchCompensator | None:
    """Build a :class:`TouchCompensator` from a skin's ``touch`` config, or
    ``None`` when compensation is absent or disabled.

    Reads ``touch.coupling`` (the matrix) and the optional ``touch.compensation``
    tuning block (``enabled``, ``threshold_ut``, ``suppress_pct``)."""
    touch = touch or {}
    coupling = touch.get("coupling")
    tuning = touch.get("compensation") or {}
    if not coupling or not tuning.get("enabled", False):
        return None
    raw_deltas = coupling.get("deltas") or {}
    deltas: dict[int, list[float]] = {}
    for slot, row in raw_deltas.items():
        try:
            deltas[int(slot)] = [float(v) for v in row]
        except (TypeError, ValueError):
            continue
    if not deltas:
        return None
    sensor_count = int(coupling.get("sensor_count")
                       or touch.get("sensor_count", 0)
                       or max((len(r) for r in deltas.values()), default=0))
    suppress = tuning.get("suppress_pct")
    return TouchCompensator(
        deltas,
        sensor_count=sensor_count,
        ref_pct=float(coupling.get("ref_pct", 100.0)),
        threshold_ut=float(tuning.get("threshold_ut", DEFAULT_THRESHOLD_UT)),
        suppress_pct=None if suppress is None else float(suppress),
    )

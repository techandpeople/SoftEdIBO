"""Chamber fill-time calibration — GUI-free core + settings helpers.

The multiplexed pressure sensors are too slow/laggy to close the loop on the
pump in real time, so chambers are inflated for a **pre-measured time** instead.
This module measures that time once, using the pressure sensor as ground truth:
inflate a chamber from empty and record how long it takes to reach (near) its
maximum — that elapsed time becomes the chamber's ``fill_time_ms``.

Two safety limits always apply (mirroring the firmware): a hard ceiling of
``MAX_FILL_MS`` (5 s) and the firmware's own ``HARD_MAX`` pressure cutoff — so a
stuck/unplugged sensor can never run a pump indefinitely during calibration.

The :class:`FillTimeCalibrator` is deliberately Qt-free and clock-injectable so
it can be unit-tested; the Qt dialog (``src/gui/fill_calibration_dialog.py``)
drives it from gateway pressure messages and a timer.
"""

from __future__ import annotations

import time
from typing import Any, Callable

# Hardcoded safety ceiling, shared with the firmware. A fill that hasn't reached
# the target by now is capped here and flagged as timed out.
MAX_FILL_MS: float = 5000.0

# Fraction of the chamber max we consider "full" for timing. Slightly under 100%
# so sensor noise / the last asymptotic creep don't stall the measurement.
DEFAULT_TARGET_PCT: float = 95.0


class FillTimeCalibrator:
    """Times a single chamber inflating from empty to ``target_pct`` of its max.

    Usage (driven by the caller):
        cal = FillTimeCalibrator()
        cal.start()                      # caller opens the inflate valve/pump
        ... feed each pressure reading ...
        done = cal.update(pressure_pct)  # returns result_ms once reached/capped
        ... or call cal.tick() periodically to enforce the timeout ...

    ``clock`` returns seconds (monotonic); inject a fake one in tests.
    """

    def __init__(self, target_pct: float = DEFAULT_TARGET_PCT,
                 max_ms: float = MAX_FILL_MS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.target_pct = float(target_pct)
        self.max_ms = float(max_ms)
        self._clock = clock
        self._t0: float | None = None
        self.result_ms: float | None = None
        self.timed_out: bool = False

    def start(self) -> None:
        """Mark the inflate start (call right when the valve/pump opens)."""
        self._t0 = self._clock() * 1000.0
        self.result_ms = None
        self.timed_out = False

    @property
    def running(self) -> bool:
        return self._t0 is not None and self.result_ms is None

    def elapsed_ms(self) -> float:
        if self._t0 is None:
            return 0.0
        return self._clock() * 1000.0 - self._t0

    def update(self, pressure_pct: float) -> float | None:
        """Feed a pressure reading (0–100 %). Returns ``result_ms`` once the
        chamber reaches the target or the timeout caps it, else ``None``."""
        if not self.running:
            return self.result_ms
        elapsed = self.elapsed_ms()
        if elapsed >= self.max_ms:
            self.timed_out = True
            self.result_ms = self.max_ms
        elif pressure_pct >= self.target_pct:
            self.result_ms = elapsed
        return self.result_ms

    def tick(self) -> float | None:
        """Enforce the timeout when no new pressure readings are arriving."""
        if self.running and self.elapsed_ms() >= self.max_ms:
            self.timed_out = True
            self.result_ms = self.max_ms
        return self.result_ms


# ---------------------------------------------------------------------------
# Settings helpers (pure dict walks over ``Settings.data``)
# ---------------------------------------------------------------------------

# Node types that actuate chambers (and so have fill times to calibrate).
ACTUATOR_NODE_TYPES = ("node_direct", "node_multiplexed")


def _iter_robots(settings_data: dict) -> Any:
    """Yield every robot dict across the robots-by-kind buckets."""
    for bucket in (settings_data.get("robots") or {}).values():
        for robot in bucket or []:
            yield robot


def iter_actuator_chambers(settings_data: dict) -> list[dict]:
    """List configured chambers that can be calibrated, one entry per chamber.

    Each entry: ``{robot_id, skin_id, mac, slot, node_type, fill_time_ms}``
    (``fill_time_ms`` is ``None`` when not yet calibrated). Built by joining each
    skin's ``chambers`` to its node's ``node_type``."""
    out: list[dict] = []
    for robot in _iter_robots(settings_data):
        node_types = {n.get("mac"): n.get("node_type")
                      for n in (robot.get("nodes") or [])}
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                mac = ch.get("mac")
                nt = node_types.get(mac)
                if nt not in ACTUATOR_NODE_TYPES:
                    continue
                out.append({
                    "robot_id": robot.get("id", ""),
                    "skin_id": skin.get("skin_id", ""),
                    "mac": mac,
                    "slot": int(ch.get("slot", 0)),
                    "node_type": nt,
                    "fill_time_ms": ch.get("fill_time_ms"),
                })
    return out


def set_fill_time(settings_data: dict, mac: str, slot: int,
                  fill_time_ms: float | None) -> int:
    """Write ``fill_time_ms`` onto every chamber entry matching ``mac``+``slot``.

    Stored next to ``max_pressure`` on the chamber. ``None`` clears it. Returns
    the number of chamber entries updated."""
    n = 0
    for robot in _iter_robots(settings_data):
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                if ch.get("mac") == mac and int(ch.get("slot", 0)) == int(slot):
                    if fill_time_ms is None:
                        ch.pop("fill_time_ms", None)
                    else:
                        ch["fill_time_ms"] = int(round(fill_time_ms))
                    n += 1
    return n


def chambers_missing_fill_time(settings_data: dict) -> list[dict]:
    """Configured actuator chambers that have no ``fill_time_ms`` yet — used by
    the pre-activity guard to offer calibration."""
    return [c for c in iter_actuator_chambers(settings_data)
            if c["fill_time_ms"] is None]

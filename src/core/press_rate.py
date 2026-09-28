"""Per-sensor press rate (presses per second) from press onsets.

One small model shared by every display and logger that shows "how fast is
this sensor being pressed", so the skin grid, the live sensor window and the
session log can never disagree. Pure Python, no Qt; safe to feed from the
gateway thread and read from the GUI thread.
"""

from __future__ import annotations

import threading
import time

# A sensor's rate is forgotten after this long without a new press.
DEFAULT_STALE_MS = 10_000.0


class PressRateMeter:
    """Tracks the latest press interval of each sensor."""

    def __init__(self, stale_ms: float = DEFAULT_STALE_MS) -> None:
        self._lock = threading.Lock()
        self._stale_ms = float(stale_ms)
        self._last_press_ms: dict[int, float] = {}
        self._interval_ms: dict[int, float] = {}

    @property
    def stale_ms(self) -> float:
        return self._stale_ms

    def set_stale_ms(self, value: float) -> None:
        """How long a rate stays valid without a new press."""
        self._stale_ms = max(0.0, float(value))

    def record(self, sensor_idx: int, now_ms: float | None = None) -> float | None:
        """Record one press onset; return its interval to the sensor's
        previous press (ms), or None for the first press."""
        now_ms = time.monotonic() * 1000.0 if now_ms is None else float(now_ms)
        with self._lock:
            previous = self._last_press_ms.get(sensor_idx)
            self._last_press_ms[sensor_idx] = now_ms
            if previous is None or now_ms <= previous:
                return None
            interval = now_ms - previous
            self._interval_ms[sensor_idx] = interval
            return interval

    def frequency_hz(self, sensor_idx: int,
                     now_ms: float | None = None) -> float | None:
        """The sensor's latest rate, or None when unknown or stale."""
        return self.frequencies_hz(now_ms).get(sensor_idx)

    def frequencies_hz(self, now_ms: float | None = None) -> dict[int, float]:
        """Every sensor's latest non-stale rate."""
        now_ms = time.monotonic() * 1000.0 if now_ms is None else float(now_ms)
        with self._lock:
            return {idx: 1000.0 / interval
                    for idx, interval in self._interval_ms.items()
                    if now_ms - self._last_press_ms[idx] < self._stale_ms}

    def last_press_ms(self) -> dict[int, float]:
        """Monotonic time (ms) of each sensor's latest press."""
        with self._lock:
            return dict(self._last_press_ms)

    def reset(self) -> None:
        with self._lock:
            self._last_press_ms.clear()
            self._interval_ms.clear()

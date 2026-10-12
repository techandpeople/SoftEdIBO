"""What gets written about each touch press/release in the session log.

Pure Python, no Qt and no database: it pairs each release with its press and
turns both into the metadata dict stored with the logged event, so the fields
of the touch record are defined (and tested) in one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from src.core.press_rate import PressRateMeter


@dataclass(frozen=True)
class _OpenPress:
    monotonic_ms: float
    timestamp: datetime
    intensity_ut: float | None
    sensor_id: int | None


class TouchLog:
    """Builds the logged details of touch presses and releases.

    One instance per session. Times come from the caller (the moment the edge
    was detected); when absent, "now" is used, which is what a simulated
    chamber-button touch needs.
    """

    def __init__(self) -> None:
        self._open: dict[tuple[str, str, int], _OpenPress] = {}
        self._rates: dict[str, PressRateMeter] = {}

    def reset(self) -> None:
        self._open.clear()
        self._rates.clear()

    @staticmethod
    def _key(skin_id: str, chamber_id: int,
             sensor_id: int | None) -> tuple[str, str, int]:
        # Several sensors may share a chamber; keep their presses apart.
        if sensor_id is None:
            return (skin_id, "chamber", chamber_id)
        return (skin_id, "sensor", sensor_id)

    def press(self, skin_id: str, chamber_id: int, *,
              sensor_id: int | None = None,
              intensity_ut: float | None = None,
              timestamp: datetime | None = None,
              monotonic_ms: float | None = None) -> dict:
        """Record a press onset; return its event metadata."""
        now_ms = (time.monotonic() * 1000.0 if monotonic_ms is None
                  else float(monotonic_ms))
        stamp = timestamp or datetime.now()
        rate_key = sensor_id if sensor_id is not None else chamber_id
        interval_ms = self._rates.setdefault(
            skin_id, PressRateMeter()).record(rate_key, now_ms)
        self._open[self._key(skin_id, chamber_id, sensor_id)] = _OpenPress(
            now_ms, stamp, intensity_ut, sensor_id)
        details: dict = {}
        if sensor_id is not None:
            details["sensor_id"] = sensor_id
        if intensity_ut is not None:
            details["intensity_ut"] = round(intensity_ut, 1)
        if interval_ms is not None:
            details["touch_interval_ms"] = round(interval_ms)
            details["frequency_hz"] = round(1000.0 / interval_ms, 3)
        return details

    def release(self, skin_id: str, chamber_id: int, *,
                sensor_id: int | None = None,
                intensity_ut: float | None = None,
                peak_ut: float | None = None,
                monotonic_ms: float | None = None) -> dict:
        """Record a release; return its event metadata (empty when the press
        was never seen, e.g. the session started mid-touch)."""
        started = self._open.pop(self._key(skin_id, chamber_id, sensor_id), None)
        if started is None:
            return {}
        now_ms = (time.monotonic() * 1000.0 if monotonic_ms is None
                  else float(monotonic_ms))
        details: dict = {
            "duration_ms": round(max(0.0, now_ms - started.monotonic_ms)),
            "press_timestamp": started.timestamp.isoformat(
                timespec="milliseconds"),
        }
        if started.sensor_id is not None:
            details["sensor_id"] = started.sensor_id
        if started.intensity_ut is not None:
            details["press_intensity_ut"] = round(started.intensity_ut, 1)
        if intensity_ut is not None:
            details["release_intensity_ut"] = round(intensity_ut, 1)
        if peak_ut is not None:
            details["peak_intensity_ut"] = round(peak_ut, 1)
        return details

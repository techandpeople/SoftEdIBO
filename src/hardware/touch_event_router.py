"""TouchEventRouter - turns a magnet board's raw sensor stream into
chamber-level press/release events.

The magnet board (a real ``node_magnet_sensor`` ESP32 or a
``SimulatedMagnetSensor``) streams the *set of currently active sensors* in
each ``on_magnet`` message. This router edge-detects that set - a sensor
entering it is a press, one leaving it a release - and maps each sensor to a
skin-local chamber index, so consumers work in chamber terms rather than sensor
terms.

It lives in the hardware/domain layer and owns no Qt: callbacks fire on
whichever thread the controller delivers ``on_magnet`` (the gateway thread on
real hardware), so GUI consumers must marshal to the UI thread themselves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from src.hardware.node_clock import NodeClock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TouchEdge:
    """One press or release of one sensor, stamped where it was detected.

    The times are those of the sensor sample itself when the node stamps its
    frames (see :class:`NodeClock`), else of the frame's arrival on the gateway
    thread. Either way they are not blurred by GUI event-loop latency.
    """

    chamber_id: int
    action: str                 # "press" | "release"
    sensor_idx: int
    intensity_ut: float | None  # magnitude at the edge sample
    peak_ut: float | None       # release only: strongest sample of the press
    timestamp: datetime         # wall clock, for the record
    monotonic_ms: float         # monotonic clock, for intervals/durations


class TouchEventRouter:
    """Edge-detects sensor press/release and maps sensors to chambers."""

    def __init__(self, sensor_to_chamber: dict[int, int], *, name: str = "") -> None:
        self._sensor_to_chamber = dict(sensor_to_chamber)
        self._name = name
        self._cbs: list[Callable[[int, str], None]] = []
        self._detail_cbs: list[Callable[[int, str, int, float | None], None]] = []
        self._edge_cbs: list[Callable[[TouchEdge], None]] = []
        self._active: set[int] = set()
        # Strongest magnitude seen so far in each sensor's current press.
        self._peaks: dict[int, float] = {}
        # Places node-stamped samples on the PC clock (frames without a node
        # stamp - old firmware, simulation - use their arrival time).
        self._clock = NodeClock()

    @property
    def frames_lost(self) -> int:
        """Stream frames the node sent that never arrived."""
        return self._clock.frames_lost

    @classmethod
    def from_touch_config(cls, touch: dict[str, Any] | None,
                          chamber_count: int, *, name: str = "") -> "TouchEventRouter":
        """Build a router from a skin's ``touch`` config, resolving the
        sensor->chamber map from ``touch.sensor_to_chamber`` and falling back to a
        1:1 mapping (the same convention used by the activity layer)."""
        return cls(cls._mapping_from_config(touch, chamber_count), name=name)

    @staticmethod
    def _mapping_from_config(touch: dict[str, Any] | None,
                             chamber_count: int) -> dict[int, int]:
        touch = touch or {}
        raw = touch.get("sensor_to_chamber")
        if isinstance(raw, dict) and raw:
            mapping: dict[int, int] = {}
            for k, v in raw.items():
                try:
                    mapping[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
            return mapping
        sensor_count = int(touch.get("sensor_count", chamber_count) or 0)
        return {i: i for i in range(min(chamber_count, sensor_count))}

    def subscribe(self, callback: Callable[[int, str], None]) -> None:
        """Register ``callback(chamber_id, action)`` for each press/release."""
        self._cbs.append(callback)

    def unsubscribe(self, callback: Callable[[int, str], None]) -> None:
        """Deregister a callback passed to :meth:`subscribe` (no-op if absent)."""
        self._cbs[:] = [cb for cb in self._cbs if cb != callback]

    def subscribe_detail(self, callback: Callable[[int, str, int, float | None], None]) -> None:
        """Register ``callback(chamber, action, sensor, intensity_ut)``.

        This supplements, rather than changes, the original two-argument API
        so existing displays and simulations remain compatible.
        """
        self._detail_cbs.append(callback)

    def unsubscribe_detail(self, callback: Callable[[int, str, int, float | None], None]) -> None:
        self._detail_cbs[:] = [cb for cb in self._detail_cbs if cb != callback]

    def subscribe_edge(self, callback: Callable[[TouchEdge], None]) -> None:
        """Register ``callback(edge)`` for each press/release, with the time
        the edge was detected and (on release) the press's peak magnitude."""
        self._edge_cbs.append(callback)

    def unsubscribe_edge(self, callback: Callable[[TouchEdge], None]) -> None:
        self._edge_cbs[:] = [cb for cb in self._edge_cbs if cb != callback]

    def attach(self, touch_controller: Any) -> None:
        """Start consuming ``on_magnet`` events from ``touch_controller`` (a no-op
        when it is missing or exposes no ``on_magnet``)."""
        if touch_controller is not None and hasattr(touch_controller, "on_magnet"):
            touch_controller.on_magnet(self.handle_magnet)

    def handle_magnet(self, data: dict[str, Any]) -> None:
        """Process one magnet message: diff its ``act`` set against the last to
        emit a press per newly-active sensor and a release per departed one."""
        active = data.get("act") or []
        if not isinstance(active, list):
            return
        new_set: set[int] = set()
        for raw in active:
            try:
                new_set.add(int(raw))
            except (TypeError, ValueError):
                continue
        now, now_ms = self._sample_time(data)
        magnitudes = data.get("mag")

        def intensity(sensor_idx: int) -> float | None:
            if (not isinstance(magnitudes, (list, tuple))
                    or sensor_idx >= len(magnitudes)):
                return None
            try:
                return float(magnitudes[sensor_idx])
            except (TypeError, ValueError):
                return None

        for sensor_idx in new_set - self._active:
            value = intensity(sensor_idx)
            if value is not None:
                self._peaks[sensor_idx] = value
            self._dispatch(sensor_idx, "press", value, None, now, now_ms)
        for sensor_idx in new_set & self._active:
            value = intensity(sensor_idx)
            if value is not None:
                self._peaks[sensor_idx] = max(
                    value, self._peaks.get(sensor_idx, value))
        for sensor_idx in self._active - new_set:
            self._dispatch(sensor_idx, "release", intensity(sensor_idx),
                           self._peaks.pop(sensor_idx, None), now, now_ms)
        self._active = new_set

    def _sample_time(self, data: dict[str, Any]) -> tuple[datetime, float]:
        """Wall-clock and monotonic (ms) time of this frame's sample."""
        wall = datetime.now()
        arrival_ms = time.monotonic() * 1000.0
        node_ms = data.get("t")
        if isinstance(node_ms, bool) or not isinstance(node_ms, (int, float)):
            return wall, arrival_ms
        count = data.get("n")
        sample_ms = self._clock.observe(
            node_ms, arrival_ms,
            count if isinstance(count, int) and not isinstance(count, bool)
            else None)
        return wall - timedelta(milliseconds=arrival_ms - sample_ms), sample_ms

    def _dispatch(self, sensor_idx: int, action: str, intensity_ut: float | None,
                  peak_ut: float | None, now: datetime, now_ms: float) -> None:
        chamber_id = self._sensor_to_chamber.get(sensor_idx, sensor_idx)
        edge = TouchEdge(chamber_id, action, sensor_idx, intensity_ut, peak_ut,
                         now, now_ms)
        self._call_all(self._cbs, "touch_event", chamber_id, action)
        self._call_all(self._detail_cbs, "detailed touch_event",
                       chamber_id, action, sensor_idx, intensity_ut)
        self._call_all(self._edge_cbs, "touch edge", edge)

    def _call_all(self, callbacks: list, what: str, *args: Any) -> None:
        """Call every subscriber; prune the ones whose Qt object is gone."""
        dead: list = []
        for cb in list(callbacks):   # snapshot: listeners change on the GUI thread
            try:
                cb(*args)
            except RuntimeError:     # Qt signal source deleted (widget gone)
                dead.append(cb)
            except Exception:   # noqa: BLE001 - a bad subscriber must not break others
                logger.exception("%s callback failed (%s)", what, self._name)
        if dead:
            callbacks[:] = [cb for cb in callbacks
                            if all(cb is not d for d in dead)]

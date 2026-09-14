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
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TouchEventRouter:
    """Edge-detects sensor press/release and maps sensors to chambers."""

    def __init__(self, sensor_to_chamber: dict[int, int], *, name: str = "") -> None:
        self._sensor_to_chamber = dict(sensor_to_chamber)
        self._name = name
        self._cbs: list[Callable[[int, str], None]] = []
        self._active: set[int] = set()

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
        for sensor_idx in new_set - self._active:
            self._dispatch(sensor_idx, "press")
        for sensor_idx in self._active - new_set:
            self._dispatch(sensor_idx, "release")
        self._active = new_set

    def _dispatch(self, sensor_idx: int, action: str) -> None:
        chamber_id = self._sensor_to_chamber.get(sensor_idx, sensor_idx)
        dead: list = []
        for cb in list(self._cbs):      # snapshot: listeners change on the GUI thread
            try:
                cb(chamber_id, action)
            except RuntimeError:        # Qt signal source deleted (widget gone) - prune it
                dead.append(cb)
            except Exception:   # noqa: BLE001 - a bad subscriber must not break others
                logger.exception("touch_event callback failed (%s)", self._name)
        if dead:
            self._cbs[:] = [cb for cb in self._cbs if all(cb is not d for d in dead)]

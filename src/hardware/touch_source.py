"""PC-side magnet touch detection and pressure-informed compensation.

Wraps a node's raw magnet controller and re-emits each ``type:"magnet"`` message
with the actuation offset removed (see :class:`src.core.touch_compensation.
TouchCompensator`). The Skin exposes this as ``skin.touch_source`` so the live
*detection* consumers (activities, gesture ML, the skin's own QuadrantDetector +
TouchEventRouter) see compensated data, while the raw controller stays available
for the stream recorder, the live monitor, and coupling calibration.

It only implements ``on_magnet`` - the one method detection consumers use; for
everything else (rebaseline, geometry, organ events) callers keep using the real
controller via ``skin.touch_controller``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Sequence

from src.core.touch_compensation import DEFAULT_THRESHOLD_UT, TouchCompensator

logger = logging.getLogger(__name__)


def subscribe_skin_magnet(skin: Any,
                          callback: Callable[[dict[str, Any]], None]) -> bool:
    """Subscribe ``callback`` to a skin's compensated magnet stream.

    Prefers ``Skin.on_magnet`` (the pressure-compensated detection source);
    falls back to the raw ``skin.touch_controller.on_magnet`` for objects that
    predate it (e.g. test doubles). Returns True if a subscription was made. This
    is the single entry point detection consumers (activities, gesture ML) should
    use so they all see compensated readings consistently."""
    skin_on_magnet = getattr(skin, "on_magnet", None)
    if callable(skin_on_magnet) and skin_on_magnet(callback):
        return True
    ctrl = getattr(skin, "touch_controller", None)
    raw = getattr(ctrl, "on_magnet", None) if ctrl is not None else None
    if raw is not None:
        raw(callback)
        return True
    return False


def unsubscribe_skin_magnet(skin: Any,
                            callback: Callable[[dict[str, Any]], None]) -> None:
    """Undo :func:`subscribe_skin_magnet` (a no-op if it was never subscribed)."""
    for owner in (getattr(skin, "touch_source", None),
                  getattr(skin, "touch_controller", None)):
        remove = getattr(owner, "remove_magnet_listener", None)
        if remove is not None:
            remove(callback)


class CompensatedMagnetSource:
    """Subscribes once to raw magnet data and derives touch state on the PC."""

    def __init__(self, controller: Any, compensator: TouchCompensator | None,
                 level_provider: Callable[[], Mapping[int, float]],
                 threshold_ut: float | Sequence[float] = DEFAULT_THRESHOLD_UT
                 ) -> None:
        self._ctrl = controller
        self._comp = compensator
        self._levels = level_provider
        self._thresholds_ut = self._normalise_thresholds(threshold_ut)
        self._subs: list[Callable[[dict[str, Any]], None]] = []
        self._attached = False

    def on_magnet(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for compensated ``type:"magnet"`` messages.

        The first subscription lazily attaches to the underlying controller, so a
        source with no listeners adds no work to the gateway thread."""
        self._subs.append(callback)
        if not self._attached and hasattr(self._ctrl, "on_magnet"):
            self._attached = True
            self._ctrl.on_magnet(self._handle)

    def remove_magnet_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Deregister a callback passed to :meth:`on_magnet` (no-op if absent)."""
        self._subs[:] = [cb for cb in self._subs if cb != callback]

    def set_threshold_ut(self, value: float) -> None:
        """Retune the PC activation threshold (uT) at runtime.

        When compensation is enabled, its residual activity threshold follows
        the same value.
        """
        self._thresholds_ut = [float(value)] * len(self._thresholds_ut)
        if self._comp is not None:
            self._comp.threshold_ut = float(value)

    def set_thresholds_ut(self, values: Sequence[float]) -> None:
        """Set the per-sensor PC activation thresholds (uT)."""
        self._thresholds_ut = self._normalise_thresholds(values)

    @staticmethod
    def _normalise_thresholds(
            values: float | Sequence[float]) -> list[float]:
        if isinstance(values, (int, float)):
            return [float(values)]
        thresholds = [float(value) for value in values]
        return thresholds or [DEFAULT_THRESHOLD_UT]

    def _derive_active(self, data: dict[str, Any]) -> dict[str, Any]:
        out = dict(data)
        magnitudes = data.get("mag")
        if not isinstance(magnitudes, (list, tuple)):
            out["act"] = []
            return out
        out["act"] = []
        for index, value in enumerate(magnitudes):
            try:
                threshold = (self._thresholds_ut[index]
                             if index < len(self._thresholds_ut)
                             else self._thresholds_ut[-1])
                if float(value) >= threshold:
                    out["act"].append(index)
            except (TypeError, ValueError):
                continue
        out["pc_detected"] = True
        return out

    def _handle(self, data: dict[str, Any]) -> None:
        try:
            if self._comp is None:
                out = self._derive_active(data)
            else:
                out = self._comp.apply(data, self._levels(),
                                       now_ms=time.monotonic() * 1000.0)
                out = self._derive_active(out)
        except Exception:   # noqa: BLE001 - never let one bad reading kill the stream
            logger.exception("touch compensation failed; deriving activity from raw uT")
            out = self._derive_active(data)
        dead: list[int] = []
        for i, cb in enumerate(self._subs):
            try:
                cb(out)
            except RuntimeError:        # Qt signal source deleted - prune it
                dead.append(cb)
            except Exception:           # noqa: BLE001 - a bad subscriber must not break others
                logger.exception("compensated magnet callback failed")
        if dead:
            self._subs[:] = [cb for cb in self._subs if all(cb is not d for d in dead)]

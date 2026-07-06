"""CompensatedMagnetSource — a magnet stream with pressure-informed compensation.

Wraps a node's raw magnet controller and re-emits each ``type:"magnet"`` message
with the actuation offset removed (see :class:`src.core.touch_compensation.
TouchCompensator`). The Skin exposes this as ``skin.touch_source`` so the live
*detection* consumers (activities, gesture ML, the skin's own QuadrantDetector +
TouchEventRouter) see compensated data, while the raw controller stays available
for the stream recorder, the live monitor, and coupling calibration.

It only implements ``on_magnet`` — the one method detection consumers use; for
everything else (rebaseline, geometry, organ events) callers keep using the real
controller via ``skin.touch_controller``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

from src.core.touch_compensation import TouchCompensator

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


class CompensatedMagnetSource:
    """Subscribes once to a raw controller's magnet stream, compensates each
    message using live chamber levels, and fans it out to its own subscribers."""

    def __init__(self, controller: Any, compensator: TouchCompensator,
                 level_provider: Callable[[], Mapping[int, float]]) -> None:
        self._ctrl = controller
        self._comp = compensator
        self._levels = level_provider
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

    def set_threshold_ut(self, value: float) -> None:
        """Retune the compensator's activation threshold (µT) at runtime.

        The compensated stream rederives ``act`` from the residual magnitudes at
        its own threshold, so when a new sensitivity is pushed to the node the
        compensator must follow — otherwise the node and the compensated stream
        would disagree about what counts as a touch."""
        self._comp.threshold_ut = float(value)

    def _handle(self, data: dict[str, Any]) -> None:
        try:
            out = self._comp.apply(data, self._levels(),
                                   now_ms=time.monotonic() * 1000.0)
        except Exception:   # noqa: BLE001 — never let one bad reading kill the stream
            logger.exception("touch compensation failed; passing raw")
            out = data
        dead: list[int] = []
        for i, cb in enumerate(self._subs):
            try:
                cb(out)
            except RuntimeError:        # Qt signal source deleted — prune it
                dead.append(i)
            except Exception:           # noqa: BLE001 — a bad subscriber must not break others
                logger.exception("compensated magnet callback failed")
        for i in reversed(dead):
            self._subs.pop(i)

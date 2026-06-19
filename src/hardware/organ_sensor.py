"""OrganSensor — interprets a node's organ-resistance stream.

Sits between a controller (real ``ESP32Controller`` or ``SimulatedController``)
and whoever cares about organs (activities, GUI panels). The controller layer
delivers raw readings — ``float`` ohms, ``float("inf")`` for an open circuit;
this class turns them into two clean, separate event streams:

- **cover events** — the silicone cover closing/opening the sensing circuit
  (``inf`` ⇄ finite transitions);
- **resistance events** — the organ network's value while the cover is on.

Keeping this interpretation out of the activities means every consumer agrees
on what "cover off" means and activities only deal with domain events.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

logger = logging.getLogger(__name__)


class OrganSensor:
    """Tracks cover state + organ resistance for ONE organ circuit.

    Args:
        controller: Any controller exposing ``on_organ(cb)`` with the
            ``cb(resistance_ohm: float, slot: int)`` contract
            (``inf`` = cover off).
        slot: Which of the controller's organ circuits this sensor follows.
            Direct nodes have a single circuit (slot 0); multiplexed nodes
            expose one slot per configured ``organ_channels`` entry — e.g.
            one per Tree branch.
    """

    def __init__(self, controller: Any, slot: int = 0):
        self._controller = controller
        self._slot = int(slot)
        self._resistance: float = math.inf      # last reading; inf = open
        self._seen_reading = False
        self._cover_callbacks: list[Callable[[bool], None]] = []
        self._resistance_callbacks: list[Callable[[float], None]] = []
        on_organ = getattr(controller, "on_organ", None)
        if on_organ is not None:
            on_organ(self._handle_reading)
        else:
            logger.debug("Controller %r has no on_organ — OrganSensor inert",
                         controller)

    @property
    def slot(self) -> int:
        """The organ-circuit slot this sensor follows on its controller."""
        return self._slot

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def resistance_ohm(self) -> float:
        """Last reported total resistance (Ω); ``inf`` while the cover is off."""
        return self._resistance

    @property
    def cover_closed(self) -> bool | None:
        """True/False once a reading arrived; None before the first one."""
        if not self._seen_reading:
            return None
        return not math.isinf(self._resistance)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def on_cover(self, callback: Callable[[bool], None]) -> None:
        """``callback(closed)`` fired only when the cover state flips."""
        self._cover_callbacks.append(callback)

    def on_resistance(self, callback: Callable[[float], None]) -> None:
        """``callback(resistance_ohm)`` fired for each finite reading change
        (i.e. while the cover is on)."""
        self._resistance_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_reading(self, resistance_ohm: float, slot: int = 0) -> None:
        if int(slot) != self._slot:
            return
        try:
            value = float(resistance_ohm)
        except (TypeError, ValueError):
            return
        was_closed = self.cover_closed
        self._resistance = value
        self._seen_reading = True
        now_closed = not math.isinf(value)

        if now_closed != was_closed:   # includes the None → first-reading edge
            for cb in self._cover_callbacks:
                cb(now_closed)
        if now_closed:
            for cb in self._resistance_callbacks:
                cb(value)

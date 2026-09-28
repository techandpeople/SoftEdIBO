"""BenchHolds - the Hold mode of a bench tool (Test Actuators), per chamber.

Hold is a MODE the user switches on or off per chamber; nothing else flips it:

* **On:** the chamber is kept at the level it reaches. Switching it on while
  the chamber already sits at a level holds it right away; after every
  Inflate / Deflate it holds the new level once the chamber settles. An
  actuation does not turn the mode off - it only pauses the hold while the
  chamber moves.
* **Off:** no hold at all; the valves stay closed and the chamber may leak.

STOP ALL, a vent or a continuous run end the hold that is running but keep
the mode, so the next actuation holds again once it settles.

Holds go through the node's :class:`~src.hardware.esp32_controller.ESP32Controller`
as ``bench`` holds: the controller's HoldKeeper is the one place that keeps
them alive, and while the bench has claimed the node the app's own automatic
holds are refused - so two owners can no longer fight over one chamber.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from src.hardware.hold_duty import HOLD_VACUUM, hold_direction


class HoldController(Protocol):
    """The slice of ESP32Controller this class drives."""

    def start_hold(self, chamber: int, duty: int, kpa: float | None = None,
                   timed: bool = False, vacuum: bool = False,
                   bench: bool = False) -> bool: ...

    def stop_hold(self, chamber: int | None = None) -> None: ...

    def active_holds(self) -> list[int]: ...


class BenchHolds:
    """Per-chamber Hold mode for a bench tool.

    Args:
        ctrl: The node's controller (holds are started as ``bench`` holds).
        level_kpa: Last reported kPa of a chamber (None = no status yet).
        level_pct: A chamber's level as % of its configured range.
        seed_duty: Seed PWM for a hold of a chamber at a kPa (0 = let the
            node predict its own).
        clock: Monotonic clock (injectable for tests).
    """

    # A one-shot actuation counts as finished when the node reports the
    # chamber idle after having been seen moving, or - if the fill was too
    # short for a status frame to catch it moving - idle this long after the
    # command went out.
    SETTLE_S = 1.5
    # A pressure pose at or under this (% of the chamber's range) is empty:
    # nothing to hold. A vacuum pose (below ambient) is always held.
    EMPTY_PCT = 5

    def __init__(self, ctrl: HoldController, *,
                 level_kpa: Callable[[int], float | None],
                 level_pct: Callable[[int, float], int],
                 seed_duty: Callable[[int, float], int],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ctrl = ctrl
        self._level_kpa = level_kpa
        self._level_pct = level_pct
        self._seed_duty = seed_duty
        self._clock = clock
        self._mode: set[int] = set()
        # slot -> {"since": t, "moving": bool} for an actuation under way.
        self._pending: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def is_on(self, slot: int) -> bool:
        return slot in self._mode

    def set_mode(self, slot: int, on: bool) -> None:
        """The user switched a chamber's Hold mode."""
        if on:
            self._mode.add(slot)
            if slot not in self._pending:
                self.hold_now(slot)
        else:
            self._mode.discard(slot)
            self._pending.pop(slot, None)
            self._ctrl.stop_hold(slot)

    # ------------------------------------------------------------------
    # Events from the bench tool
    # ------------------------------------------------------------------

    def actuation_started(self, slots: list[int]) -> None:
        """An Inflate / Deflate went out on ``slots``: pause their holds and,
        for chambers in Hold mode, hold the new level once they settle."""
        now = self._clock()
        for slot in slots:
            self._ctrl.stop_hold(slot)
            if slot in self._mode:
                self._pending[slot] = {"since": now, "moving": False}

    def actuation_state(self, slot: int, moving: bool) -> None:
        """Node status for a chamber: moving (inflating/deflating) or idle."""
        pending = self._pending.get(slot)
        if pending is None:
            return
        if moving:
            pending["moving"] = True
            return
        if not pending["moving"] and self._clock() - pending["since"] < self.SETTLE_S:
            return
        self._pending.pop(slot, None)
        if slot in self._mode:
            self.hold_now(slot)

    def interrupt(self, slot: int | None = None) -> None:
        """A vent, continuous run or STOP took the chamber (every chamber
        when None): end its hold, keep the mode."""
        if slot is None:
            self._pending.clear()
        else:
            self._pending.pop(slot, None)
        self._ctrl.stop_hold(slot)

    def release(self) -> None:
        """The bench tool is closing: every hold ends, every mode is off."""
        self._pending.clear()
        self._mode.clear()
        self._ctrl.stop_hold()

    # ------------------------------------------------------------------
    # Holding
    # ------------------------------------------------------------------

    def hold_now(self, slot: int) -> bool:
        """Hold the chamber at the level it reports now. False when there is
        nothing to hold (no reading yet, at ambient, or an empty pressure pose)."""
        kpa = self._level_kpa(slot)
        if kpa is None:
            return False
        side = hold_direction(kpa)
        if side is None:
            return False
        if side != HOLD_VACUUM and self._level_pct(slot, kpa) <= self.EMPTY_PCT:
            return False
        return self._ctrl.start_hold(slot, self._seed_duty(slot, kpa), kpa=kpa,
                                     vacuum=side == HOLD_VACUUM, bench=True)

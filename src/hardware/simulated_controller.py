"""SimulatedController — mock chamber actuator for simulation.

Pure **chamber actuation**: accepts inflate / deflate / set_pressure commands
(the *targets*) and animates the chamber pressure locally toward them at a
configurable rate, firing ``on_pressure`` callbacks exactly as real hardware
would report back the *actual* pressure. This lets the widget / activity layer
stay hardware-agnostic — swap in an ``ESP32Controller`` and behaviour is
identical.

Touch / magnet sensor sensing lives in :class:`~src.hardware.simulated_magnet_sensor.SimulatedMagnetSensor`,
not here, so the two hardware roles (sense vs actuate) stay separate.

Session-level concerns (pause, freeze) are handled by SimulatedRobot.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

# Defaults for the tunable simulation knobs. Each ``SimulatedController``
# overrides these from the activity's ``sim_params`` dict when constructed.
# The rate models the pump speed — real motors differ, so it is configurable.
_DEFAULT_INFLATE_PCT_PER_S = 33    # → step ~3 every 100 ms tick
_DEFAULT_DEFLATE_PCT_PER_S = 33

# Internal tick cadence — fixed at 100 ms. The configurable speeds set the
# step size per tick so the user can dial the rate without changing the timer.
_TICK_MS = 100


class SimulatedController(QObject):
    """Mock chamber actuator — animates pressure toward targets at a set rate."""

    def __init__(
        self,
        mac_address: str,
        parent: QObject | None = None,
        *,
        sim_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.mac_address = mac_address
        self._targets:  dict[int, int] = {}
        self._current:  dict[int, int] = {}
        self._max_pressures: dict[int, float] = {}
        self._pressure_callbacks: list[Callable[[int, int], None]] = []
        self._target_callbacks:   list[Callable[[int, int], None]] = []
        self._organ_callbacks:    list[Callable[[float, int], None]] = []
        # Simulated organ networks per slot: None = open circuit (cover off).
        self._organ_resistance: dict[int, float | None] = {}

        # Tunable knobs from the activity preset (Param defaults in
        # BaseActivity.SIM_PARAMS). Values converted from "%/s" to per-tick
        # step sizes so the fixed-rate timer can stay simple. The deflate /
        # inflate rates model real pumps — slower or faster motors — and are
        # configurable so simulation matches the eventual hardware.
        params = sim_params or {}
        self._inflate_step = max(1, round(int(
            params.get("sim_inflate_speed_pct_per_s",
                       _DEFAULT_INFLATE_PCT_PER_S)
        ) * _TICK_MS / 1000))
        self._deflate_step = max(1, round(int(
            params.get("sim_deflate_speed_pct_per_s",
                       _DEFAULT_DEFLATE_PCT_PER_S)
        ) * _TICK_MS / 1000))

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    def set_max_pressure(self, chamber: int, max_p: float) -> None:
        """Set max pressure for a chamber in kPa (used by Skin to propagate config)."""
        self._max_pressures[chamber] = float(max_p)

    @property
    def is_connected(self) -> bool:
        return True

    def inflate(self, chamber: int, delta: int = 10) -> bool:
        """Inflate by delta % (relative to current target)."""
        self._current.setdefault(chamber, 0)
        base = self._targets.get(chamber, self._current[chamber])
        new_target = min(100, base + delta)
        self._targets[chamber] = new_target
        for cb in self._target_callbacks:
            cb(chamber, new_target)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def deflate(self, chamber: int, delta: int = 10) -> bool:
        """Deflate by delta % (relative to current target)."""
        self._current.setdefault(chamber, 0)
        base = self._targets.get(chamber, self._current[chamber])
        new_target = max(0, base - delta)
        self._targets[chamber] = new_target
        for cb in self._target_callbacks:
            cb(chamber, new_target)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def set_pressure(self, chamber: int, value: int) -> bool:
        """Set absolute target pressure (clamped to chamber limits)."""
        value = max(0, min(100, value))
        self._current.setdefault(chamber, 0)
        self._targets[chamber] = value
        for cb in self._target_callbacks:
            cb(chamber, value)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def hold(self, chamber: int) -> bool:
        """Freeze this chamber at its current pressure."""
        current = self._current.get(chamber, 0)
        self._targets[chamber] = current
        for cb in self._target_callbacks:
            cb(chamber, current)
        return True

    def send_command(self, command: str, **kwargs) -> bool:
        if command == "inflate":
            return self.inflate(kwargs["chamber"], kwargs.get("delta", 10))
        if command == "deflate":
            return self.deflate(kwargs["chamber"], kwargs.get("delta", 10))
        if command == "set_pressure":
            return self.set_pressure(kwargs["chamber"], kwargs.get("value", 50))
        if command == "hold":
            return self.hold(kwargs["chamber"])
        return True

    def on_pressure(self, callback: Callable[[int, int], None]) -> None:
        self._pressure_callbacks.append(callback)

    def on_target(self, callback: Callable[[int, int], None]) -> None:
        """Register callback fired whenever a target pressure changes (chamber_id, target)."""
        self._target_callbacks.append(callback)

    def on_organ(self, callback: Callable[[float, int], None]) -> None:
        """Register a callback for simulated organ-resistance readings.

        Same contract as ``ESP32Controller.on_organ``: called with
        ``(resistance_ohm, slot)``; ``float("inf")`` when the cover is off.
        """
        self._organ_callbacks.append(callback)

    def sim_set_organ(self, resistance_ohm: float | None, slot: int = 0) -> None:
        """Drive a simulated organ circuit from the GUI / tests.

        ``None`` simulates the cover being off (open circuit); a float is the
        total parallel resistance of the plugged-in organs with the cover on.
        Fires ``on_organ`` callbacks exactly like real hardware would.
        """
        self._organ_resistance[slot] = resistance_ohm
        value = float("inf") if resistance_ohm is None else float(resistance_ohm)
        for cb in self._organ_callbacks:
            cb(value, slot)

    def set_led(self, color: str, pattern: str = "solid",
                period_ms: int = 0, count: int | None = None) -> bool:
        """No-op shim — simulation has no WS2818 strip but activities call
        this on enter/exit so we accept and log to keep the code paths
        symmetric with real hardware."""
        logger.debug("SIM set_led(%s, pattern=%s, period=%dms, count=%s)",
                     color, pattern, period_ms, count)
        return True

    def stop_all(self) -> None:
        """Stop the animation timer. Call on cleanup or pause."""
        self._timer.stop()

    def _tick(self) -> None:
        still_moving = False
        for chamber_id, target in self._targets.items():
            current = self._current.get(chamber_id, 0)
            if current == target:
                continue
            still_moving = True
            if current < target:
                new_val = min(target, current + self._inflate_step)
            else:
                new_val = max(target, current - self._deflate_step)
            self._current[chamber_id] = new_val
            for cb in self._pressure_callbacks:
                cb(chamber_id, new_val)
        if not still_moving:
            self._timer.stop()

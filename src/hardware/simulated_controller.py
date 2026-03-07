"""SimulatedController — mock ESP32 controller for the Simulation activity.

Accepts inflate/deflate commands and animates pressure locally,
firing pressure callbacks exactly as real hardware would.
This makes the widget layer completely hardware-agnostic.

Session-level concerns (pause, freeze) are handled by SimulatedRobot,
not here. This class is a dumb hardware mock.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer

from src.hardware.touch_sensor import SensorType, TouchSensor


class SimulatedController(QObject):
    """Mock ESP32 controller — responds to inflate/deflate with local pressure animation."""

    _SIM_STEP = 10    # % per tick (300 ms → ~6%/tick)
    _TICK_MS  = 300
    _RAMP_STEP_MS   = 50   # target ramp step interval
    _RAMP_TARGET_STEP = 25  # % per ramp step
    _TOUCH_INFLATE_MULTIPLIER = 1  # deflate starts after hold_ms × this + 300 ms

    def __init__(self, mac_address: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.mac_address = mac_address
        self._targets:  dict[int, int] = {}
        self._current:  dict[int, int] = {}
        self._pressure_callbacks: list[Callable[[int, int], None]] = []
        self._target_callbacks:   list[Callable[[int, int], None]] = []
        self._touch_callbacks:    list[Callable[[int, int], None]] = []
        self._touch_sensors:      dict[int, TouchSensor] = {}
        self._ramp_timers:        dict[int, QTimer] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def is_connected(self) -> bool:
        return True

    def inflate(self, chamber: int, delta: int = 10) -> bool:
        """Inflate by delta % (relative to current pressure)."""
        current = self._current.setdefault(chamber, 0)
        new_target = min(100, current + delta)
        self._targets[chamber] = new_target
        for cb in self._target_callbacks:
            cb(chamber, new_target)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def deflate(self, chamber: int, delta: int = 10) -> bool:
        """Deflate by delta % (relative to current pressure)."""
        current = self._current.setdefault(chamber, 0)
        new_target = max(0, current - delta)
        self._targets[chamber] = new_target
        for cb in self._target_callbacks:
            cb(chamber, new_target)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def set_pressure(self, chamber: int, value: int) -> bool:
        """Set absolute target pressure (0-100 %)."""
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
        self._cancel_ramp(chamber)
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

    def on_touch(self, callback: Callable[[int, int], None]) -> None:
        self._touch_callbacks.append(callback)

    def fire_touch(self, sensor_id: int, raw_value: int) -> None:
        """Simulate a touch sensor reading, firing callbacks if state changes."""
        if sensor_id not in self._touch_sensors:
            self._touch_sensors[sensor_id] = TouchSensor(
                sensor_id=sensor_id,
                sensor_type=SensorType.CAPACITIVE_COPPER,
                esp32_mac=self.mac_address,
                pin=sensor_id,
            )
        sensor = self._touch_sensors[sensor_id]
        if sensor.update(raw_value):
            for cb in self._touch_callbacks:
                cb(sensor_id, raw_value)

    def simulate_touch_press(self, chamber_id: int) -> None:
        """Start a gradual inflate ramp while touch is held."""
        self._cancel_ramp(chamber_id)

        timer = QTimer(self)
        timer.setInterval(self._RAMP_STEP_MS)
        self._ramp_timers[chamber_id] = timer

        def _press_tick() -> None:
            if self._targets.get(chamber_id, 0) >= 100:
                timer.stop()
                self._ramp_timers.pop(chamber_id, None)
                return
            self.inflate(chamber_id, self._RAMP_TARGET_STEP)

        timer.timeout.connect(_press_tick)
        timer.start()

    def simulate_touch_release(self, chamber_id: int, hold_ms: int) -> None:
        """Stop the inflate ramp and schedule a gradual deflate ramp after a delay."""
        self._cancel_ramp(chamber_id)

        delay = hold_ms * self._TOUCH_INFLATE_MULTIPLIER + 300
        delay_timer = QTimer(self)
        delay_timer.setSingleShot(True)
        delay_timer.setInterval(delay)
        delay_timer.timeout.connect(lambda: self._start_deflate_ramp(chamber_id))
        self._ramp_timers[chamber_id] = delay_timer
        delay_timer.start()

    def stop_all(self) -> None:
        """Stop all active timers (animation + ramps). Call on cleanup or pause."""
        self._timer.stop()
        for t in list(self._ramp_timers.values()):
            t.stop()
        self._ramp_timers.clear()

    def _cancel_ramp(self, chamber_id: int) -> None:
        """Cancel any ramp (inflate, deflate, or delay) for a single chamber."""
        old = self._ramp_timers.pop(chamber_id, None)
        if old is not None:
            old.stop()

    def _start_deflate_ramp(self, chamber_id: int) -> None:
        """Start a per-chamber timer that steps target down by _RAMP_TARGET_STEP each tick."""
        self._cancel_ramp(chamber_id)

        timer = QTimer(self)
        timer.setInterval(self._RAMP_STEP_MS)
        self._ramp_timers[chamber_id] = timer

        def _ramp_tick() -> None:
            if self._targets.get(chamber_id, 0) <= 0:
                timer.stop()
                self._ramp_timers.pop(chamber_id, None)
                return
            self.deflate(chamber_id, self._RAMP_TARGET_STEP)

        timer.timeout.connect(_ramp_tick)
        timer.start()

    def _tick(self) -> None:
        still_moving = False
        for chamber_id, target in list(self._targets.items()):
            current = self._current.get(chamber_id, 0)
            if current == target:
                continue
            still_moving = True
            if current < target:
                new_val = min(target, current + self._SIM_STEP)
            else:
                new_val = max(target, current - self._SIM_STEP)
            self._current[chamber_id] = new_val
            for cb in self._pressure_callbacks:
                cb(chamber_id, new_val)
        if not still_moving:
            self._timer.stop()

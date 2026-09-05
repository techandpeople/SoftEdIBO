"""Leak-compensating regulated hold - PC-side constants and seed helper.

Mirrors ``firmware/common/hold_duty.h``: a held chamber keeps its inflate
valve open while below target and the node servos the SHARED pressure pump in
real time, never below :data:`HOLD_DUTY_MIN` while a held valve is open (the
leaky tubing needs at least that much continuous delivery). The PC only seeds
the servo: the calibrated equilibrium duty at the hold pressure when a
``hold_duty_curve`` exists, else the floor.
"""

from __future__ import annotations

from typing import Any

from src.hardware.fill_scaling import interp_curve

# Pump PWM floor/ceiling for a hold (8-bit). Must match the firmware's shared
# ``pump_duty::MIN`` / ``pump_duty::FULL`` (firmware/common/pump_duty.h) - the
# same floor the vacuum pump drops to for a deflate past the gauge floor.
HOLD_DUTY_MIN = 180
HOLD_DUTY_MAX = 255


def clamp_hold_duty(duty: float | int | None) -> int:
    """Clamp a seed duty into the hold range (``None`` -> the floor)."""
    if duty is None:
        return HOLD_DUTY_MIN
    return max(HOLD_DUTY_MIN, min(HOLD_DUTY_MAX, int(round(duty))))


def seed_hold_duty(curve: Any, kpa: float) -> int:
    """Seed PWM for a hold at ``kpa``: the calibrated ``hold_duty_curve``
    interpolated there (clamped to the hold range), else the floor."""
    return clamp_hold_duty(interp_curve(curve, kpa))

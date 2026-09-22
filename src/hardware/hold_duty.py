"""Leak-compensating regulated hold - PC-side constants and helpers.

Mirrors ``firmware/common/hold_duty.h``: the node regulates a held chamber on
its own gauge with short, soft top-up pulses - a PRESSURE hold (inflate valve
+ pressure pump) for a pose above ambient, a VACUUM hold (deflate valve +
vacuum pump) for one below it. The pulse duty never sits below
:data:`HOLD_DUTY_MIN` (the diaphragm pumps barely move air under it). The PC
only picks the side and seeds the servo: the calibrated equilibrium duty at
the hold pressure when a ``hold_duty_curve`` exists, else the floor.
"""

from __future__ import annotations

from typing import Any

from src.hardware.fill_scaling import interp_curve

# Pump PWM floor/ceiling for a hold (8-bit). Must match the firmware's shared
# ``pump_duty::MIN`` / ``pump_duty::FULL`` (firmware/common/pump_duty.h) - the
# same floor the vacuum pump drops to for a deflate past the gauge floor.
HOLD_DUTY_MIN = 180
HOLD_DUTY_MAX = 255

# Hold side on the wire (``hold_duty`` ``dir`` field).
HOLD_PRESSURE = 0
HOLD_VACUUM = 1

# A target within this of ambient is "empty": nothing to hold on either side
# (a tared gauge idles at 0 +- a little noise).
AMBIENT_BAND_KPA = 0.5


def hold_direction(kpa: float) -> int | None:
    """Which hold a target of ``kpa`` (gauge, ambient = 0) needs.

    :data:`HOLD_PRESSURE` above ambient, :data:`HOLD_VACUUM` below it, and
    ``None`` inside :data:`AMBIENT_BAND_KPA` of ambient (nothing to hold).
    """
    if kpa != kpa:      # NaN
        return None
    if kpa >= AMBIENT_BAND_KPA:
        return HOLD_PRESSURE
    if kpa <= -AMBIENT_BAND_KPA:
        return HOLD_VACUUM
    return None


def clamp_hold_duty(duty: float | int | None) -> int:
    """Clamp a seed duty into the hold range (``None`` -> the floor)."""
    if duty is None:
        return HOLD_DUTY_MIN
    return max(HOLD_DUTY_MIN, min(HOLD_DUTY_MAX, int(round(duty))))


def seed_hold_duty(curve: Any, kpa: float) -> int:
    """Seed PWM for a hold at ``kpa``: the calibrated ``hold_duty_curve``
    interpolated there (clamped to the hold range), else the floor."""
    return clamp_hold_duty(interp_curve(curve, kpa))

"""Leak-compensating regulated hold - PC-side constants and helpers.

Mirrors ``firmware/common/hold_duty.h``: the node regulates a held chamber on
its own gauge from the losses it measures in real time - a PRESSURE hold
(inflate valve + pressure pump) for a pose above ambient, a VACUUM hold
(deflate valve + vacuum pump) for one below it. A tight chamber stays closed
and unpumped; a leaky one is held with its valve open and the pump PWM
servoed continuously down to :data:`HOLD_DUTY_MIN` (the RUN floor a spinning
pump keeps turning at - every pump start is a short kick at
:data:`HOLD_DUTY_KICK`, the START floor). The PC only picks the side and
seeds the servo: the calibrated equilibrium duty at the hold pressure when a
``hold_duty_curve`` exists, else 0 = let the node predict its own first duty
from the closed-valve loss it measures before opening.
"""

from __future__ import annotations

from typing import Any

from src.hardware.fill_scaling import interp_curve

# Pump PWM floors/ceiling for a hold (8-bit). Must match the firmware's shared
# ``pump_duty::RUN_MIN`` / ``pump_duty::MIN`` / ``pump_duty::FULL``
# (firmware/common/pump_duty.h). The servo never sits below the run floor; a
# pump start (and a blind timed re-pull) runs at least the start floor.
HOLD_DUTY_MIN = 70
HOLD_DUTY_KICK = 180
HOLD_DUTY_MAX = 255

# ``duty`` on the wire meaning "no seed - predict it from the measured loss".
HOLD_DUTY_UNSEEDED = 0

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
    """Clamp a seed duty into the hold range. ``None`` / 0 stay
    :data:`HOLD_DUTY_UNSEEDED` (the node predicts its own)."""
    if duty is None:
        return HOLD_DUTY_UNSEEDED
    d = int(round(duty))
    if d <= 0:
        return HOLD_DUTY_UNSEEDED
    return max(HOLD_DUTY_MIN, min(HOLD_DUTY_MAX, d))


def seed_hold_duty(curve: Any, kpa: float) -> int:
    """Seed PWM for a hold at ``kpa``: the calibrated ``hold_duty_curve``
    interpolated there (clamped to the hold range), else
    :data:`HOLD_DUTY_UNSEEDED`."""
    return clamp_hold_duty(interp_curve(curve, kpa))

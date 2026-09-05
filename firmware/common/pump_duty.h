#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Pump PWM floor/ceiling shared by every regulated or reduced pump run on both
// actuator boards (hold_duty.h servo, the below-floor deflate pull). ONE place
// so the firmware, the PC (src/hardware/hold_duty.py HOLD_DUTY_MIN) and the
// protocol doc cannot drift.
//
// The diaphragm pumps barely move air below ~180 PWM: the motor stalls and the
// leaky tubing is not replenished, so any reduced duty is floored here rather
// than allowed to wind down to a stall. Full duty stays the engine default for
// a normal fill; the floor is only reached by the hold servo and by a vacuum
// pull the gauge can no longer supervise.
// ---------------------------------------------------------------------------

namespace pump_duty {

constexpr uint8_t MIN  = 180;
constexpr uint8_t FULL = 255;

// Duty for a deflate that has passed the gauge floor: the 0..100 kPa gauge is
// blind below ambient, so once an open deflate chamber's reading has bottomed
// out at its floor (and its target lies below it) the pull continues on time
// only - at this reduced duty, not full, so the unsupervised part of the pull
// is gentler on the chamber and on the FA0520E valves (deep-vacuum lock).
constexpr uint8_t DEFLATE_BELOW_FLOOR = MIN;

}  // namespace pump_duty

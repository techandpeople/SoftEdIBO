#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Pump PWM floors/ceiling shared by every regulated or reduced pump run on
// both actuator boards (hold_duty.h regulator, the below-floor deflate pull).
// ONE place so the firmware, the PC (src/hardware/hold_duty.py) and the
// protocol doc cannot drift.
//
// A brushed diaphragm pump has TWO floors:
//   * MIN     - the START floor. From rest the motor needs this much to break
//               static friction; below it a stopped pump just hums. Every
//               pump start (a fill, a re-pull, a hold kick) goes through it.
//   * RUN_MIN - the RUN floor. Once spinning the motor keeps turning, and
//               keeps moving a little air, far below the start floor. The hold
//               regulator descends to it to balance a small leak with a
//               continuous, pulse-free trickle instead of on/off bursts at the
//               start floor. The regulator learns upward from here at runtime
//               whenever the pump stops delivering (stall / no head), so this
//               only needs to be a safe lower bound, not an exact figure.
// Full duty stays the engine default for a normal fill.
// ---------------------------------------------------------------------------

namespace pump_duty {

constexpr uint8_t RUN_MIN = 70;
constexpr uint8_t MIN     = 180;
constexpr uint8_t FULL    = 255;

// Duty for a deflate that has passed the gauge floor: the 0..100 kPa gauge is
// blind below ambient, so once an open deflate chamber's reading has bottomed
// out at its floor (and its target lies below it) the pull continues on time
// only - at this reduced duty, not full, so the unsupervised part of the pull
// is gentler on the chamber and on the FA0520E valves (deep-vacuum lock).
constexpr uint8_t DEFLATE_BELOW_FLOOR = MIN;

}  // namespace pump_duty

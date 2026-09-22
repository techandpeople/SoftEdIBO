#pragma once
// Minimal Arduino.h stand-in for the native (host) simulations under
// firmware/common/sim. Only what the shared headers touch.
#include <stdint.h>
#include <math.h>
#include <algorithm>
using std::min;
using std::max;
extern uint32_t sim_now_ms;
inline uint32_t millis() { return sim_now_ms; }

#pragma once
#include <Arduino.h>

// kPa <-> percentage conversion against a per-chamber reference max.

namespace units {

inline float pctToKpaOf(int pct, float ref_kpa) {
    return constrain(pct, 0, 100) * max(0.0f, ref_kpa) / 100.0f;
}

inline int kpaToPctOf(float kpa, float ref_kpa) {
    if (kpa <= 0.0f || ref_kpa <= 0.0f) return 0;
    return min(static_cast<int>(kpa * 100.0f / ref_kpa + 0.5f), 100);
}

}  // namespace units

#pragma once
#include <Arduino.h>

// kPa <-> percentage over a per-chamber [min_kpa, max_kpa] range.
//   0%   → min_kpa
//   100% → max_kpa
//
// With min_kpa = 0 (default), this matches the previous semantics
// (0% → 0 kPa, 100% → max_kpa). When a chamber is fed by a vacuum reservoir,
// min_kpa can be negative — 0% then represents the deepest vacuum point.

namespace units {

inline float pctToKpa(int pct, float min_kpa, float max_kpa) {
    int p = constrain(pct, 0, 100);
    return min_kpa + (max_kpa - min_kpa) * p / 100.0f;
}

inline int kpaToPct(float kpa, float min_kpa, float max_kpa) {
    float span = max_kpa - min_kpa;
    if (span <= 0.0f) return 0;
    int pct = static_cast<int>((kpa - min_kpa) * 100.0f / span + 0.5f);
    return constrain(pct, 0, 100);
}

}  // namespace units

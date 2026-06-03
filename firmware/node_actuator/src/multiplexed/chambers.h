#pragma once
#include <Arduino.h>
#include "pins.h"
#include "config.h"
#include "pca_valves.h"

// Per-chamber state: each chamber inflates by opening its inflate valve to
// the pressure tank, deflates by opening the deflate valve to the vacuum tank.
// No per-chamber pumps — pumps maintain the shared tanks.

namespace chambers {

enum State : uint8_t {
    IDLE, INFLATING, DEFLATING
};

struct Chamber {
    State    state      = IDLE;
    float    target_kpa = 0.0f;
    float    min_kpa    = config::DEFAULT_CHAMBER_MIN_KPA;
    float    max_kpa    = config::DEFAULT_CHAMBER_MAX_KPA;
};

inline Chamber state[MAX_CHAMBERS];
inline float   cachedKpa[MAX_CHAMBERS] = {};

inline void stop(int n) {
    pca_valves::setChamberValve(n, false, false);
    float saved_max = state[n].max_kpa;
    float saved_min = state[n].min_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved_max;
    state[n].min_kpa = saved_min;
}

inline void beginInflate(int n, float target_kpa) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa) return;
    state[n].state      = INFLATING;
    state[n].target_kpa = target_kpa;
    pca_valves::setChamberValve(n, true, false);   // inflate open, deflate closed
}

inline void beginDeflate(int n, float target_kpa) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    state[n].state      = DEFLATING;
    state[n].target_kpa = target_kpa;
    pca_valves::setChamberValve(n, false, true);   // deflate open, inflate closed
}

inline void closeAll() {
    for (int i = 0; i < MAX_CHAMBERS; i++) stop(i);
}

}  // namespace chambers

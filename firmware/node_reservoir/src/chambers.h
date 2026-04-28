#pragma once
#include <Arduino.h>
#include "pins.h"
#include "config.h"
#include "pca_valves.h"

// Per-chamber state: each chamber inflates by opening its inflate valve to
// the pressure tank, deflates by opening the deflate valve to the vacuum tank.
// No per-chamber pumps — pumps maintain the shared tanks.

namespace chambers {

constexpr uint32_t VALVE_SETTLE_MS = 20;

enum State : uint8_t {
    IDLE, PRE_INFLATE, PRE_DEFLATE, INFLATING, DEFLATING
};

struct Chamber {
    State    state      = IDLE;
    float    target_kpa = 0.0f;
    float    max_kpa    = config::DEFAULT_CHAMBER_MAX_KPA;
    uint32_t settle_ts  = 0;
};

inline Chamber state[MAX_CHAMBERS];
inline float   cachedKpa[MAX_CHAMBERS] = {};

inline void stop(int n) {
    pca_valves::setChamberValve(n, false, false);
    float saved = state[n].max_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved;
}

inline void beginInflate(int n, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, state[n].max_kpa));
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa) return;
    if (state[n].state == DEFLATING || state[n].state == PRE_DEFLATE) {
        pca_valves::setChamberValve(n, false, false);
        state[n].state      = PRE_INFLATE;
        state[n].target_kpa = target_kpa;
        state[n].settle_ts  = millis();
        return;
    }
    state[n].state      = INFLATING;
    state[n].target_kpa = target_kpa;
    pca_valves::setChamberValve(n, true, false);
}

inline void beginDeflate(int n, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    if (state[n].state == INFLATING || state[n].state == PRE_INFLATE) {
        pca_valves::setChamberValve(n, false, false);
        state[n].state      = PRE_DEFLATE;
        state[n].target_kpa = target_kpa;
        state[n].settle_ts  = millis();
        return;
    }
    state[n].state      = DEFLATING;
    state[n].target_kpa = target_kpa;
    pca_valves::setChamberValve(n, false, true);
}

inline void closeAll() {
    for (int i = 0; i < MAX_CHAMBERS; i++) stop(i);
}

}  // namespace chambers

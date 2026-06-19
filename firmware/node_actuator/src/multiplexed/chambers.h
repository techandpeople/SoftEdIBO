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

// Child-safety watchdog: if a chamber stays INFLATING/DEFLATING longer than
// this without reaching its target (e.g. its mux pressure channel unplugged
// or stuck, so the cutoff in loop() never fires), force-stop it. Normal
// actuations on these small chambers finish in a few seconds.
constexpr uint32_t ACTUATION_TIMEOUT_MS = 10000;

struct Chamber {
    State    state      = IDLE;
    float    target_kpa = 0.0f;
    float    min_kpa    = config::DEFAULT_CHAMBER_MIN_KPA;
    float    max_kpa    = config::DEFAULT_CHAMBER_MAX_KPA;
    uint32_t since_ms   = 0;     // when INFLATING/DEFLATING began (watchdog)
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
    state[n].since_ms   = millis();
    pca_valves::setChamberValve(n, true, false);   // inflate open, deflate closed
}

inline void beginDeflate(int n, float target_kpa) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    state[n].state      = DEFLATING;
    state[n].target_kpa = target_kpa;
    state[n].since_ms   = millis();
    pca_valves::setChamberValve(n, false, true);   // deflate open, inflate closed
}

inline void closeAll() {
    for (int i = 0; i < MAX_CHAMBERS; i++) stop(i);
}

// Force-stop any chamber actuating past ACTUATION_TIMEOUT_MS (sensor failure
// safety net — see constant above). Call periodically from loop().
inline void actuationWatchdog(uint32_t now) {
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (state[i].state == IDLE) continue;
        if (now - state[i].since_ms >= ACTUATION_TIMEOUT_MS) {
            stop(i);
        }
    }
}

}  // namespace chambers

#pragma once
#include <Arduino.h>
#include "pins.h"
#include "config.h"
#include "pca_valves.h"
#include "fill_control.h"   // shared time-based fill + idle leak-maintenance policy

// Per-chamber state: each chamber inflates by opening its inflate valve to
// the pressure tank, deflates by opening the deflate valve to the vacuum tank.
// No per-chamber pumps — pumps maintain the shared tanks.
// Board-agnostic fill policy (time-based fill, leak maintenance, safety ceilings)
// lives in firmware/common/fill_control.h, shared with node_direct.

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
    State    state         = IDLE;
    float    target_kpa    = 0.0f;
    float    min_kpa       = config::DEFAULT_CHAMBER_MIN_KPA;
    float    max_kpa       = config::DEFAULT_CHAMBER_MAX_KPA;
    uint32_t since_ms      = 0;  // when INFLATING/DEFLATING began (watchdog)
    uint32_t fill_until_ms = 0;  // INFLATING: stop at this millis() (0 = pressure-based)
    float    hold_kpa      = 0.0f;  // IDLE: level to maintain against leaks (0 = none)
    uint8_t  droop_count   = 0;  // consecutive idle checks seen below hold (touch debounce)
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

// ``fill_ms`` > 0 selects time-based fill: the inflate valve stays open to the
// pressure tank for that long (clamped to MAX_FILL_MS) regardless of the mux
// pressure reading, with max_kpa as the only pressure cutoff (caller passes
// target_kpa = max_kpa). ``fill_ms`` == 0 keeps the pressure-target behaviour.
inline void beginInflate(int n, float target_kpa, uint32_t fill_ms = 0) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    uint32_t until = fill_control::fillUntil(fill_ms);
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa
        && state[n].fill_until_ms == 0 && until == 0) return;
    state[n].state         = INFLATING;
    state[n].target_kpa    = target_kpa;
    state[n].since_ms      = millis();
    state[n].fill_until_ms = until;
    pca_valves::setChamberValve(n, true, false);   // inflate open, deflate closed
}

// Thin node-specific wrappers over the shared fill-control policy: they supply
// the multiplexed board's state predicates and its stop/top-up actuation.
// Called every loop() — independent of the (slow) mux pressure cadence, which is
// the whole point of timing the fill. max_kpa cutoff + the actuation watchdog
// remain independent safety nets.
inline void fillTimeTick(uint32_t now) {
    fill_control::fillTimeTick(
        state, cachedKpa, MAX_CHAMBERS, now,
        [](const Chamber& ch) { return ch.state == INFLATING; },
        [](int i, float achieved) {
            stop(i);
            state[i].hold_kpa = achieved;   // maintain the level we reached
        });
}

inline void maintainTick(uint32_t now) {
    static uint32_t last = 0;
    fill_control::maintainTick(
        state, cachedKpa, MAX_CHAMBERS, now, last,
        [](const Chamber& ch) { return ch.state == IDLE; },
        [](int i, float hold) {
            beginInflate(i, hold);   // pressure-based top-up to the held level
        });
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

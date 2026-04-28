#pragma once
#include <Arduino.h>
#include "pins.h"

// Per-chamber state machine + valve/pump coordination for node_direct.
// Pumps are shared: any chamber inflating runs PUMP1, any deflating runs PUMP2.

namespace chambers {

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float HARD_MAX_KPA    = 12.0f;
constexpr uint8_t  DEFAULT_INFLATE_DUTY = 255;
constexpr uint32_t VALVE_SETTLE_MS      =  20;

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr int PUMP1_LEDC_CH =     0;
constexpr int PUMP2_LEDC_CH =     1;

enum State : uint8_t {
    IDLE, PRE_INFLATE, PRE_DEFLATE, INFLATING, DEFLATING
};

struct Chamber {
    State    state      = IDLE;
    uint8_t  duty       = 0;
    float    target_kpa = 0.0f;
    float    max_kpa    = DEFAULT_MAX_KPA;
    uint32_t settle_ts  = 0;
};

inline Chamber state[NUM_CHAMBERS];
inline float   cachedKpa[NUM_CHAMBERS] = {};

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------

inline void setValve(int ch, int side, bool open) {
    digitalWrite(VALVE_PINS[ch * 2 + side], open ? HIGH : LOW);
}

inline void recalcPumps() {
    uint8_t maxDuty    = 0;
    bool    anyDeflate = false;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (state[i].state == INFLATING)
            maxDuty = max(maxDuty, state[i].duty);
        if (state[i].state == DEFLATING)
            anyDeflate = true;
    }
    ledcWrite(PUMP1_LEDC_CH, maxDuty);
    ledcWrite(PUMP2_LEDC_CH, anyDeflate ? 255 : 0);
}

inline void stop(int n) {
    setValve(n, 0, false);
    setValve(n, 1, false);
    float saved = state[n].max_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved;
}

// ---------------------------------------------------------------------------
// Inflate / deflate with non-blocking valve settle (avoid both valves open)
// ---------------------------------------------------------------------------

inline void beginInflate(int n, uint8_t duty, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, state[n].max_kpa));
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa) return;
    if (state[n].state == DEFLATING || state[n].state == PRE_DEFLATE) {
        setValve(n, 1, false);
        state[n].state      = PRE_INFLATE;
        state[n].duty       = duty;
        state[n].target_kpa = target_kpa;
        state[n].settle_ts  = millis();
        recalcPumps();
        return;
    }
    state[n].state      = INFLATING;
    state[n].duty       = duty;
    state[n].target_kpa = target_kpa;
    setValve(n, 0, true);
    recalcPumps();
}

inline void beginDeflate(int n, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    if (state[n].state == INFLATING || state[n].state == PRE_INFLATE) {
        setValve(n, 0, false);
        state[n].state      = PRE_DEFLATE;
        state[n].target_kpa = target_kpa;
        state[n].settle_ts  = millis();
        recalcPumps();
        return;
    }
    state[n].state      = DEFLATING;
    state[n].target_kpa = target_kpa;
    setValve(n, 1, true);
    recalcPumps();
}

// ---------------------------------------------------------------------------
// Setup all chamber I/O. Call once from setup().
// ---------------------------------------------------------------------------

inline void hardware_init() {
    for (int i = 0; i < NUM_CHAMBERS * 2; i++) {
        pinMode(VALVE_PINS[i], OUTPUT);
        digitalWrite(VALVE_PINS[i], LOW);
    }
    ledcSetup(PUMP1_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP2_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP_PINS[0], PUMP1_LEDC_CH);
    ledcAttachPin(PUMP_PINS[1], PUMP2_LEDC_CH);
    ledcWrite(PUMP1_LEDC_CH, 0);
    ledcWrite(PUMP2_LEDC_CH, 0);
}

}  // namespace chambers

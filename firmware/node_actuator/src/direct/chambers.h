#pragma once
#include <Arduino.h>
#include "pins.h"
#include "dbg.h"

// Per-chamber state machine + valve/pump coordination for node_direct.
// Pumps are shared: any chamber inflating runs PUMP1, any deflating runs PUMP2.

namespace chambers {

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float DEFAULT_MIN_KPA = 0.0f;
constexpr float HARD_MAX_KPA    = 12.0f;
constexpr float HARD_MIN_KPA    = -12.0f;   // limit for vacuum-fed chambers
constexpr uint8_t  DEFAULT_INFLATE_DUTY = 255;

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr int PUMP1_LEDC_CH =     0;
constexpr int PUMP2_LEDC_CH =     1;

enum State : uint8_t {
    IDLE, INFLATING, DEFLATING
};

// Child-safety watchdog: if a chamber stays INFLATING/DEFLATING longer than
// this without reaching its target (e.g. pressure sensor unplugged or stuck,
// so the cutoff in loop() never fires), force-stop it. Normal actuations on
// these small chambers finish in a few seconds.
constexpr uint32_t ACTUATION_TIMEOUT_MS = 10000;

struct Chamber {
    State    state      = IDLE;
    uint8_t  duty       = 0;
    float    target_kpa = 0.0f;
    float    min_kpa    = DEFAULT_MIN_KPA;
    float    max_kpa    = DEFAULT_MAX_KPA;
    uint32_t since_ms   = 0;     // when INFLATING/DEFLATING began (watchdog)
};

inline Chamber state[NUM_CHAMBERS];
inline float   cachedKpa[NUM_CHAMBERS] = {};

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------

inline void setValve(int ch, int side, bool open) {
    DBG_PRINT("VALVE ch=%d %s %s\n",
              ch, side == 0 ? "inflate" : "deflate", open ? "OPEN" : "close");
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
    static uint8_t lastInflateDuty = 0xFF;
    static bool    lastDeflateOn   = true;
    if (maxDuty != lastInflateDuty || anyDeflate != lastDeflateOn) {
        DBG_PRINT("PUMPS inflate_duty=%u deflate=%s\n",
                  maxDuty, anyDeflate ? "ON" : "off");
        lastInflateDuty = maxDuty;
        lastDeflateOn   = anyDeflate;
    }
    ledcWrite(PUMP1_LEDC_CH, maxDuty);
    ledcWrite(PUMP2_LEDC_CH, anyDeflate ? 255 : 0);
}

inline void stop(int n) {
    setValve(n, 0, false);
    setValve(n, 1, false);
    float saved_max = state[n].max_kpa;
    float saved_min = state[n].min_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved_max;
    state[n].min_kpa = saved_min;
}

// ---------------------------------------------------------------------------
// Inflate / deflate. Each chamber has its own inflate and deflate valve, so on
// a direction reversal we just close the opposite valve and open the new one —
// the valve then stays open until stop() (target/limit reached, or hold).
// ---------------------------------------------------------------------------

inline void beginInflate(int n, uint8_t duty, float target_kpa) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa) return;
    setValve(n, 1, false);              // close deflate before opening inflate
    state[n].state      = INFLATING;
    state[n].duty       = duty;
    state[n].target_kpa = target_kpa;
    state[n].since_ms   = millis();
    setValve(n, 0, true);
    recalcPumps();
}

inline void beginDeflate(int n, float target_kpa) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    setValve(n, 0, false);              // close inflate before opening deflate
    state[n].state      = DEFLATING;
    state[n].target_kpa = target_kpa;
    state[n].since_ms   = millis();
    setValve(n, 1, true);
    recalcPumps();
}

// Force-stop any chamber actuating past ACTUATION_TIMEOUT_MS (sensor failure
// safety net — see constant above). Call periodically from loop().
inline void actuationWatchdog(uint32_t now) {
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (state[i].state == IDLE) continue;
        if (now - state[i].since_ms >= ACTUATION_TIMEOUT_MS) {
            DBG_PRINT("WATCHDOG ch=%d stopped after %lu ms\n", i,
                      (unsigned long)(now - state[i].since_ms));
            stop(i);
            recalcPumps();
        }
    }
}

// ---------------------------------------------------------------------------
// Manual (dev/test) actuation — bypasses the chamber state machine, so it needs
// its own safety net. Two guards, enforced by manualSafetyTick() from loop():
//   1. Dead-man: any manual actuator auto-offs after MANUAL_MAX_ON_MS, so a lost
//      "off" command or a distracted operator can't leave a pump running.
//   2. HARD_MAX cutoff: the inflate pump is cut (and the offending inflate valve
//      closed) if any chamber reaches the hard pressure limit.
// At most one valve per chamber is held open at a time (inflate XOR deflate).
// These controls are for developers/teachers, never exposed to children.
// ---------------------------------------------------------------------------

constexpr uint32_t MANUAL_MAX_ON_MS = 5000;

inline bool     manualPumpOn[2]                 = {false, false};
inline uint32_t manualPumpTs[2]                 = {0, 0};
inline bool     manualValveOn[NUM_CHAMBERS * 2] = {};
inline uint32_t manualValveTs[NUM_CHAMBERS * 2] = {};

inline void setManualPump(int idx, bool on) {
    if (idx < 0 || idx > 1) return;
    manualPumpOn[idx] = on;
    manualPumpTs[idx] = on ? millis() : 0;
    ledcWrite(idx == 0 ? PUMP1_LEDC_CH : PUMP2_LEDC_CH, on ? DEFAULT_INFLATE_DUTY : 0);
}

inline void setManualValve(int ch, int side, bool open) {
    if (ch < 0 || ch >= NUM_CHAMBERS || side < 0 || side > 1) return;
    // Single side open per chamber: opening one side closes the other.
    if (open) {
        int other = ch * 2 + (1 - side);
        if (manualValveOn[other]) {
            manualValveOn[other] = false;
            manualValveTs[other] = 0;
            setValve(ch, 1 - side, false);
        }
    }
    int i = ch * 2 + side;
    manualValveOn[i] = open;
    manualValveTs[i] = open ? millis() : 0;
    setValve(ch, side, open);
}

inline void manualSafetyTick(uint32_t now) {
    // 1. Dead-man auto-off.
    for (int i = 0; i < 2; i++)
        if (manualPumpOn[i] && now - manualPumpTs[i] >= MANUAL_MAX_ON_MS)
            setManualPump(i, false);
    for (int i = 0; i < NUM_CHAMBERS * 2; i++)
        if (manualValveOn[i] && now - manualValveTs[i] >= MANUAL_MAX_ON_MS)
            setManualValve(i / 2, i % 2, false);

    // 2. HARD limit cutoff, both directions (symmetric with node_multiplexed):
    //    - inflate pump cut (+ inflate valve closed) if a chamber hits HARD_MAX;
    //    - deflate pump cut (+ deflate valve closed) if a chamber hits HARD_MIN.
    if (manualPumpOn[0]) {
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            if (cachedKpa[i] >= HARD_MAX_KPA) {
                setManualPump(0, false);
                if (manualValveOn[i * 2]) setManualValve(i, 0, false);
            }
        }
    }
    if (manualPumpOn[1]) {
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            if (cachedKpa[i] <= HARD_MIN_KPA) {
                setManualPump(1, false);
                if (manualValveOn[i * 2 + 1]) setManualValve(i, 1, false);
            }
        }
    }
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

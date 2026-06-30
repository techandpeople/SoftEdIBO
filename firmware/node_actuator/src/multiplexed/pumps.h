#pragma once
#include <Arduino.h>
#include "pins.h"
#include "dbg.h"

// 6 pumps via 3× DRV3297. Each pump has a single PWM input. LEDC channels 0..5
// are dedicated to pump PWM. Each pump is assigned a "role" (PRESSURE_TANK,
// VACUUM_TANK, or UNKNOWN) by the boot autodetect; runtime configure can override.

namespace pumps {

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr uint8_t PUMP_DEFAULT_DUTY = 255;

enum Role : uint8_t { ROLE_UNKNOWN = 0, ROLE_PRESSURE = 1, ROLE_VACUUM = 2 };

inline Role roles[NUM_PUMPS] = {ROLE_UNKNOWN};
inline bool _anyOn = false;  // tracked by setters so stopAll can print on transition

inline void hardware_init() {
    for (int i = 0; i < NUM_PUMPS; i++) {
        ledcSetup(i, PUMP_PWM_FREQ, PUMP_PWM_RES);
        ledcAttachPin(PUMP_PINS[i], i);
        ledcWrite(i, 0);
    }
}

// Drive a single pump (LEDC channel = pump index).
inline void setDuty(int pump, uint8_t duty) {
    if (pump < 0 || pump >= NUM_PUMPS) return;
    DBG_PRINT("PUMP %d duty=%u\n", pump + 1, duty);
    if (duty > 0) _anyOn = true;
    ledcWrite(pump, duty);
}

inline void stopAll() {
    if (_anyOn) {
        DBG_PRINT("PUMPS stopAll\n");
        _anyOn = false;
    }
    for (int i = 0; i < NUM_PUMPS; i++) ledcWrite(i, 0);
}

// Set duty for all pumps with a given role. duty=0 stops them.
inline void setRoleDuty(Role role, uint8_t duty) {
    static uint8_t lastDuty[3] = {0, 0, 0};  // indexed by Role enum (UNKNOWN/PRESSURE/VACUUM)
    if (role < 3 && lastDuty[role] != duty) {
        const char* name = role == ROLE_PRESSURE ? "pressure"
                         : role == ROLE_VACUUM   ? "vacuum"
                         : "?";
        DBG_PRINT("PUMPS role=%s duty=%u\n", name, duty);
        lastDuty[role] = duty;
    }
    if (duty > 0) _anyOn = true;
    for (int i = 0; i < NUM_PUMPS; i++) {
        if (roles[i] == role) ledcWrite(i, duty);
    }
}

inline int countByRole(Role role) {
    int n = 0;
    for (int i = 0; i < NUM_PUMPS; i++) if (roles[i] == role) n++;
    return n;
}

// Run only the FIRST `activeCount` pumps of a role at `duty`, the rest off.
// Common-manifold scaling: with all role pumps feeding one shared line, only the
// NUMBER running matters (not which), so we keep the same pumps spun up as the
// open-valve count rises/falls. `activeCount` is clamped to [0, pumps-of-role].
// Running fewer pumps than open valves can vent keeps the fill speed per chamber
// roughly constant AND stops spare pumps dead-heading the manifold (the "pump
// running dry / forcing" failure): a pump only spins when valves can take its air.
inline void setRoleActiveCount(Role role, int activeCount, uint8_t duty) {
    static uint8_t lastCount[3] = {0xFF, 0xFF, 0xFF};
    int seen = 0;
    for (int i = 0; i < NUM_PUMPS; i++) {
        if (roles[i] != role) continue;
        bool on = seen < activeCount;
        ledcWrite(i, on ? duty : 0);
        if (on) _anyOn = true;
        seen++;
    }
    int clamped = (activeCount < 0) ? 0 : (activeCount > seen ? seen : activeCount);
    if (role < 3 && lastCount[role] != (uint8_t)clamped) {
        const char* name = role == ROLE_PRESSURE ? "pressure"
                         : role == ROLE_VACUUM   ? "vacuum" : "?";
        DBG_PRINT("PUMPS role=%s active=%d/%d duty=%u\n", name, clamped, seen, duty);
        lastCount[role] = (uint8_t)clamped;
    }
}

}  // namespace pumps

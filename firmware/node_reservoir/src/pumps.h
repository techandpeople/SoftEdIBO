#pragma once
#include <Arduino.h>
#include "pins.h"

// 6 pumps via 3× DRV3297. Each pump has a single PWM input. LEDC channels 0..5
// are dedicated to pump PWM. Each pump is assigned a "role" (PRESSURE_TANK,
// VACUUM_TANK, or UNKNOWN) by the boot autodetect; runtime configure can override.

namespace pumps {

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr uint8_t PUMP_DEFAULT_DUTY = 255;

enum Role : uint8_t { ROLE_UNKNOWN = 0, ROLE_PRESSURE = 1, ROLE_VACUUM = 2 };

inline Role roles[NUM_PUMPS] = {ROLE_UNKNOWN};

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
    ledcWrite(pump, duty);
}

inline void stopAll() {
    for (int i = 0; i < NUM_PUMPS; i++) ledcWrite(i, 0);
}

// Set duty for all pumps with a given role. duty=0 stops them.
inline void setRoleDuty(Role role, uint8_t duty) {
    for (int i = 0; i < NUM_PUMPS; i++) {
        if (roles[i] == role) ledcWrite(i, duty);
    }
}

inline int countByRole(Role role) {
    int n = 0;
    for (int i = 0; i < NUM_PUMPS; i++) if (roles[i] == role) n++;
    return n;
}

}  // namespace pumps

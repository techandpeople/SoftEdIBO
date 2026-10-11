#pragma once
#include <Arduino.h>
#include "pins.h"
#include "dbg.h"

// 6 pumps via 3x DRV3297. Each pump has a single PWM input. LEDC channels 0..5
// are dedicated to pump PWM. The pumps are wired in pairs (PUMP_GROUPS below):
// one inflate + one deflate pump per skin, each pair feeding its own chambers.

namespace pumps {

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr uint8_t PUMP_DEFAULT_DUTY = 255;

// Pneumatic wiring: every skin has its own pump pair - the inflate pump feeds a
// private pressure line, the deflate pump a private vacuum line - shared only by
// that skin's chambers. Pump indices follow PUMP_PINS (PUMP1 = 0); connector
// names are the PCB references. `chambers` is a bit mask (bit i = chamber i,
// see pca_valves::VALVE_MAP for each chamber's valve connectors). A chamber in
// no group has no pump behind its valves.
struct PumpGroup {
    uint8_t  inflate_pump;
    uint8_t  deflate_pump;
    uint16_t chambers;
};

constexpr PumpGroup PUMP_GROUPS[] = {
    {0, 1, 0b000111000000},  // J12 / J13 -> chambers 6, 7, 8
    {3, 2, 0b000000111000},  // J24 / J23 -> chambers 3, 4, 5
    {4, 5, 0b000000000111},  // J26 / J27 -> chambers 0, 1, 2
};
constexpr int NUM_PUMP_GROUPS = sizeof(PUMP_GROUPS) / sizeof(PUMP_GROUPS[0]);

// Every pump drives at most one line and every chamber hangs on at most one pair.
constexpr bool pumpGroupsAreDisjoint() {
    uint8_t  pumpsSeen = 0;
    uint16_t chambersSeen = 0;
    for (int g = 0; g < NUM_PUMP_GROUPS; g++) {
        const PumpGroup& grp = PUMP_GROUPS[g];
        if (grp.inflate_pump >= NUM_PUMPS || grp.deflate_pump >= NUM_PUMPS) return false;
        if (grp.inflate_pump == grp.deflate_pump) return false;
        uint8_t pair = (uint8_t)((1u << grp.inflate_pump) | (1u << grp.deflate_pump));
        if ((pumpsSeen & pair) || (chambersSeen & grp.chambers)) return false;
        if (grp.chambers >> MAX_CHAMBERS) return false;
        pumpsSeen |= pair;
        chambersSeen |= grp.chambers;
    }
    return true;
}
static_assert(pumpGroupsAreDisjoint(), "PUMP_GROUPS reuses a pump or a chamber");

// Group whose pump pair feeds chamber n, or -1 when it has none.
inline int groupOf(int chamber) {
    if (chamber < 0 || chamber >= MAX_CHAMBERS) return -1;
    for (int g = 0; g < NUM_PUMP_GROUPS; g++)
        if (PUMP_GROUPS[g].chambers & (1u << chamber)) return g;
    return -1;
}

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

// Drive one group's pump pair (duty 0 = off). Each line is private to its
// group, so a pump only spins while one of its own chambers' valves can take
// its air - a pump with no open valve would dead-head its line (the "pump
// running dry / forcing" failure).
inline void setGroupDuty(int group, uint8_t inflate_duty, uint8_t deflate_duty) {
    if (group < 0 || group >= NUM_PUMP_GROUPS) return;
    static uint8_t last[NUM_PUMP_GROUPS][2] = {};
    if (last[group][0] != inflate_duty || last[group][1] != deflate_duty) {
        DBG_PRINT("PUMPS group=%d inflate=%u deflate=%u\n", group, inflate_duty, deflate_duty);
        last[group][0] = inflate_duty;
        last[group][1] = deflate_duty;
    }
    if (inflate_duty > 0 || deflate_duty > 0) _anyOn = true;
    ledcWrite(PUMP_GROUPS[group].inflate_pump, inflate_duty);
    ledcWrite(PUMP_GROUPS[group].deflate_pump, deflate_duty);
}

}  // namespace pumps

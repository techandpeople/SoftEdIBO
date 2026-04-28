#pragma once
#include <Arduino.h>
#include "pins.h"

// Runtime configuration — populated by boot autodetect, optionally overridden
// by the gateway's `configure` command.

namespace config {

constexpr float DEFAULT_TANK_PRESSURE_MAX_KPA = 50.0f;
constexpr float DEFAULT_TANK_VACUUM_MAX_KPA   = 50.0f;
constexpr float HARD_TANK_MAX_KPA             = 80.0f;
constexpr float DEFAULT_CHAMBER_MAX_KPA       = 8.0f;
constexpr float HARD_CHAMBER_MAX_KPA          = 12.0f;

struct State {
    bool ready          = false;     // becomes true after autodetect succeeds
    bool error          = false;     // true if autodetect saw <2 PCA9685s
    int  num_chambers   = 0;         // detected count (max 12)

    // Mux channel assignments
    int  chamber_mux_ch[MAX_CHAMBERS] = {};   // mux channel per chamber index
    int  pressure_tank_mux_ch         = -1;
    int  vacuum_tank_mux_ch           = -1;

    // Tank pressure targets and limits
    float tank_pressure_target_kpa = 0.0f;
    float tank_vacuum_target_kpa   = 0.0f;
    float tank_pressure_max_kpa    = DEFAULT_TANK_PRESSURE_MAX_KPA;
    float tank_vacuum_max_kpa      = DEFAULT_TANK_VACUUM_MAX_KPA;
};

inline State state;

}  // namespace config

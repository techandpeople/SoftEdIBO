#pragma once
#include <Arduino.h>
#include "pins.h"
#include "pressure.h"   // VACUUM_HOLD_FLOOR_KPA (valve-safe vacuum cap)

// Runtime configuration - populated by boot autodetect, optionally overridden
// by the gateway's `configure` command.

namespace config {

constexpr float DEFAULT_TANK_PRESSURE_MAX_KPA =  50.0f;   // hard upper cap for positive tank
constexpr float DEFAULT_TANK_PRESSURE_MIN_KPA =   0.0f;   // never below ambient (negative would mean broken vacuum into pressure tank)
constexpr float DEFAULT_TANK_VACUUM_MAX_KPA   =   0.0f;   // never above ambient (positive would mean broken pressure into vacuum tank)
constexpr float DEFAULT_TANK_VACUUM_MIN_KPA   = -50.0f;   // hard lower cap for vacuum tank
constexpr float HARD_TANK_MAX_KPA             =  80.0f;
constexpr float HARD_TANK_MIN_KPA             = -80.0f;

constexpr float DEFAULT_CHAMBER_MAX_KPA =   8.0f;
constexpr float DEFAULT_CHAMBER_MIN_KPA =   0.0f;
// Effectively uncapped per-chamber (the unreliable gauge must not gate fills):
// over-pressure is bounded by TIME - MAX_FILL_MS, the manual dead-man, and the
// actuation watchdog - not by this ceiling. Kept in sync with
// skin_config.MAX_ALLOWED_KPA. (The shared TANK caps above are unchanged.)
constexpr float HARD_CHAMBER_MAX_KPA    =  100.0f;
// Deepest vacuum a chamber may hold - valve-safe, derived from the sensor floor
// (pressure.h): -100 (inert) with the blind 0..100 gauge, -40 with the -40..40
// vacuum sensor so the FA0520E always re-opens. Bounds set_min + the manual cutoff.
constexpr float HARD_CHAMBER_MIN_KPA    = pressure::VACUUM_HOLD_FLOOR_KPA;

struct State {
    bool ready          = false;     // becomes true after autodetect succeeds
    bool error          = false;     // true if autodetect saw <2 PCA9685s
    int  num_chambers   = 0;         // detected count (max 12)

    // Mux channel assignments
    int  chamber_mux_ch[MAX_CHAMBERS] = {};   // mux channel per chamber index
    int  pressure_tank_mux_ch         = -1;
    int  vacuum_tank_mux_ch           = -1;

    // Tank pressure targets and limits.
    // Pressure tank operates in [min, max] with target somewhere inside.
    // Vacuum tank limits are usually negative; same pump-on-when-out-of-range logic.
    float tank_pressure_target_kpa = 0.0f;
    float tank_vacuum_target_kpa   = 0.0f;
    float tank_pressure_max_kpa    = DEFAULT_TANK_PRESSURE_MAX_KPA;
    float tank_pressure_min_kpa    = DEFAULT_TANK_PRESSURE_MIN_KPA;
    float tank_vacuum_max_kpa      = DEFAULT_TANK_VACUUM_MAX_KPA;
    float tank_vacuum_min_kpa      = DEFAULT_TANK_VACUUM_MIN_KPA;
};

inline State state;

}  // namespace config

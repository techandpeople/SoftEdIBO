#pragma once
#include <Arduino.h>

// Lock-free SPSC ring buffer for ESP-NOW commands.

namespace cmd_queue {

enum CmdType : uint8_t {
    CMD_NONE = 0,
    // Per-chamber commands (chamber field is the chamber index).
    CMD_INFLATE, CMD_DEFLATE, CMD_SET_PRESSURE, CMD_SET_MAX, CMD_SET_MIN, CMD_HOLD,
    // Manual valve/pump control (debug/test).
    CMD_VALVE_MANUAL, CMD_PUMP_MANUAL,
    // Configuration / status.
    CMD_CONFIGURE, CMD_PING
#ifdef DEBUG_BUILD
    , CMD_DEBUG
#endif
};

struct Cmd {
    CmdType  type;
    int8_t   chamber;       // chamber index
    int16_t  param;         // delta or value (percent), or valve side / pump idx
    float    param_kpa;     // chamber min or max in kPa (depends on type)
    int16_t  cfg_chambers;  // configure: num_chambers, or manual: open/on (bool)
    float    cfg_p_min;     // configure: tank_pressure_min_kpa
    float    cfg_p_max;     // configure: tank_pressure_max_kpa
    float    cfg_v_min;     // configure: tank_vacuum_min_kpa
    float    cfg_v_max;     // configure: tank_vacuum_max_kpa
    float    cfg_p_target;  // configure: tank_pressure_target_kpa
    float    cfg_v_target;  // configure: tank_vacuum_target_kpa
    uint8_t  cfg_pressure_mask; // configure: bit i -> pump (i+1) in pressure group
    uint8_t  cfg_vacuum_mask;   // configure: bit i -> pump (i+1) in vacuum group
};

constexpr uint8_t QUEUE_MASK = 0x0F;
inline Cmd queue[QUEUE_MASK + 1];
inline volatile uint8_t head = 0;
inline volatile uint8_t tail = 0;

inline bool push(const Cmd& c) {
    uint8_t next = (head + 1) & QUEUE_MASK;
    if (next == tail) return false;
    queue[head] = c;
    head = next;
    return true;
}

inline bool pop(Cmd& c) {
    if (tail == head) return false;
    c = queue[tail];
    tail = (tail + 1) & QUEUE_MASK;
    return true;
}

}  // namespace cmd_queue

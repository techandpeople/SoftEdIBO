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
    // Per-chamber vent (bench): BOTH valves of the chamber open with no pump
    // feeding them, so it equalises to atmosphere; chamber -1 = every chamber.
    // cfg_chambers = open (0/1). Manual-override rules (dead-man) apply.
    CMD_VENT,
    // Continuous bench test: latch one pump + all of its valves wide open,
    // ignoring pressure + the manual dead-man, until stopped or its own ~3 s
    // keepalive lapses (node_direct only).
    CMD_TEST_RUN, CMD_TEST_STOP,
    // Emergency stop: latch all actuators off until re-armed.
    CMD_STOP, CMD_RESUME,
    // Telemetry cadence: temporarily raise the status broadcast rate (dense
    // pressure for calibration / live gauges / touch coupling), auto-reverting.
    CMD_STATUS_RATE,
    // Zero the pressure sensors: capture each chamber's current reading as its
    // ambient offset (caller must have vented to atmosphere first). Persisted.
    CMD_TARE,
    // Leak-compensating hold (see common/hold_duty.h): pressure or vacuum
    // side (dir), regulated on the chamber's gauge. param = off flag (1 = drop).
    CMD_HOLD_DUTY,
    // Configuration / status.
    CMD_CONFIGURE, CMD_PING
#ifdef DEBUG_BUILD
    , CMD_DEBUG
#endif
};

// Narrow a JSON "chamber" to the int8_t field. -1 means "every chamber";
// anything outside -1..127 becomes 127 (never a valid index) so the per-board
// range check rejects it instead of it wrapping to -1 (all chambers) or to
// another negative value that some handlers also treat as "all".
inline int8_t chamberArg(long v) {
    return (v < -1 || v > 127) ? (int8_t)127 : (int8_t)v;
}

struct Cmd {
    CmdType  type;
    int8_t   chamber;       // chamber index
    int16_t  param;         // delta or value (percent), or valve side / pump idx
    uint8_t  duty;          // inflate/deflate: pump PWM duty 0-255 (0 = unset -> full)
    uint32_t fill_ms;       // inflate/deflate: per-chamber open-time budget (ms; 0 = engine default cap)
    uint8_t  timed;         // inflate/deflate: 1 = open-loop ("timed":1) - the board has
                            // no pressure sensor populated, run purely on fill_ms and
                            // ignore the (floating, noise) gauge readings entirely
    float    param_kpa;     // chamber min or max in kPa (depends on type)
    uint8_t  dir;           // hold_duty: 0 = pressure hold, 1 = vacuum hold
    int16_t  cfg_chambers;  // configure: num_chambers, or manual: open/on (bool)
    float    cfg_p_min;     // configure: tank_pressure_min_kpa
    float    cfg_p_max;     // configure: tank_pressure_max_kpa
    float    cfg_v_min;     // configure: tank_vacuum_min_kpa
    float    cfg_v_max;     // configure: tank_vacuum_max_kpa
    float    cfg_p_target;  // configure: tank_pressure_target_kpa
    float    cfg_v_target;  // configure: tank_vacuum_target_kpa
    uint8_t  cfg_pressure_mask; // configure: bit i -> pump (i+1) in pressure group
    uint8_t  cfg_vacuum_mask;   // configure: bit i -> pump (i+1) in vacuum group
    uint16_t seq;               // confirm sequence for set_max/set_min (NO_SEQ = fire-and-forget)
};

// Cmd::seq sentinel: this command carries no confirm request, so the node must
// NOT ACK it (backward compatible - a limit sent by old PC code omits `seq`).
// The PC's per-node sequence wraps within [0, 0xFFFE] so it never uses this.
constexpr uint16_t NO_SEQ = 0xFFFF;

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

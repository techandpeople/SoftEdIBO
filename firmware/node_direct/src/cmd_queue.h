#pragma once
#include <Arduino.h>

// Lock-free single-producer / single-consumer ring buffer for ESP-NOW commands.
// Producer: WiFi/ESP-NOW callback. Consumer: Arduino loop().

namespace cmd_queue {

enum CmdType : uint8_t {
    CMD_NONE = 0, CMD_INFLATE, CMD_DEFLATE, CMD_SET_PRESSURE,
    CMD_SET_MAX, CMD_HOLD, CMD_PING
#ifdef DEBUG_BUILD
    , CMD_DEBUG
#endif
};

struct Cmd {
    CmdType type;
    int8_t  chamber;
    int16_t param;
    float   param_kpa;
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

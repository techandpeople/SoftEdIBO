#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#include "se_espnow.h"
#include "cmd_queue.h"
#include "chambers.h"
#include "leds.h"
#include "pins.h"
#include "units.h"
#include "dbg.h"

namespace commands {

// Gateway MAC tracking + tx counters live in the shared ESP-NOW layer.
using se::node::gatewayMac;
using se::node::gatewayKnown;

#ifdef DEBUG_BUILD
inline uint32_t cmdDropped = 0;
#endif

inline void sendStatus(int ch, float kpa) {
    if (!gatewayKnown) return;
    int  pct = units::kpaToPct(kpa, chambers::state[ch].min_kpa, chambers::state[ch].max_kpa);
    char buf[48];
    int  len = snprintf(buf, sizeof(buf),
                        "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}", ch, pct);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

inline void sendPong() {
    if (!gatewayKnown) return;
    static const char pong[] = "{\"type\":\"pong\"}";
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(pong), sizeof(pong) - 1);
}

#ifdef DEBUG_BUILD
inline void sendDebug() {
    if (!gatewayKnown) return;
    char buf[64 + NUM_CHAMBERS * 96];
    int  pos = 0;
    pos += snprintf(buf + pos, sizeof(buf) - pos,
        "{\"type\":\"debug\",\"num_chambers\":%d,\"ch\":[", NUM_CHAMBERS);
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (i > 0) buf[pos++] = ',';
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "{\"s\":%d,\"kpa\":%.2f,\"tgt\":%.2f,\"min\":%.2f,\"max\":%.2f}",
            chambers::state[i].state, chambers::cachedKpa[i],
            chambers::state[i].target_kpa,
            chambers::state[i].min_kpa,
            chambers::state[i].max_kpa);
    }
    pos += snprintf(buf + pos, sizeof(buf) - pos,
        "],\"tx_ok\":%lu,\"tx_fail\":%lu,\"drop\":%lu,\"up\":%lu}",
        se::txOk, se::txFail, cmdDropped, millis() / 1000);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), pos);
}
#endif

inline void process(const cmd_queue::Cmd& c) {
    using namespace cmd_queue;
    if (c.type == CMD_PING)  { sendPong();  return; }
#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) { sendDebug(); return; }
#endif

    int n = c.chamber;
    if (n < 0 || n >= NUM_CHAMBERS) return;

    auto& ch = chambers::state[n];

    switch (c.type) {
    case CMD_INFLATE: {
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = min(chambers::cachedKpa[n] + delta, ch.max_kpa);
        chambers::beginInflate(n, chambers::DEFAULT_INFLATE_DUTY, target);
        break;
    }
    case CMD_DEFLATE: {
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = max(chambers::cachedKpa[n] - delta, ch.min_kpa);
        chambers::beginDeflate(n, target);
        break;
    }
    case CMD_SET_PRESSURE: {
        float target = units::pctToKpa(constrain(c.param, 0, 100),
                                       ch.min_kpa, ch.max_kpa);
        if      (chambers::cachedKpa[n] < target)
            chambers::beginInflate(n, chambers::DEFAULT_INFLATE_DUTY, target);
        else if (chambers::cachedKpa[n] > target)
            chambers::beginDeflate(n, target);
        else { chambers::stop(n); chambers::recalcPumps(); }
        break;
    }
    case CMD_SET_MAX: {
        float new_max = constrain(c.param_kpa, ch.min_kpa + 0.1f, chambers::HARD_MAX_KPA);
        ch.max_kpa = new_max;
        if (ch.state == chambers::INFLATING && chambers::cachedKpa[n] >= ch.max_kpa) {
            chambers::stop(n);
            chambers::recalcPumps();
        }
        break;
    }
    case CMD_SET_MIN: {
        float new_min = constrain(c.param_kpa, chambers::HARD_MIN_KPA, ch.max_kpa - 0.1f);
        ch.min_kpa = new_min;
        if (ch.state == chambers::DEFLATING && chambers::cachedKpa[n] <= ch.min_kpa) {
            chambers::stop(n);
            chambers::recalcPumps();
        }
        break;
    }
    case CMD_HOLD:
        chambers::stop(n);
        chambers::recalcPumps();
        break;
    default:
        break;
    }
}

inline void parseAndQueue(const uint8_t* data, int len) {
    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return;

    using namespace cmd_queue;
    const char* cmd = doc["cmd"] | "";
    Cmd c{};

    if      (strcmp(cmd, "ping") == 0)             { c.type = CMD_PING;         c.chamber = -1; }
    else if (strcmp(cmd, "inflate") == 0)           { c.type = CMD_INFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "deflate") == 0)           { c.type = CMD_DEFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "set_pressure") == 0)      { c.type = CMD_SET_PRESSURE; c.chamber = doc["chamber"] | -1; c.param = doc["value"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0)  { c.type = CMD_SET_MAX;      c.chamber = doc["chamber"] | -1; c.param_kpa = doc["value"] | chambers::DEFAULT_MAX_KPA; }
    else if (strcmp(cmd, "set_min_pressure") == 0)  { c.type = CMD_SET_MIN;      c.chamber = doc["chamber"] | -1; c.param_kpa = doc["value"] | chambers::DEFAULT_MIN_KPA; }
    else if (strcmp(cmd, "hold") == 0)              { c.type = CMD_HOLD;         c.chamber = doc["chamber"] | -1; }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)             { c.type = CMD_DEBUG;        c.chamber = -1; }
#endif
    else if (strcmp(cmd, "set_led") == 0) {
        // Handled inline (not queued): just stores the target LED state, which
        // loop()'s leds::update() animates. {"cmd":"set_led","color":"#RRGGBB",
        // "pattern":"off|solid|blink|pulse","period_ms":N,"count":N}
        const char* col = doc["color"]   | "#000000";
        const char* pat = doc["pattern"]  | "solid";
        uint32_t period = doc["period_ms"] | 0;
        int32_t  count  = doc["count"]     | 0;
        uint8_t r = 0, g = 0, b = 0;
        if (col[0] == '#' && strlen(col) >= 7) {
            long v = strtol(col + 1, nullptr, 16);
            r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
        }
        int idx = doc["index"] | -1;
        if (idx >= 0) leds::setPixel(idx, r, g, b);   // single pixel (test panel)
        else          leds::set(r, g, b, leds::patternFromStr(pat), period, count);
        return;
    }
    else return;

    if (!push(c)) {
#ifdef DEBUG_BUILD
        cmdDropped++;
#endif
    }
}

}  // namespace commands

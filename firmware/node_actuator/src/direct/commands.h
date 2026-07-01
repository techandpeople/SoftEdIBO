#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#include "se_espnow.h"
#include "cmd_queue.h"
#include "chambers.h"
#include "leds.h"
#include "magnet.h"
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
    // "st" is the real actuation state (0 idle, 1 inflating, 2 deflating) so the
    // PC reflects whether a pump is actually driving the chamber rather than
    // inferring it from pressure-vs-target (which never settles with pumps off).
    // "vi"/"vd" are the ACTUAL inflate/deflate valve outputs (chambers::valveOpen,
    // the mirror every valve write goes through), so the PC shows a valve as open
    // whoever opened it — a manual toggle, an inflate/deflate, the closed-loop
    // control, or the firmware's own dead-man closing it again.
    char buf[96];
    int  len = snprintf(buf, sizeof(buf),
                        "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d,\"kpa\":%.2f,\"st\":%d,\"vi\":%d,\"vd\":%d}",
                        ch, pct, kpa, (int)chambers::state[ch].state,
                        chambers::valveOpen[ch * 2 + 0] ? 1 : 0,
                        chambers::valveOpen[ch * 2 + 1] ? 1 : 0);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

inline void sendPong() {
    if (!gatewayKnown) return;
    static const char pong[] = "{\"type\":\"pong\",\"rgbw\":" LED_RGBW_JSON "}";
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(pong), sizeof(pong) - 1);
}

// Echo back that a command actually reached the node (used to tell a lost
// gateway->node ESP-NOW frame apart from a frame that arrived but didn't act).
inline void sendAck(const char* cmd) {
    if (!gatewayKnown) return;
    char buf[48];
    int  len = snprintf(buf, sizeof(buf), "{\"type\":\"ack\",\"cmd\":\"%s\"}", cmd);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// Wireless diagnostic for Inflate/Deflate-All: the live engine phase + the set of
// chambers each engine still wants (pendingMask), plus per-chamber kpa/max. Sent
// over ESP-NOW so it shows in the PC log without a USB cable. Lets us see why a
// chamber did/didn't actuate and watch the round/measure cycle shrink the set.
inline void sendSeq() {
    if (!gatewayKnown) return;
    char buf[220];
    int  pos = snprintf(buf, sizeof(buf),
        "{\"type\":\"seq\",\"inf_ph\":%d,\"inf_mask\":%d,\"def_ph\":%d,\"def_mask\":%d,\"ch\":[",
        chambers::inflateEng.phase, chambers::inflateEng.pendingMask,
        chambers::deflateEng.phase, chambers::deflateEng.pendingMask);
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (i) buf[pos++] = ',';
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "{\"k\":%.1f,\"mx\":%.1f}",
            chambers::cachedKpa[i], chambers::state[i].max_kpa);
    }
    pos += snprintf(buf + pos, sizeof(buf) - pos, "]}");
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), pos);
}

#ifdef DEBUG_BUILD
// Which valves are open the moment an actuation command arrives — the info the
// user explicitly wants kept (e.g. "a new inflate landed while these were still
// open"). Streamed over ESP-NOW so it lands in the PC log without a cable.
inline void sendRxOpen(const char* cmd, int chamber) {
    if (!gatewayKnown) return;
    uint16_t inf = 0, def = 0;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (chambers::valveOpen[i * 2 + 0]) inf |= (uint16_t)(1u << i);
        if (chambers::valveOpen[i * 2 + 1]) def |= (uint16_t)(1u << i);
    }
    char buf[112];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"rx\",\"cmd\":\"%s\",\"ch\":%d,\"open_inf\":%d,\"open_def\":%d}",
        cmd, chamber, inf, def);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// Engine round/measure trace (coupled_fill::Event). Registered as chambers::dbgHook.
inline void sendEngEvent(uint8_t ev, uint16_t mask, uint8_t dir) {
    if (!gatewayKnown) return;
    char buf[80];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"eng\",\"dir\":%d,\"code\":%d,\"mask\":%d}", dir, ev, mask);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// A pump must never spin without an open valve of its direction (the "running
// dry / forcing" failure). recalcPumps() makes that impossible by construction,
// so this only fires on a regression — report it loudly over ESP-NOW.
inline void checkDryPumps() {
    if (!gatewayKnown) return;
    bool anyInf = false, anyDef = false;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (chambers::valveOpen[i * 2 + 0]) anyInf = true;
        if (chambers::valveOpen[i * 2 + 1]) anyDef = true;
    }
    bool dryInf = ledcRead(chambers::PUMP1_LEDC_CH) > 0 && !anyInf;
    bool dryDef = ledcRead(chambers::PUMP2_LEDC_CH) > 0 && !anyDef;
    if (!dryInf && !dryDef) return;
    char buf[80];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"dry\",\"inf\":%d,\"def\":%d}", dryInf, dryDef);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

inline void installDebugHook() { chambers::dbgHook = &sendEngEvent; }
#endif

// Report the live pump PWM duties (read straight from the LEDC registers, so it
// reflects whatever last drove them — recalcPumps, manual, or emergencyStopAll).
// Lets the PC see whether/when the firmware actually cut the pumps.
inline void sendPumps() {
    if (!gatewayKnown) return;
    char buf[48];
    int  len = snprintf(buf, sizeof(buf), "{\"type\":\"pumps\",\"inf\":%u,\"def\":%u}",
                        (unsigned)ledcRead(chambers::PUMP1_LEDC_CH),
                        (unsigned)ledcRead(chambers::PUMP2_LEDC_CH));
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
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

// Actuation commands that a ``chamber == -1`` target fans out to every chamber in
// parallel (one frame, not one per chamber). Inflate and Deflate are NOT here —
// "Inflate/Deflate All" run the two-phase chambers::inflateAll / deflateAll (coarse
// parallel + isolated finish when 2+ chambers actuate) because the in-line gauges
// read the shared line, not each chamber, while several valves are open. Only
// set_pressure / hold-all stay a plain fan-out (no precise per-chamber target on the
// coupled line). Limit commands (set_max/set_min) and the manual bench controls are
// excluded; they stay single-target.
inline bool isFanOut(cmd_queue::CmdType t) {
    using namespace cmd_queue;
    return t == CMD_SET_PRESSURE || t == CMD_HOLD;
}

// Apply one already-parsed command to a single (assumed valid) chamber ``n``.
// Split out of process() so a ``chamber == -1`` actuation can fan out to every
// chamber in one pass.
inline void applyChamberCmd(int n, const cmd_queue::Cmd& c) {
    using namespace cmd_queue;
    auto& ch = chambers::state[n];

    // duty 0 (unset) -> full speed. (Direct runs its single pump on/off at full
    // duty; the value is kept for telemetry.) ``fill_ms`` is no longer a separate
    // time-fill path: the engine's coupled measure gives a trustworthy per-chamber
    // pressure, so all targeting is pressure-based, with the engine's chamber_max_ms
    // as the time backstop.
    switch (c.type) {
    case CMD_INFLATE: {
        uint8_t duty = c.duty ? c.duty : chambers::DEFAULT_INFLATE_DUTY;
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = min(chambers::cachedKpa[n] + delta, ch.max_kpa);
        // Only actuate if actually below target (else a repeated "+" at the cap
        // would creep past max one round at a time).
        if (chambers::cachedKpa[n] < target)
            chambers::requestInflate(n, target, duty);
        break;
    }
    case CMD_DEFLATE: {
        uint8_t duty = c.duty ? c.duty : chambers::DEFAULT_DEFLATE_DUTY;
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = max(chambers::cachedKpa[n] - delta, ch.min_kpa);
        if (chambers::cachedKpa[n] > target)
            chambers::requestDeflate(n, target, duty);
        break;
    }
    case CMD_SET_PRESSURE: {
        float target = units::pctToKpa(constrain(c.param, 0, 100),
                                       ch.min_kpa, ch.max_kpa);
        if      (chambers::cachedKpa[n] < target)
            chambers::requestInflate(n, target, c.duty ? c.duty : chambers::DEFAULT_INFLATE_DUTY);
        else if (chambers::cachedKpa[n] > target)
            chambers::requestDeflate(n, target, c.duty ? c.duty : chambers::DEFAULT_DEFLATE_DUTY);
        else chambers::holdChamber(n);
        break;
    }
    case CMD_SET_MAX: {
        float new_max = constrain(c.param_kpa, ch.min_kpa + 0.1f, chambers::HARD_MAX_KPA);
        ch.max_kpa = new_max;
        if (ch.state == chambers::INFLATING && chambers::cachedKpa[n] >= ch.max_kpa)
            chambers::holdChamber(n);
        break;
    }
    case CMD_SET_MIN: {
        float new_min = constrain(c.param_kpa, chambers::HARD_MIN_KPA, ch.max_kpa - 0.1f);
        ch.min_kpa = new_min;
        if (ch.state == chambers::DEFLATING && chambers::cachedKpa[n] <= ch.min_kpa)
            chambers::holdChamber(n);
        break;
    }
    case CMD_HOLD:
        chambers::holdChamber(n);
        break;
    case CMD_VALVE_MANUAL: {
        // chamber = chamber, param = side (0=inflate, 1=deflate), cfg_chambers = open (0/1)
        // Manual (dev) override takes the chamber out of the engine's hands.
        chambers::inflateEng.drop(n, [](int i) { chambers::stop(i); });
        chambers::deflateEng.drop(n, [](int i) { chambers::stop(i); });
        chambers::setManualValve(n, c.param, c.cfg_chambers != 0);
        break;
    }
    case CMD_PUMP_MANUAL: {
        // param = pump (0=inflate, 1=deflate), cfg_chambers = on (0/1)
        chambers::setManualPump(c.param, c.cfg_chambers != 0);
        break;
    }
    default:
        break;
    }
}

inline void process(const cmd_queue::Cmd& c) {
    using namespace cmd_queue;
    if (c.type == CMD_PING)  { sendPong();  return; }
#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) { sendDebug(); return; }
#endif

    // Emergency stop / re-arm: handled regardless of chamber, even while stopped.
    if (c.type == CMD_STOP)   { chambers::stopped = true;  chambers::emergencyStopAll(); sendAck("stop");   sendPumps(); return; }
    if (c.type == CMD_RESUME) { chambers::stopped = false; sendAck("resume"); return; }

    // Stop a continuous bench-test run: honoured even while latched stopped.
    if (c.type == CMD_TEST_STOP) { chambers::testStop(); sendAck("test_stop"); sendPumps(); return; }

    // While latched stopped, drop every actuation command so nothing re-actuates.
    if (chambers::stopped) return;

    // Start a continuous bench-test run (param = direction). Targetless, so it
    // must be handled before the per-chamber index guard below. The bench test
    // owns the hardware directly, so drop any engine sequence first.
    if (c.type == CMD_TEST_RUN) {
        chambers::inflateEng.abort();
        chambers::deflateEng.abort();
        chambers::testRun(c.param, c.chamber); sendAck("test_run"); sendPumps(); return;
    }

#ifdef DEBUG_BUILD
    // Record which valves are open the instant an actuation command lands.
    if (c.type == CMD_INFLATE || c.type == CMD_DEFLATE
        || c.type == CMD_SET_PRESSURE || c.type == CMD_HOLD) {
        const char* name = c.type == CMD_INFLATE      ? "inflate"
                         : c.type == CMD_DEFLATE      ? "deflate"
                         : c.type == CMD_SET_PRESSURE ? "set_pressure" : "hold";
        sendRxOpen(name, c.chamber);
    }
#endif

    // chamber == -1 actuates EVERY chamber. Inflate/Deflate-All request every
    // chamber that still needs to move; the engine batches them into one coupled
    // round (open together → fill to lowest target → close → measure isolated).
    int n = c.chamber;
    if (n == -1 && c.type == CMD_INFLATE) {
        chambers::inflateAll(c.param);
        sendSeq();              // wireless diagnostic: engine phase + pending masks
        return;
    }
    if (n == -1 && c.type == CMD_DEFLATE) {
        chambers::deflateAll(c.param);
        sendSeq();
        return;
    }
    if (n == -1 && isFanOut(c.type)) {
        // set_pressure / hold-all fan out per chamber; each routes through the
        // engine (which groups co-active same-direction chambers itself).
        for (int i = 0; i < NUM_CHAMBERS; i++) applyChamberCmd(i, c);
        return;
    }
    if (n < 0 || n >= NUM_CHAMBERS) return;
    applyChamberCmd(n, c);
}

inline void parseAndQueue(const uint8_t* data, int len) {
    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return;

    using namespace cmd_queue;
    const char* cmd = doc["cmd"] | "";
    Cmd c{};

    if      (strcmp(cmd, "ping") == 0)             { c.type = CMD_PING;         c.chamber = -1; }
    else if (strcmp(cmd, "inflate") == 0)           { c.type = CMD_INFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; c.fill_ms = doc["ms"] | 0; c.duty = doc["duty"] | 0; }
    else if (strcmp(cmd, "deflate") == 0)           { c.type = CMD_DEFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; c.fill_ms = doc["ms"] | 0; c.duty = doc["duty"] | 0; }
    else if (strcmp(cmd, "set_pressure") == 0)      { c.type = CMD_SET_PRESSURE; c.chamber = doc["chamber"] | -1; c.param = doc["value"] | 0; c.duty = doc["duty"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0)  { c.type = CMD_SET_MAX;      c.chamber = doc["chamber"] | -1; c.param_kpa = doc["value"] | chambers::DEFAULT_MAX_KPA; }
    else if (strcmp(cmd, "set_min_pressure") == 0)  { c.type = CMD_SET_MIN;      c.chamber = doc["chamber"] | -1; c.param_kpa = doc["value"] | chambers::DEFAULT_MIN_KPA; }
    else if (strcmp(cmd, "hold") == 0)              { c.type = CMD_HOLD;         c.chamber = doc["chamber"] | -1; }
    else if (strcmp(cmd, "stop") == 0)              { c.type = CMD_STOP;         c.chamber = -1; }
    else if (strcmp(cmd, "resume") == 0)            { c.type = CMD_RESUME;       c.chamber = -1; }
    else if (strcmp(cmd, "test_run") == 0)          { c.type = CMD_TEST_RUN;     c.chamber = doc["chamber"] | -1; c.param = doc["dir"] | 0; }  // 0=inflate, 1=deflate; chamber -1 = all
    else if (strcmp(cmd, "test_stop") == 0)         { c.type = CMD_TEST_STOP;    c.chamber = -1; }
    else if (strcmp(cmd, "valve_manual") == 0) {
        c.type = CMD_VALVE_MANUAL;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["side"] | 0;     // 0=inflate, 1=deflate
        c.cfg_chambers = doc["open"] | 0;
    }
    else if (strcmp(cmd, "pump_manual") == 0) {
        c.type = CMD_PUMP_MANUAL;
        c.param = doc["pump"] | 0;     // 0=inflate, 1=deflate
        c.cfg_chambers = doc["on"] | 0;
    }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)             { c.type = CMD_DEBUG;        c.chamber = -1; }
#endif
    else if (strcmp(cmd, "set_led") == 0) {
        // Handled inline (not queued): just stores the target LED state, which
        // loop()'s leds::update() renders. {"cmd":"set_led","color":"#RRGGBB",
        // "pattern":"off|solid|blink|pulse|comet","period_ms":N,"count":N,"fade_ms":N}
        const char* col = doc["color"]   | "#000000";
        const char* pat = doc["pattern"]  | "solid";
        uint32_t period = doc["period_ms"] | 0;
        int32_t  count  = doc["count"]     | 0;
        uint32_t fade   = doc["fade_ms"]   | leds::DEFAULT_FADE_MS;
        float    offset = ((float)(doc["angle"] | 0.0f)) / 360.0f;   // split/comet rotation
        uint8_t r, g, b;
        leds::parseHexColor(col, r, g, b);
        if (strcmp(pat, "off") == 0) { r = g = b = 0; }   // "off" = dark, any colour
        int idx = doc["index"] | -1;
        if (idx >= 0) leds::setPixel(idx, r, g, b, fade);   // single pixel (test panel)
        else          leds::setAll(r, g, b, leds::patternFromStr(pat), period, count, fade, offset);
        return;
    }
    else if (strcmp(cmd, "set_led_halves") == 0) {
        // Split the ring into len(colors) equal arcs (the purple/yellow look) in
        // ONE frame. Replaces the PC's old per-pixel burst — 24 set_led frames
        // that reset the node by calling strip.show() once per pixel in the recv
        // task. {"cmd":"set_led_halves","colors":["#RRGGBB",...],"pattern":...,
        // "fade_ms":N}  pattern "comet" paints one comet per colour.
        const char* pat = doc["pattern"]   | "solid";
        uint32_t period = doc["period_ms"] | 0;
        int32_t  count  = doc["count"]     | 0;
        uint32_t fade   = doc["fade_ms"]   | leds::DEFAULT_FADE_MS;
        float    offset = ((float)(doc["angle"] | 0.0f)) / 360.0f;   // rotate the split
        uint8_t r[leds::MAX_SEGMENTS], g[leds::MAX_SEGMENTS], b[leds::MAX_SEGMENTS];
        int k = 0;
        for (JsonVariant v : doc["colors"].as<JsonArray>()) {
            if (k >= leds::MAX_SEGMENTS) break;
            leds::parseHexColor(v.as<const char*>(), r[k], g[k], b[k]);
            k++;
        }
        if (k > 0) leds::setSegments(r, g, b, k, leds::patternFromStr(pat), period, count, fade, offset);
        return;
    }
    // ---- Magnet/touch board commands (handled inline, not queued) ----
    // Match what the PC's touch tuning panel sends; no-ops if no sensors wired.
    else if (strcmp(cmd, "rebaseline") == 0) { magnet::resetBaseline();   return; }
    else if (strcmp(cmd, "configure") == 0)  { magnet::applyConfigure(doc); return; }
    else return;

    if (!push(c)) {
#ifdef DEBUG_BUILD
        cmdDropped++;
#endif
    }
}

}  // namespace commands

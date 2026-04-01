/**
 * SoftEdIBO — Multiplexed Reservoir Node Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Build environments (platformio.ini):
 *   pio run              -> release  (no debug overhead, no Serial output)
 *   pio run -e debug     -> debug    (Serial logs + "debug" command via ESP-NOW)
 *
 * NUM_TANKS: compile-time constant set via build flag (e.g. -DNUM_TANKS=2).
 *   Default: 2 (one pressure tank + one vacuum tank on the same ESP32).
 *   Max: 8 with a single 74HC4051 sensor mux.
 *
 * This node manages NUM_TANKS independent air tanks.
 * Each tank i has one inflate pump relay, one deflate pump relay (controlled
 * via a 74HC595 shift register), and one pressure sensor (read via a 74HC4051
 * analog mux). The firmware runs pump relays autonomously to hold a target
 * pressure for each tank.
 *
 * Shift register bit layout per tank i:
 *   bit(i*2)   = inflate pump relay
 *   bit(i*2+1) = deflate pump relay
 *
 * Commands received (ESP-NOW, JSON) — "chamber" selects tank index:
 *   {"cmd":"inflate","chamber":0,"delta":20}
 *   {"cmd":"deflate","chamber":0,"delta":20}
 *   {"cmd":"set_pressure","chamber":0,"value":80}
 *   {"cmd":"set_max_pressure","chamber":0,"value":9.5}
 *   {"cmd":"hold","chamber":0}
 *   {"cmd":"ping"}
 *   {"cmd":"debug"}   (debug build only)
 *
 * Status sent back every STATUS_REPORT_MS for each tank:
 *   {"type":"status","chamber":0,"pressure":pct}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

#include "pins.h"
#include "pressure.h"

#ifndef NUM_TANKS
#error "NUM_TANKS must be defined via build flags (e.g. -DNUM_TANKS=2)"
#endif

static_assert(NUM_TANKS >= 1 && NUM_TANKS <= 8,
              "NUM_TANKS must be between 1 and 8 for single 74HC4051 sensor mux.");

// ---------------------------------------------------------------------------
// Debug macros
// ---------------------------------------------------------------------------

#ifdef DEBUG_BUILD
  #define DBG_PRINT(...)  Serial.printf(__VA_ARGS__)
  #define DBG_PRINTLN(s)  Serial.println(s)
#else
  #define DBG_PRINT(...)  ((void)0)
  #define DBG_PRINTLN(s)  ((void)0)
#endif

// ---------------------------------------------------------------------------
// Tuning constants
// ---------------------------------------------------------------------------

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float HARD_MAX_KPA    = 12.0f;

constexpr uint32_t PRESSURE_CHECK_MS = 200;
constexpr uint32_t STATUS_REPORT_MS  = 500;
constexpr uint32_t SMUX_SETTLE_US    =  10;   // 74HC4051 channel-select settle time

// ---------------------------------------------------------------------------
// kPa <-> percentage helpers
// ---------------------------------------------------------------------------

static inline float pctToKpa(int pct, float ref_kpa) {
    return constrain(pct, 0, 100) * max(0.0f, ref_kpa) / 100.0f;
}

static inline int kpaToPct(float kpa, float ref_kpa) {
    if (kpa <= 0.0f || ref_kpa <= 0.0f) return 0;
    return min(static_cast<int>(kpa * 100.0f / ref_kpa + 0.5f), 100);
}

// ---------------------------------------------------------------------------
// Command queue
// ---------------------------------------------------------------------------

enum CmdType : uint8_t {
    CMD_NONE = 0, CMD_INFLATE, CMD_DEFLATE, CMD_SET_PRESSURE,
    CMD_SET_MAX, CMD_HOLD, CMD_PING
#ifdef DEBUG_BUILD
    , CMD_DEBUG
#endif
};

struct Cmd {
    CmdType type;
    int8_t  tank;
    int16_t param;
    float   param_kpa;
};

static constexpr uint8_t QUEUE_MASK = 0x0F;
static Cmd      cmdQueue[QUEUE_MASK + 1];
static volatile uint8_t qHead = 0;
static volatile uint8_t qTail = 0;

static inline bool queuePush(const Cmd& c) {
    uint8_t next = (qHead + 1) & QUEUE_MASK;
    if (next == qTail) return false;
    cmdQueue[qHead] = c;
    qHead = next;
    return true;
}

static inline bool queuePop(Cmd& c) {
    if (qTail == qHead) return false;
    c = cmdQueue[qTail];
    qTail = (qTail + 1) & QUEUE_MASK;
    return true;
}

// ---------------------------------------------------------------------------
// Per-tank state
// ---------------------------------------------------------------------------

enum TankState : uint8_t { TANK_IDLE, TANK_INFLATING, TANK_DEFLATING };

#ifdef DEBUG_BUILD
static const char* stateStr(TankState s) {
    switch (s) {
        case TANK_IDLE:      return "IDLE";
        case TANK_INFLATING: return "INFLATING";
        case TANK_DEFLATING: return "DEFLATING";
        default:             return "?";
    }
}
#endif

struct Tank {
    TankState state      = TANK_IDLE;
    float     target_kpa = 0.0f;
    float     max_kpa    = DEFAULT_MAX_KPA;
};

static Tank  tanks[NUM_TANKS];
static float cachedKpa[NUM_TANKS] = {};

static uint8_t  gatewayMac[6] = {};
static bool     gatewayKnown  = false;
static uint32_t lastPressureMs = 0;
static uint32_t lastStatusMs   = 0;

#ifdef DEBUG_BUILD
static uint32_t sendOk = 0, sendFail = 0, cmdDropped = 0;
#endif

// ---------------------------------------------------------------------------
// Shift register — pump relay control
// Bit layout per tank i: bit(i*2) = inflate relay, bit(i*2+1) = deflate relay
// ---------------------------------------------------------------------------

static uint32_t srState = 0;

static void flushShiftReg() {
    constexpr int BYTES = (NUM_TANKS * 2 + 7) / 8;
    digitalWrite(SR_LATCH_PIN, LOW);
    for (int b = BYTES - 1; b >= 0; b--)
        shiftOut(SR_DATA_PIN, SR_CLK_PIN, MSBFIRST, uint8_t(srState >> (b * 8)));
    digitalWrite(SR_LATCH_PIN, HIGH);
}

static void setRelay(int tank, int side, bool on) {
    int bit = tank * 2 + side;
    if (on) srState |=  (1UL << bit);
    else    srState &= ~(1UL << bit);
    flushShiftReg();
}

static void stopTankRelays(int t) {
    setRelay(t, 0, false);
    setRelay(t, 1, false);
    tanks[t].state = TANK_IDLE;
}

// ---------------------------------------------------------------------------
// Sensor mux (74HC4051) — read one tank's pressure
// ---------------------------------------------------------------------------

static float readTankKpa(int t) {
    digitalWrite(SMUX_S0_PIN, (t & 1) ? HIGH : LOW);
    digitalWrite(SMUX_S1_PIN, (t & 2) ? HIGH : LOW);
    digitalWrite(SMUX_S2_PIN, (t & 4) ? HIGH : LOW);
    delayMicroseconds(SMUX_SETTLE_US);
    return pressure::readKpa(SMUX_SIG_PIN);
}

// ---------------------------------------------------------------------------
// Tank helpers
// ---------------------------------------------------------------------------

static void startInflate(int t, float tgt) {
    tanks[t].target_kpa = min(tgt, tanks[t].max_kpa);
    tanks[t].state      = TANK_INFLATING;
    setRelay(t, 1, false);   // ensure deflate off
    setRelay(t, 0, true);    // inflate on
    DBG_PRINT("TANK%d INFLATE tgt=%.2f kPa (cur=%.2f)\n", t, tanks[t].target_kpa, cachedKpa[t]);
}

static void startDeflate(int t, float tgt) {
    tanks[t].target_kpa = max(tgt, 0.0f);
    tanks[t].state      = TANK_DEFLATING;
    setRelay(t, 0, false);   // ensure inflate off
    setRelay(t, 1, true);    // deflate on
    DBG_PRINT("TANK%d DEFLATE tgt=%.2f kPa (cur=%.2f)\n", t, tanks[t].target_kpa, cachedKpa[t]);
}

static void sendStatus(int t) {
    if (!gatewayKnown) return;
    int  pct = kpaToPct(cachedKpa[t], tanks[t].max_kpa);
    char buf[48];
    int  len = snprintf(buf, sizeof(buf),
                        "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}", t, pct);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// ---------------------------------------------------------------------------
// Process one queued command
// ---------------------------------------------------------------------------

static void processCommand(const Cmd& c) {
    if (c.type == CMD_PING) {
        if (!gatewayKnown) return;
        static const char pong[] = "{\"type\":\"pong\"}";
        esp_now_send(gatewayMac,
                     reinterpret_cast<const uint8_t*>(pong), sizeof(pong) - 1);
        return;
    }

#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) {
        if (!gatewayKnown) return;
        char buf[64 + NUM_TANKS * 96];
        int  pos = 0;
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "{\"type\":\"debug\",\"num_tanks\":%d"
            ",\"defaults\":{\"max_kpa\":%.1f,\"hard_max_kpa\":%.1f}"
            ",\"tanks\":[",
            NUM_TANKS, DEFAULT_MAX_KPA, HARD_MAX_KPA);
        for (int i = 0; i < NUM_TANKS; i++) {
            if (i > 0) buf[pos++] = ',';
            pos += snprintf(buf + pos, sizeof(buf) - pos,
                "{\"s\":%d,\"st\":\"%s\",\"kpa\":%.3f,\"pct\":%d"
                ",\"tgt_kpa\":%.3f,\"max_kpa\":%.3f}",
                tanks[i].state, stateStr(tanks[i].state),
                cachedKpa[i], kpaToPct(cachedKpa[i], tanks[i].max_kpa),
                tanks[i].target_kpa, tanks[i].max_kpa);
        }
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "],\"tx_ok\":%lu,\"tx_fail\":%lu,\"drop\":%lu,\"up\":%lu}",
            sendOk, sendFail, cmdDropped, millis() / 1000);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), pos);
        return;
    }
#endif

    int t = c.tank;
    if (t < 0 || t >= NUM_TANKS) {
        DBG_PRINT("WARN: invalid tank %d (max %d), dropping\n", t, NUM_TANKS - 1);
        return;
    }

    switch (c.type) {
    case CMD_INFLATE: {
        float delta = pctToKpa(constrain(c.param, 0, 100), tanks[t].max_kpa);
        float tgt   = min(cachedKpa[t] + delta, tanks[t].max_kpa);
        if (cachedKpa[t] < tgt) startInflate(t, tgt);
        break;
    }
    case CMD_DEFLATE: {
        float delta = pctToKpa(constrain(c.param, 0, 100), tanks[t].max_kpa);
        float tgt   = max(cachedKpa[t] - delta, 0.0f);
        if (cachedKpa[t] > tgt) startDeflate(t, tgt);
        break;
    }
    case CMD_SET_PRESSURE: {
        float tgt = pctToKpa(constrain(c.param, 0, 100), tanks[t].max_kpa);
        if      (cachedKpa[t] < tgt) startInflate(t, tgt);
        else if (cachedKpa[t] > tgt) startDeflate(t, tgt);
        else stopTankRelays(t);
        break;
    }
    case CMD_SET_MAX: {
        float new_max = constrain(c.param_kpa, 0.1f, HARD_MAX_KPA);
        DBG_PRINT("TANK%d set_max %.2f -> %.2f kPa\n", t, tanks[t].max_kpa, new_max);
        tanks[t].max_kpa = new_max;
        if (tanks[t].state == TANK_INFLATING && cachedKpa[t] >= tanks[t].max_kpa)
            stopTankRelays(t);
        break;
    }
    case CMD_HOLD:
        DBG_PRINT("TANK%d HOLD\n", t);
        stopTankRelays(t);
        break;
    default:
        break;
    }
}

// ---------------------------------------------------------------------------
// ESP-NOW callbacks
// ---------------------------------------------------------------------------

#ifdef DEBUG_BUILD
static void onSent(const uint8_t*, esp_now_send_status_t status) {
    if (status == ESP_NOW_SEND_SUCCESS) sendOk++; else sendFail++;
}
#endif

static void onReceived(const uint8_t* mac_addr, const uint8_t* data, int len) {
    if (!gatewayKnown) {
        memcpy(gatewayMac, mac_addr, 6);
        gatewayKnown = true;
        esp_now_peer_info_t peer{};
        memcpy(peer.peer_addr, gatewayMac, 6);
        peer.channel = 0;
        peer.encrypt = false;
        esp_now_add_peer(&peer);
    }

    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return;

    const char* cmd = doc["cmd"] | "";
    Cmd c{};
    c.tank = doc["chamber"] | 0;   // "chamber" field selects the tank index

    if      (strcmp(cmd, "ping") == 0)             { c.type = CMD_PING;         c.tank = -1; }
    else if (strcmp(cmd, "inflate") == 0)           { c.type = CMD_INFLATE;      c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "deflate") == 0)           { c.type = CMD_DEFLATE;      c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "set_pressure") == 0)      { c.type = CMD_SET_PRESSURE; c.param = doc["value"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0)  { c.type = CMD_SET_MAX;      c.param_kpa = doc["value"] | DEFAULT_MAX_KPA; }
    else if (strcmp(cmd, "hold") == 0)              { c.type = CMD_HOLD;         }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)             { c.type = CMD_DEBUG;        c.tank = -1; }
#endif
    else return;

    if (!queuePush(c)) {
#ifdef DEBUG_BUILD
        cmdDropped++;
#endif
    }
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
#ifdef DEBUG_BUILD
    Serial.begin(115200);
#endif

    // Shift register — all pump relays off
    pinMode(SR_DATA_PIN,  OUTPUT);
    pinMode(SR_CLK_PIN,   OUTPUT);
    pinMode(SR_LATCH_PIN, OUTPUT);
    srState = 0;
    flushShiftReg();

    // Sensor mux select pins
    pinMode(SMUX_S0_PIN, OUTPUT);
    pinMode(SMUX_S1_PIN, OUTPUT);
    pinMode(SMUX_S2_PIN, OUTPUT);

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    if (esp_now_init() != ESP_OK) {
        DBG_PRINTLN(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }
#ifdef DEBUG_BUILD
    esp_now_register_send_cb(onSent);
#endif
    esp_now_register_recv_cb(onReceived);

    for (int i = 0; i < NUM_TANKS; i++)
        cachedKpa[i] = readTankKpa(i);

    DBG_PRINTLN(F("{\"status\":\"mux_reservoir_node_ready\"}"));
    DBG_PRINT("NUM_TANKS=%d  DEFAULT_MAX_KPA=%.1f  HARD_MAX_KPA=%.1f"
              "  CHECK=%lums  STATUS=%lums\n",
              NUM_TANKS, DEFAULT_MAX_KPA, HARD_MAX_KPA,
              PRESSURE_CHECK_MS, STATUS_REPORT_MS);
}

void loop() {
    uint32_t now = millis();

    // ---- Process queued commands ----
    Cmd c;
    while (queuePop(c))
        processCommand(c);

    // ---- Pressure read + safety check ----
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;

        for (int i = 0; i < NUM_TANKS; i++)
            cachedKpa[i] = readTankKpa(i);

        for (int i = 0; i < NUM_TANKS; i++) {
            float kpa = cachedKpa[i];

            if (tanks[i].state == TANK_INFLATING &&
                (kpa >= tanks[i].target_kpa || kpa >= tanks[i].max_kpa)) {
                DBG_PRINT("TANK%d STOP inflate: %.2f >= tgt=%.2f / max=%.2f\n",
                          i, kpa, tanks[i].target_kpa, tanks[i].max_kpa);
                stopTankRelays(i);
            }

            if (tanks[i].state == TANK_DEFLATING && kpa <= tanks[i].target_kpa) {
                DBG_PRINT("TANK%d STOP deflate: %.2f <= tgt=%.2f\n",
                          i, kpa, tanks[i].target_kpa);
                stopTankRelays(i);
            }
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < NUM_TANKS; i++)
            sendStatus(i);

#ifdef DEBUG_BUILD
        for (int i = 0; i < NUM_TANKS; i++)
            DBG_PRINT("TANK%d  %s  %.2f kPa  tgt=%.2f  max=%.2f  pct=%d%%\n",
                      i, stateStr(tanks[i].state),
                      cachedKpa[i], tanks[i].target_kpa,
                      tanks[i].max_kpa, kpaToPct(cachedKpa[i], tanks[i].max_kpa));
        DBG_PRINT("tx ok=%lu fail=%lu drop=%lu  up=%lus\n",
                  sendOk, sendFail, cmdDropped, millis() / 1000);
#endif
    }
}

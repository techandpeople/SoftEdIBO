/**
 * SoftEdIBO — Reservoir Node Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Build environments (platformio.ini):
 *   pio run              -> release  (no debug overhead, no Serial output)
 *   pio run -e debug     -> debug    (Serial logs + "debug" command via ESP-NOW)
 *
 * This node manages a single central air reservoir (pressure or vacuum).
 * It drives one inflate pump and one deflate pump to maintain a target
 * pressure in the tank. Chamber nodes feed from this reservoir via a shared
 * air manifold; the firmware runs the pumps autonomously to hold the set point.
 *
 * Commands received (ESP-NOW, JSON) — "chamber" field is ignored (always 0):
 *   {"cmd":"inflate","delta":20}          — raise target by delta %
 *   {"cmd":"deflate","delta":20}          — lower target by delta %
 *   {"cmd":"set_pressure","value":80}     — set absolute target (%)
 *   {"cmd":"set_max_pressure","value":9.5}— update max kPa limit
 *   {"cmd":"hold"}                        — stop both pumps, freeze target
 *   {"cmd":"ping"}
 *   {"cmd":"debug"}                        (debug build only)
 *
 * Status sent back every STATUS_REPORT_MS:
 *   {"type":"status","chamber":0,"pressure":pct}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

#include "pins.h"
#include "pressure.h"

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

constexpr uint8_t DEFAULT_INFLATE_DUTY = 255;

constexpr uint32_t PRESSURE_CHECK_MS = 200;
constexpr uint32_t STATUS_REPORT_MS  = 500;

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr int PUMP_INF_CH   =     0;   // LEDC channel for inflate pump
constexpr int PUMP_DEF_CH   =     1;   // LEDC channel for deflate pump

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
// Reservoir state
// ---------------------------------------------------------------------------

enum ReservoirState : uint8_t { RES_IDLE, RES_INFLATING, RES_DEFLATING };

static ReservoirState resState   = RES_IDLE;
static float          target_kpa = 0.0f;
static float          max_kpa    = DEFAULT_MAX_KPA;
static float          cachedKpa  = 0.0f;

static uint8_t  gatewayMac[6] = {};
static bool     gatewayKnown  = false;
static uint32_t lastPressureMs = 0;
static uint32_t lastStatusMs   = 0;

#ifdef DEBUG_BUILD
static uint32_t sendOk = 0, sendFail = 0, cmdDropped = 0;
#endif

// ---------------------------------------------------------------------------
// Pump control
// ---------------------------------------------------------------------------

static void setPumps(uint8_t infDuty, uint8_t defDuty) {
    ledcWrite(PUMP_INF_CH, infDuty);
    ledcWrite(PUMP_DEF_CH, defDuty);
}

static void stopPumps() {
    setPumps(0, 0);
    resState = RES_IDLE;
}

static void startInflate(float tgt) {
    target_kpa = min(tgt, max_kpa);
    resState   = RES_INFLATING;
    setPumps(DEFAULT_INFLATE_DUTY, 0);
    DBG_PRINT("RESERVOIR INFLATE tgt=%.2f kPa (cur=%.2f)\n", target_kpa, cachedKpa);
}

static void startDeflate(float tgt) {
    target_kpa = max(tgt, 0.0f);
    resState   = RES_DEFLATING;
    setPumps(0, 255);
    DBG_PRINT("RESERVOIR DEFLATE tgt=%.2f kPa (cur=%.2f)\n", target_kpa, cachedKpa);
}

// ---------------------------------------------------------------------------
// Send status
// ---------------------------------------------------------------------------

static void sendStatus() {
    if (!gatewayKnown) return;
    int  pct = kpaToPct(cachedKpa, max_kpa);
    char buf[48];
    int  len = snprintf(buf, sizeof(buf),
                        "{\"type\":\"status\",\"chamber\":0,\"pressure\":%d}", pct);
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
        static const char* stateNames[] = {"IDLE","INFLATING","DEFLATING"};
        char buf[128];
        int  len = snprintf(buf, sizeof(buf),
            "{\"type\":\"debug\",\"state\":\"%s\",\"kpa\":%.3f,\"pct\":%d"
            ",\"tgt_kpa\":%.3f,\"max_kpa\":%.3f"
            ",\"tx_ok\":%lu,\"tx_fail\":%lu,\"drop\":%lu,\"up\":%lu}",
            stateNames[resState], cachedKpa, kpaToPct(cachedKpa, max_kpa),
            target_kpa, max_kpa,
            sendOk, sendFail, cmdDropped, millis() / 1000);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
        return;
    }
#endif

    switch (c.type) {
    case CMD_INFLATE: {
        float delta  = pctToKpa(constrain(c.param, 0, 100), max_kpa);
        float tgt    = min(cachedKpa + delta, max_kpa);
        if (cachedKpa < tgt) startInflate(tgt);
        break;
    }
    case CMD_DEFLATE: {
        float delta = pctToKpa(constrain(c.param, 0, 100), max_kpa);
        float tgt   = max(cachedKpa - delta, 0.0f);
        if (cachedKpa > tgt) startDeflate(tgt);
        break;
    }
    case CMD_SET_PRESSURE: {
        float tgt = pctToKpa(constrain(c.param, 0, 100), max_kpa);
        if      (cachedKpa < tgt) startInflate(tgt);
        else if (cachedKpa > tgt) startDeflate(tgt);
        else stopPumps();
        break;
    }
    case CMD_SET_MAX: {
        float new_max = constrain(c.param_kpa, 0.1f, HARD_MAX_KPA);
        DBG_PRINT("RESERVOIR set_max %.2f -> %.2f kPa\n", max_kpa, new_max);
        max_kpa = new_max;
        if (resState == RES_INFLATING && cachedKpa >= max_kpa) stopPumps();
        break;
    }
    case CMD_HOLD:
        DBG_PRINT("RESERVOIR HOLD\n");
        stopPumps();
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

    if      (strcmp(cmd, "ping") == 0)             { c.type = CMD_PING;         }
    else if (strcmp(cmd, "inflate") == 0)           { c.type = CMD_INFLATE;      c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "deflate") == 0)           { c.type = CMD_DEFLATE;      c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "set_pressure") == 0)      { c.type = CMD_SET_PRESSURE; c.param = doc["value"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0)  { c.type = CMD_SET_MAX;      c.param_kpa = doc["value"] | DEFAULT_MAX_KPA; }
    else if (strcmp(cmd, "hold") == 0)              { c.type = CMD_HOLD;         }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)             { c.type = CMD_DEBUG;        }
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

    ledcSetup(PUMP_INF_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP_DEF_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP_INF_PIN, PUMP_INF_CH);
    ledcAttachPin(PUMP_DEF_PIN, PUMP_DEF_CH);
    setPumps(0, 0);

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

    cachedKpa = pressure::readKpa(SENSOR_PIN);

    DBG_PRINTLN(F("{\"status\":\"reservoir_node_ready\"}"));
    DBG_PRINT("DEFAULT_MAX_KPA=%.1f  HARD_MAX_KPA=%.1f"
              "  CHECK=%lums  STATUS=%lums\n",
              DEFAULT_MAX_KPA, HARD_MAX_KPA,
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
        cachedKpa = pressure::readKpa(SENSOR_PIN);

        if (resState == RES_INFLATING &&
            (cachedKpa >= target_kpa || cachedKpa >= max_kpa)) {
            DBG_PRINT("RESERVOIR STOP inflate: %.2f >= tgt=%.2f / max=%.2f\n",
                      cachedKpa, target_kpa, max_kpa);
            stopPumps();
        }

        if (resState == RES_DEFLATING && cachedKpa <= target_kpa) {
            DBG_PRINT("RESERVOIR STOP deflate: %.2f <= tgt=%.2f\n",
                      cachedKpa, target_kpa);
            stopPumps();
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        sendStatus();
#ifdef DEBUG_BUILD
        static const char* stateNames[] = {"IDLE","INFLATING","DEFLATING"};
        DBG_PRINT("RESERVOIR  %s  %.2f kPa  tgt=%.2f  max=%.2f  pct=%d%%\n",
                  stateNames[resState], cachedKpa, target_kpa, max_kpa,
                  kpaToPct(cachedKpa, max_kpa));
        DBG_PRINT("tx ok=%lu fail=%lu drop=%lu  up=%lus\n",
                  sendOk, sendFail, cmdDropped, millis() / 1000);
#endif
    }
}

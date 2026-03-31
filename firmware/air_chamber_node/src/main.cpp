/**
 * SoftEdIBO — Air Chamber Node Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Build environments (platformio.ini):
 *   pio run              -> release (no debug overhead, no Serial output)
 *   pio run -e debug     -> debug   (Serial logs + "debug" command via ESP-NOW)
 *
 * Commands received (ESP-NOW, newline-terminated JSON stripped by gateway):
 *   {"cmd":"inflate","chamber":0,"delta":20}            <- inflate by 20% of max
 *   {"cmd":"deflate","chamber":1,"delta":20}            <- deflate by 20% of max
 *   {"cmd":"set_pressure","chamber":0,"value":75}       <- absolute target 75% of max
 *   {"cmd":"set_max_pressure","chamber":0,"value":80}   <- cap chamber 0 at 80 kPa
 *   {"cmd":"hold","chamber":2}                          <- freeze at current pressure
 *   {"cmd":"ping"}
 *   {"cmd":"debug"}                                     <- (debug build only)
 *
 * Status sent back (ESP-NOW => gateway, every STATUS_REPORT_MS):
 *   {"type":"status","chamber":0,"pressure":42}     <- pressure as % of MAX_KPA
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

#include "pins.h"
#include "pressure.h"

// ---------------------------------------------------------------------------
// Debug macros — compiled out entirely in release builds
// ---------------------------------------------------------------------------

#ifdef DEBUG_BUILD
  #define DBG_PRINT(...)   Serial.printf(__VA_ARGS__)
  #define DBG_PRINTLN(s)   Serial.println(s)
#else
  #define DBG_PRINT(...)   ((void)0)
  #define DBG_PRINTLN(s)   ((void)0)
#endif

// ---------------------------------------------------------------------------
// Tuning constants
// ---------------------------------------------------------------------------

constexpr float MAX_KPA = 8.0f;    // hard safety cap (~8 kPa burst protection)

constexpr uint8_t DEFAULT_INFLATE_DUTY = 255;

constexpr uint32_t PRESSURE_CHECK_MS =  200;
constexpr uint32_t STATUS_REPORT_MS  =  500;
constexpr uint32_t VALVE_SETTLE_MS   =   20;

constexpr int PUMP_PWM_FREQ   = 20000;
constexpr int PUMP_PWM_RES    =     8;
constexpr int PUMP1_LEDC_CH   =     0;
constexpr int PUMP2_LEDC_CH   =     1;

// ---------------------------------------------------------------------------
// Helpers: kPa  <->  percentage (0-100 of MAX_KPA)
// ---------------------------------------------------------------------------

static inline float pctToKpa(int pct) {
    return constrain(pct, 0, 100) * MAX_KPA / 100.0f;
}

static inline int kpaToPct(float kpa) {
    if (kpa <= 0.0f) return 0;
    int pct = static_cast<int>(kpa * 100.0f / MAX_KPA + 0.5f);
    return min(pct, 100);
}

// ---------------------------------------------------------------------------
// Command queue (single-producer / single-consumer ring buffer)
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
    int8_t  chamber;
    int16_t param;
};

static constexpr uint8_t QUEUE_MASK = 0x0F;   // size 16, power-of-2
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
// Per-chamber state
// ---------------------------------------------------------------------------

enum ChamberState : uint8_t {
    CH_IDLE,
    CH_PRE_INFLATE,
    CH_PRE_DEFLATE,
    CH_INFLATING,
    CH_DEFLATING
};

#ifdef DEBUG_BUILD
static const char* stateStr(ChamberState s) {
    switch (s) {
        case CH_IDLE:         return "IDLE";
        case CH_PRE_INFLATE:  return "PRE_INF";
        case CH_PRE_DEFLATE:  return "PRE_DEF";
        case CH_INFLATING:    return "INFLATING";
        case CH_DEFLATING:    return "DEFLATING";
        default:              return "?";
    }
}
#endif

struct Chamber {
    ChamberState state      = CH_IDLE;
    uint8_t      duty       = 0;
    float        target_kpa = 0.0f;
    float        max_kpa    = MAX_KPA;
    uint32_t     settle_ts  = 0;
};

static Chamber   chambers[3];
static uint8_t   gatewayMac[6]  = {};
static bool      gatewayKnown   = false;
static uint32_t  lastPressureMs = 0;
static uint32_t  lastStatusMs   = 0;
static float     cachedKpa[3]   = {};

#ifdef DEBUG_BUILD
static uint32_t  sendOk = 0, sendFail = 0;
static uint32_t  cmdDropped = 0;
#endif

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------

static void setValve(int ch, int side, bool open) {
    digitalWrite(VALVE_PINS[ch][side], open ? HIGH : LOW);
}

static void recalcPumps() {
    uint8_t maxDuty = 0;
    bool anyDeflating = false;
    for (int i = 0; i < 3; i++) {
        if (chambers[i].state == CH_INFLATING)
            maxDuty = max(maxDuty, chambers[i].duty);
        if (chambers[i].state == CH_DEFLATING)
            anyDeflating = true;
    }
    ledcWrite(PUMP1_LEDC_CH, maxDuty);
    ledcWrite(PUMP2_LEDC_CH, anyDeflating ? 255 : 0);
}

static void stopChamber(int n) {
    setValve(n, 0, false);
    setValve(n, 1, false);
    float saved_max = chambers[n].max_kpa;
    chambers[n] = Chamber{};
    chambers[n].max_kpa = saved_max;
}

static void sendStatus(int ch, float kpa) {
    if (!gatewayKnown) return;
    int pct = kpaToPct(kpa);
    char buf[48];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}",
                       ch, pct);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// ---------------------------------------------------------------------------
// Inflate / deflate with non-blocking valve settle
// ---------------------------------------------------------------------------

static void beginInflate(int n, uint8_t duty, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, chambers[n].max_kpa));

    if (chambers[n].state == CH_INFLATING && chambers[n].target_kpa == target_kpa)
        return;

    if (chambers[n].state == CH_DEFLATING || chambers[n].state == CH_PRE_DEFLATE) {
        setValve(n, 1, false);
        DBG_PRINT("CH%d settle DEF->INF (tgt=%.2f kPa)\n", n, target_kpa);
        chambers[n].state      = CH_PRE_INFLATE;
        chambers[n].duty       = duty;
        chambers[n].target_kpa = target_kpa;
        chambers[n].settle_ts  = millis();
        recalcPumps();
        return;
    }

    DBG_PRINT("CH%d INFLATE tgt=%.2f kPa (cur=%.2f)\n", n, target_kpa, cachedKpa[n]);
    chambers[n].state      = CH_INFLATING;
    chambers[n].duty       = duty;
    chambers[n].target_kpa = target_kpa;
    setValve(n, 0, true);
    recalcPumps();
}

static void beginDeflate(int n, float target_kpa) {
    target_kpa = max(0.0f, min(target_kpa, chambers[n].max_kpa));

    if (chambers[n].state == CH_DEFLATING && chambers[n].target_kpa == target_kpa)
        return;

    if (chambers[n].state == CH_INFLATING || chambers[n].state == CH_PRE_INFLATE) {
        setValve(n, 0, false);
        DBG_PRINT("CH%d settle INF->DEF (tgt=%.2f kPa)\n", n, target_kpa);
        chambers[n].state      = CH_PRE_DEFLATE;
        chambers[n].target_kpa = target_kpa;
        chambers[n].settle_ts  = millis();
        recalcPumps();
        return;
    }

    DBG_PRINT("CH%d DEFLATE tgt=%.2f kPa (cur=%.2f)\n", n, target_kpa, cachedKpa[n]);
    chambers[n].state      = CH_DEFLATING;
    chambers[n].target_kpa = target_kpa;
    setValve(n, 1, true);
    recalcPumps();
}

// ---------------------------------------------------------------------------
// Process one queued command
// ---------------------------------------------------------------------------

static void processCommand(const Cmd& c) {
    if (c.type == CMD_PING) {
        if (!gatewayKnown) return;
        static const char pong[] = "{\"type\":\"pong\"}";
        esp_now_send(gatewayMac,
                     reinterpret_cast<const uint8_t*>(pong),
                     sizeof(pong) - 1);
        DBG_PRINT("PONG\n");
        return;
    }

#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) {
        if (!gatewayKnown) return;
        char buf[200];
        int len = snprintf(buf, sizeof(buf),
            "{\"type\":\"debug\""
            ",\"ch\":[{\"s\":%d,\"kpa\":%.1f,\"tgt\":%.1f,\"max\":%.1f}"
                    ",{\"s\":%d,\"kpa\":%.1f,\"tgt\":%.1f,\"max\":%.1f}"
                    ",{\"s\":%d,\"kpa\":%.1f,\"tgt\":%.1f,\"max\":%.1f}]"
            ",\"tx_ok\":%lu,\"tx_fail\":%lu,\"drop\":%lu,\"up\":%lu}",
            chambers[0].state, cachedKpa[0], chambers[0].target_kpa, chambers[0].max_kpa,
            chambers[1].state, cachedKpa[1], chambers[1].target_kpa, chambers[1].max_kpa,
            chambers[2].state, cachedKpa[2], chambers[2].target_kpa, chambers[2].max_kpa,
            sendOk, sendFail, cmdDropped, millis() / 1000);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
        return;
    }
#endif

    int n = c.chamber;
    if (n < 0 || n > 2) {
        DBG_PRINT("WARN: invalid chamber %d, dropping\n", n);
        return;
    }

    switch (c.type) {
    case CMD_INFLATE: {
        int delta_pct  = constrain(c.param, 0, 100);
        float delta    = pctToKpa(delta_pct);
        float current  = cachedKpa[n];
        float target   = min(current + delta, chambers[n].max_kpa);
        beginInflate(n, DEFAULT_INFLATE_DUTY, target);
        break;
    }
    case CMD_DEFLATE: {
        int delta_pct  = constrain(c.param, 0, 100);
        float delta    = pctToKpa(delta_pct);
        float current  = cachedKpa[n];
        float target   = max(current - delta, 0.0f);
        beginDeflate(n, target);
        break;
    }
    case CMD_SET_PRESSURE: {
        int value_pct  = constrain(c.param, 0, 100);
        float target   = pctToKpa(value_pct);
        target         = max(0.0f, min(target, chambers[n].max_kpa));
        float current  = cachedKpa[n];
        if (current < target)
            beginInflate(n, DEFAULT_INFLATE_DUTY, target);
        else if (current > target)
            beginDeflate(n, target);
        else {
            DBG_PRINT("CH%d already at target %.2f kPa\n", n, target);
            stopChamber(n);
            recalcPumps();
        }
        break;
    }
    case CMD_SET_MAX: {
        int value_pct = constrain(c.param, 0, 100);
        float new_max = pctToKpa(value_pct);
        DBG_PRINT("CH%d set_max %.2f -> %.2f kPa\n", n, chambers[n].max_kpa, new_max);
        chambers[n].max_kpa = new_max;
        if (chambers[n].state == CH_INFLATING && cachedKpa[n] >= chambers[n].max_kpa) {
            DBG_PRINT("CH%d above new max, stopping\n", n);
            stopChamber(n);
            recalcPumps();
        }
        break;
    }
    case CMD_HOLD:
        DBG_PRINT("CH%d HOLD (was %s at %.2f kPa)\n", n, stateStr(chambers[n].state), cachedKpa[n]);
        stopChamber(n);
        recalcPumps();
        break;
    default:
        break;
    }
}

// ---------------------------------------------------------------------------
// ESP-NOW callbacks (WiFi task — keep minimal, no blocking)
// ---------------------------------------------------------------------------

#ifdef DEBUG_BUILD
static void onSent(const uint8_t*, esp_now_send_status_t status) {
    if (status == ESP_NOW_SEND_SUCCESS) sendOk++; else sendFail++;
}
#endif

static void onReceived(const uint8_t* mac_addr,
                       const uint8_t* data, int len) {
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

    if      (strcmp(cmd, "ping") == 0)             { c.type = CMD_PING;         c.chamber = -1; }
    else if (strcmp(cmd, "inflate") == 0)           { c.type = CMD_INFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "deflate") == 0)           { c.type = CMD_DEFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "set_pressure") == 0)      { c.type = CMD_SET_PRESSURE; c.chamber = doc["chamber"] | -1; c.param = doc["value"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0)  { c.type = CMD_SET_MAX;      c.chamber = doc["chamber"] | -1; c.param = doc["value"] | 100; }
    else if (strcmp(cmd, "hold") == 0)              { c.type = CMD_HOLD;         c.chamber = doc["chamber"] | -1; }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)             { c.type = CMD_DEBUG;        c.chamber = -1; }
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

    for (int i = 0; i < 3; i++) {
        pinMode(VALVE_PINS[i][0], OUTPUT); digitalWrite(VALVE_PINS[i][0], LOW);
        pinMode(VALVE_PINS[i][1], OUTPUT); digitalWrite(VALVE_PINS[i][1], LOW);
    }

    ledcSetup(PUMP1_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP2_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP1_PIN, PUMP1_LEDC_CH);
    ledcAttachPin(PUMP2_PIN, PUMP2_LEDC_CH);

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

    for (int i = 0; i < 3; i++)
        cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);

    DBG_PRINTLN(F("{\"status\":\"node_ready\"}"));
    DBG_PRINT("MAX_KPA=%.1f  CHECK=%lums  STATUS=%lums  SETTLE=%lums\n",
              MAX_KPA, PRESSURE_CHECK_MS, STATUS_REPORT_MS, VALVE_SETTLE_MS);
}

void loop() {
    uint32_t now = millis();

    // ---- Process queued commands ----
    Cmd c;
    while (queuePop(c))
        processCommand(c);

    // ---- Handle valve settle transitions (non-blocking) ----
    for (int i = 0; i < 3; i++) {
        if (chambers[i].state == CH_PRE_INFLATE &&
            now - chambers[i].settle_ts >= VALVE_SETTLE_MS) {
            chambers[i].state = CH_INFLATING;
            setValve(i, 0, true);
            recalcPumps();
            DBG_PRINT("CH%d settle done -> INFLATING\n", i);
        }
        if (chambers[i].state == CH_PRE_DEFLATE &&
            now - chambers[i].settle_ts >= VALVE_SETTLE_MS) {
            chambers[i].state = CH_DEFLATING;
            setValve(i, 1, true);
            recalcPumps();
            DBG_PRINT("CH%d settle done -> DEFLATING\n", i);
        }
    }

    // ---- Pressure safety check ----
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;

        for (int i = 0; i < 3; i++)
            cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);

        for (int i = 0; i < 3; i++) {
            float kpa = cachedKpa[i];

            if (chambers[i].state == CH_INFLATING &&
                (kpa >= chambers[i].target_kpa || kpa >= chambers[i].max_kpa)) {
                DBG_PRINT("CH%d STOP inflate: %.2f kPa >= tgt=%.2f / max=%.2f\n",
                          i, kpa, chambers[i].target_kpa, chambers[i].max_kpa);
                stopChamber(i);
                recalcPumps();
            }

            if (chambers[i].state == CH_DEFLATING && kpa <= chambers[i].target_kpa) {
                DBG_PRINT("CH%d STOP deflate: %.2f kPa <= tgt=%.2f\n",
                          i, kpa, chambers[i].target_kpa);
                stopChamber(i);
                recalcPumps();
            }
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < 3; i++)
            sendStatus(i, cachedKpa[i]);

#ifdef DEBUG_BUILD
        for (int i = 0; i < 3; i++)
            DBG_PRINT("CH%d  %s  %.2f kPa  tgt=%.2f  max=%.2f  pct=%d%%\n",
                      i, stateStr(chambers[i].state),
                      cachedKpa[i], chambers[i].target_kpa,
                      chambers[i].max_kpa, kpaToPct(cachedKpa[i]));
        DBG_PRINT("tx ok=%lu fail=%lu drop=%lu  up=%lus\n",
                  sendOk, sendFail, cmdDropped, millis() / 1000);
#endif
    }
}

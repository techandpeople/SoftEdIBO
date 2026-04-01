/**
 * SoftEdIBO — Mux Chamber Node Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Build environments (platformio.ini):
 *   pio run                  -> release      (no debug overhead, no Serial output)
 *   pio run -e debug         -> debug        (Serial logs + "debug" command via ESP-NOW)
 *   pio run -e release_res   -> release_res  (reservoir mode — no onboard pumps)
 *   pio run -e debug_res     -> debug_res    (reservoir mode + Serial logs)
 *
 * NUM_CHAMBERS: compile-time constant set via build flag (e.g. -DNUM_CHAMBERS=8).
 *   Max 8 with a single 74HC4051 sensor mux.
 *
 * RESERVOIR_MODE: valves only — a central reservoir supplies pressure.
 *   Pump pins are not initialised and recalcPumps() is a no-op.
 *
 * Valve control: 74HC595 shift-register chain.
 *   Bit layout per chamber i: bit(i*2) = inflate valve, bit(i*2+1) = deflate valve.
 *
 * Sensor mux: 74HC4051 — S0/S1/S2 select channel, SIG read via one ADC1 pin.
 *
 * Commands received (ESP-NOW, JSON):
 *   {"cmd":"inflate","chamber":N,"delta":20}
 *   {"cmd":"deflate","chamber":N,"delta":20}
 *   {"cmd":"set_pressure","chamber":N,"value":75}
 *   {"cmd":"set_max_pressure","chamber":N,"value":6.5}
 *   {"cmd":"hold","chamber":N}
 *   {"cmd":"ping"}
 *   {"cmd":"debug"}   (debug build only)
 *
 * Status sent back every STATUS_REPORT_MS:
 *   {"type":"status","chamber":N,"pressure":pct}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

#include "pins.h"
#include "pressure.h"

#ifndef NUM_CHAMBERS
#error "NUM_CHAMBERS must be defined via build flags (e.g. -DNUM_CHAMBERS=8)"
#endif

static_assert(NUM_CHAMBERS >= 1 && NUM_CHAMBERS <= 8,
              "NUM_CHAMBERS must be between 1 and 8 for single 74HC4051 sensor mux.");

// ---------------------------------------------------------------------------
// Debug macros
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

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float HARD_MAX_KPA    = 12.0f;

constexpr uint8_t DEFAULT_INFLATE_DUTY = 255;

constexpr uint32_t PRESSURE_CHECK_MS  = 200;
constexpr uint32_t STATUS_REPORT_MS   = 500;
constexpr uint32_t VALVE_SETTLE_MS    =  20;
constexpr uint32_t SMUX_SETTLE_US     =  10;   // 74HC4051 channel-select settle time

#ifndef RESERVOIR_MODE
constexpr int PUMP_PWM_FREQ  = 20000;
constexpr int PUMP_PWM_RES   =     8;
constexpr int PUMP1_LEDC_CH  =     0;
constexpr int PUMP2_LEDC_CH  =     1;
#endif

// ---------------------------------------------------------------------------
// kPa <-> percentage helpers
// ---------------------------------------------------------------------------

static inline float pctToKpaOf(int pct, float ref_kpa) {
    return constrain(pct, 0, 100) * max(0.0f, ref_kpa) / 100.0f;
}

static inline int kpaToPctOf(float kpa, float ref_kpa) {
    if (kpa <= 0.0f || ref_kpa <= 0.0f) return 0;
    int pct = static_cast<int>(kpa * 100.0f / ref_kpa + 0.5f);
    return min(pct, 100);
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
    int8_t  chamber;
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
// Per-chamber state
// ---------------------------------------------------------------------------

enum ChamberState : uint8_t {
    CH_IDLE, CH_PRE_INFLATE, CH_PRE_DEFLATE, CH_INFLATING, CH_DEFLATING
};

#ifdef DEBUG_BUILD
static const char* stateStr(ChamberState s) {
    switch (s) {
        case CH_IDLE:        return "IDLE";
        case CH_PRE_INFLATE: return "PRE_INF";
        case CH_PRE_DEFLATE: return "PRE_DEF";
        case CH_INFLATING:   return "INFLATING";
        case CH_DEFLATING:   return "DEFLATING";
        default:             return "?";
    }
}
#endif

struct Chamber {
    ChamberState state      = CH_IDLE;
    uint8_t      duty       = 0;
    float        target_kpa = 0.0f;
    float        max_kpa    = DEFAULT_MAX_KPA;
    uint32_t     settle_ts  = 0;
};

static Chamber  chambers[NUM_CHAMBERS];
static uint8_t  gatewayMac[6] = {};
static bool     gatewayKnown  = false;
static uint32_t lastPressureMs = 0;
static uint32_t lastStatusMs   = 0;
static float    cachedKpa[NUM_CHAMBERS]  = {};

#ifdef DEBUG_BUILD
static uint32_t sendOk = 0, sendFail = 0, cmdDropped = 0;
#endif

// ---------------------------------------------------------------------------
// Shift register — valve control
// Bit layout: bit(ch*2) = inflate valve, bit(ch*2+1) = deflate valve
// ---------------------------------------------------------------------------

static uint32_t srState = 0;   // up to 32 valves (16 chambers)

static void flushShiftReg() {
    constexpr int BYTES = (NUM_CHAMBERS * 2 + 7) / 8;
    digitalWrite(SR_LATCH_PIN, LOW);
    for (int b = BYTES - 1; b >= 0; b--)
        shiftOut(SR_DATA_PIN, SR_CLK_PIN, MSBFIRST, uint8_t(srState >> (b * 8)));
    digitalWrite(SR_LATCH_PIN, HIGH);
}

static void setValve(int ch, int side, bool open) {
    int bit = ch * 2 + side;
    if (open) srState |=  (1UL << bit);
    else      srState &= ~(1UL << bit);
    flushShiftReg();
}

// ---------------------------------------------------------------------------
// Sensor mux (74HC4051) — read one chamber's pressure
// ---------------------------------------------------------------------------

static float readChamberKpa(int ch) {
    digitalWrite(SMUX_S0_PIN, (ch & 1) ? HIGH : LOW);
    digitalWrite(SMUX_S1_PIN, (ch & 2) ? HIGH : LOW);
    digitalWrite(SMUX_S2_PIN, (ch & 4) ? HIGH : LOW);
    delayMicroseconds(SMUX_SETTLE_US);
    return pressure::readKpa(SMUX_SIG_PIN);
}

// ---------------------------------------------------------------------------
// Pump control
// ---------------------------------------------------------------------------

static void recalcPumps() {
#ifndef RESERVOIR_MODE
    uint8_t maxDuty    = 0;
    bool    anyDeflate = false;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (chambers[i].state == CH_INFLATING)
            maxDuty = max(maxDuty, chambers[i].duty);
        if (chambers[i].state == CH_DEFLATING)
            anyDeflate = true;
    }
    ledcWrite(PUMP1_LEDC_CH, maxDuty);
    ledcWrite(PUMP2_LEDC_CH, anyDeflate ? 255 : 0);
#endif
}

// ---------------------------------------------------------------------------
// Chamber helpers
// ---------------------------------------------------------------------------

static void stopChamber(int n) {
    setValve(n, 0, false);
    setValve(n, 1, false);
    float saved_max = chambers[n].max_kpa;
    chambers[n] = Chamber{};
    chambers[n].max_kpa = saved_max;
}

static void sendStatus(int ch, float kpa) {
    if (!gatewayKnown) return;
    int  pct = kpaToPctOf(kpa, chambers[ch].max_kpa);
    char buf[48];
    int  len = snprintf(buf, sizeof(buf),
                        "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}", ch, pct);
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
                     reinterpret_cast<const uint8_t*>(pong), sizeof(pong) - 1);
        DBG_PRINT("PONG\n");
        return;
    }

#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) {
        if (!gatewayKnown) return;
        // Build JSON dynamically for NUM_CHAMBERS chambers
        char buf[64 + NUM_CHAMBERS * 100];
        int  pos = 0;
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "{\"type\":\"debug\",\"num_chambers\":%d"
            ",\"defaults\":{\"max_kpa\":%.1f,\"hard_max_kpa\":%.1f}"
#ifdef RESERVOIR_MODE
            ",\"reservoir_mode\":true"
#else
            ",\"reservoir_mode\":false"
#endif
            ",\"ch\":[",
            NUM_CHAMBERS, DEFAULT_MAX_KPA, HARD_MAX_KPA);
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            if (i > 0) buf[pos++] = ',';
            pos += snprintf(buf + pos, sizeof(buf) - pos,
                "{\"s\":%d,\"st\":\"%s\",\"kpa\":%.3f,\"pct\":%d"
                ",\"tgt_kpa\":%.3f,\"tgt_pct\":%d,\"max_kpa\":%.3f}",
                chambers[i].state, stateStr(chambers[i].state),
                cachedKpa[i], kpaToPctOf(cachedKpa[i], chambers[i].max_kpa),
                chambers[i].target_kpa,
                kpaToPctOf(chambers[i].target_kpa, chambers[i].max_kpa),
                chambers[i].max_kpa);
        }
        pos += snprintf(buf + pos, sizeof(buf) - pos,
            "],\"tx_ok\":%lu,\"tx_fail\":%lu,\"drop\":%lu,\"up\":%lu}",
            sendOk, sendFail, cmdDropped, millis() / 1000);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), pos);
        return;
    }
#endif

    int n = c.chamber;
    if (n < 0 || n >= NUM_CHAMBERS) {
        DBG_PRINT("WARN: invalid chamber %d (max %d), dropping\n", n, NUM_CHAMBERS - 1);
        return;
    }

    switch (c.type) {
    case CMD_INFLATE: {
        int   delta_pct = constrain(c.param, 0, 100);
        float delta     = pctToKpaOf(delta_pct, chambers[n].max_kpa);
        float target    = min(cachedKpa[n] + delta, chambers[n].max_kpa);
        beginInflate(n, DEFAULT_INFLATE_DUTY, target);
        break;
    }
    case CMD_DEFLATE: {
        int   delta_pct = constrain(c.param, 0, 100);
        float delta     = pctToKpaOf(delta_pct, chambers[n].max_kpa);
        float target    = max(cachedKpa[n] - delta, 0.0f);
        beginDeflate(n, target);
        break;
    }
    case CMD_SET_PRESSURE: {
        int   value_pct = constrain(c.param, 0, 100);
        float target    = pctToKpaOf(value_pct, chambers[n].max_kpa);
        float current   = cachedKpa[n];
        if (current < target)
            beginInflate(n, DEFAULT_INFLATE_DUTY, target);
        else if (current > target)
            beginDeflate(n, target);
        else {
            stopChamber(n);
            recalcPumps();
        }
        break;
    }
    case CMD_SET_MAX: {
        float new_max = constrain(c.param_kpa, 0.1f, HARD_MAX_KPA);
        DBG_PRINT("CH%d set_max %.2f -> %.2f kPa\n", n, chambers[n].max_kpa, new_max);
        chambers[n].max_kpa = new_max;
        if (chambers[n].state == CH_INFLATING && cachedKpa[n] >= chambers[n].max_kpa) {
            stopChamber(n);
            recalcPumps();
        }
        break;
    }
    case CMD_HOLD:
        DBG_PRINT("CH%d HOLD\n", n);
        stopChamber(n);
        recalcPumps();
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

    if      (strcmp(cmd, "ping") == 0)            { c.type = CMD_PING;         c.chamber = -1; }
    else if (strcmp(cmd, "inflate") == 0)          { c.type = CMD_INFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "deflate") == 0)          { c.type = CMD_DEFLATE;      c.chamber = doc["chamber"] | -1; c.param = doc["delta"] | 10; }
    else if (strcmp(cmd, "set_pressure") == 0)     { c.type = CMD_SET_PRESSURE; c.chamber = doc["chamber"] | -1; c.param = doc["value"] | 0; }
    else if (strcmp(cmd, "set_max_pressure") == 0) { c.type = CMD_SET_MAX;      c.chamber = doc["chamber"] | -1; c.param_kpa = doc["value"] | DEFAULT_MAX_KPA; }
    else if (strcmp(cmd, "hold") == 0)             { c.type = CMD_HOLD;         c.chamber = doc["chamber"] | -1; }
#ifdef DEBUG_BUILD
    else if (strcmp(cmd, "debug") == 0)            { c.type = CMD_DEBUG;        c.chamber = -1; }
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

    // Shift register — all valves closed
    pinMode(SR_DATA_PIN,  OUTPUT);
    pinMode(SR_CLK_PIN,   OUTPUT);
    pinMode(SR_LATCH_PIN, OUTPUT);
    srState = 0;
    flushShiftReg();

    // Sensor mux select pins
    pinMode(SMUX_S0_PIN, OUTPUT);
    pinMode(SMUX_S1_PIN, OUTPUT);
    pinMode(SMUX_S2_PIN, OUTPUT);

#ifndef RESERVOIR_MODE
    ledcSetup(PUMP1_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP2_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP1_PIN, PUMP1_LEDC_CH);
    ledcAttachPin(PUMP2_PIN, PUMP2_LEDC_CH);
#endif

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

    for (int i = 0; i < NUM_CHAMBERS; i++)
        cachedKpa[i] = readChamberKpa(i);

    DBG_PRINTLN(F("{\"status\":\"mux_node_ready\"}"));
    DBG_PRINT("NUM_CHAMBERS=%d  DEFAULT_MAX_KPA=%.1f  HARD_MAX_KPA=%.1f"
              "  CHECK=%lums  STATUS=%lums  SETTLE=%lums"
#ifdef RESERVOIR_MODE
              "  RESERVOIR_MODE=1"
#endif
              "\n",
              NUM_CHAMBERS, DEFAULT_MAX_KPA, HARD_MAX_KPA,
              PRESSURE_CHECK_MS, STATUS_REPORT_MS, VALVE_SETTLE_MS);
}

void loop() {
    uint32_t now = millis();

    // ---- Process queued commands ----
    Cmd c;
    while (queuePop(c))
        processCommand(c);

    // ---- Handle valve settle transitions ----
    for (int i = 0; i < NUM_CHAMBERS; i++) {
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

        for (int i = 0; i < NUM_CHAMBERS; i++)
            cachedKpa[i] = readChamberKpa(i);

        for (int i = 0; i < NUM_CHAMBERS; i++) {
            float kpa = cachedKpa[i];

            if (chambers[i].state == CH_INFLATING &&
                (kpa >= chambers[i].target_kpa || kpa >= chambers[i].max_kpa)) {
                DBG_PRINT("CH%d STOP inflate: %.2f >= tgt=%.2f / max=%.2f\n",
                          i, kpa, chambers[i].target_kpa, chambers[i].max_kpa);
                stopChamber(i);
                recalcPumps();
            }

            if (chambers[i].state == CH_DEFLATING && kpa <= chambers[i].target_kpa) {
                DBG_PRINT("CH%d STOP deflate: %.2f <= tgt=%.2f\n",
                          i, kpa, chambers[i].target_kpa);
                stopChamber(i);
                recalcPumps();
            }
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++)
            sendStatus(i, cachedKpa[i]);

#ifdef DEBUG_BUILD
        for (int i = 0; i < NUM_CHAMBERS; i++)
            DBG_PRINT("CH%d  %s  %.2f kPa  tgt=%.2f  max=%.2f  pct=%d%%\n",
                      i, stateStr(chambers[i].state),
                      cachedKpa[i], chambers[i].target_kpa,
                      chambers[i].max_kpa, kpaToPctOf(cachedKpa[i], chambers[i].max_kpa));
        DBG_PRINT("tx ok=%lu fail=%lu drop=%lu  up=%lus\n",
                  sendOk, sendFail, cmdDropped, millis() / 1000);
#endif
    }
}

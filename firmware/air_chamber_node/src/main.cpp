/**
 * SoftEdIBO — Air Chamber Node Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Controls 3 air chambers (inflate + deflate solenoid valves per chamber)
 * driven by 2 global pumps (PWM + GND) via ESP-NOW commands from the gateway.
 *
 * Commands received (ESP-NOW, newline-terminated JSON stripped by gateway):
 *   {"cmd":"inflate","chamber":0,"delta":20}            ← inflate by 20% of max
 *   {"cmd":"deflate","chamber":1,"delta":20}            ← deflate by 20% of max
 *   {"cmd":"set_pressure","chamber":0,"value":75}       ← absolute target 75% of max
 *   {"cmd":"set_max_pressure","chamber":0,"value":80}   ← cap chamber 0 at 80% of hardware max
 *   {"cmd":"hold","chamber":2}                          ← freeze chamber at current pressure
 *   {"cmd":"ping"}
 *
 * Status sent back (ESP-NOW => gateway, every STATUS_REPORT_MS):
 *   {"type":"status","chamber":0,"pressure":75}     ← pressure as % of MAX_PRESSURE_ADC
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------------------
// Hardware pinout — adjust to your PCB
// ---------------------------------------------------------------------------

// [chamber][0] = inflate valve GPIO, [chamber][1] = deflate valve GPIO
// HIGH = valve open, LOW = valve closed
constexpr int VALVE_PINS[3][2] = {
    {22, 23},   // chamber 0
    {21, 13},   // chamber 1
    {14, 33},   // chamber 2
};

constexpr int PUMP1_PIN = 25;   // inflate pump  (PWM, other wire to GND)
constexpr int PUMP2_PIN = 26;   // deflate pump  (PWM, other wire to GND)

constexpr int PSENSOR_PINS[3] = {34, 35, 32};  // XGZP6847 analog (12-bit ADC)

// ---------------------------------------------------------------------------
// Tuning constants — edit these before flashing
// ---------------------------------------------------------------------------

// Pressure limits (raw 12-bit ADC, 0-4095)
// Adjust after measuring sensor output at known pressures.
constexpr int MAX_PRESSURE_ADC     =  500;  // ADC hard cap — ~8 kPa burst protection (100 kPa sensor)
constexpr int MIN_PRESSURE_ADC     =  200;  // near-empty threshold

constexpr uint8_t DEFAULT_INFLATE_DUTY = 255;   // pump PWM duty when 'value' omitted

// Timing
constexpr uint32_t PRESSURE_CHECK_MS =  200;   // pressure safety check interval
constexpr uint32_t STATUS_REPORT_MS  =  500;   // ESP-NOW status broadcast interval
constexpr uint32_t VALVE_SETTLE_MS   =   20;   // pause after toggling a valve

// ADC
constexpr int ADC_SAMPLES = 4;   // number of samples to average per pressure read

// Pump PWM
constexpr int PUMP_PWM_FREQ    = 20000;   // Hz — above audible range
constexpr int PUMP_PWM_RES     =     8;   // bits (0-255)
constexpr int PUMP1_LEDC_CH    =     0;   // LEDC channel for inflate pump
constexpr int PUMP2_LEDC_CH    =     1;   // LEDC channel for deflate pump

// ---------------------------------------------------------------------------
// Per-chamber state
// ---------------------------------------------------------------------------

struct Chamber {
    bool    inflating        = false;
    bool    deflating        = false;
    uint8_t duty             = 0;
    int     target_pressure  = MAX_PRESSURE_ADC;
    int     max_pressure_adc = MAX_PRESSURE_ADC;  // per-chamber software limit (set by app)
};

static Chamber   chambers[3];
static uint8_t   gatewayMac[6]    = {};
static bool      gatewayKnown     = false;
static uint32_t  lastPressureMs   = 0;
static uint32_t  lastStatusMs     = 0;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Multi-sample ADC read — reduces noise on pressure sensor output
static int readPressure(int pin) {
    int sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) sum += analogRead(pin);
    return sum / ADC_SAMPLES;
}

static void setValve(int chamber, int side, bool open) {
    digitalWrite(VALVE_PINS[chamber][side], open ? HIGH : LOW);
}

static void recalcPump1() {
    uint8_t maxDuty = 0;
    for (const auto& ch : chambers)
        if (ch.inflating) maxDuty = max(maxDuty, ch.duty);
    ledcWrite(PUMP1_LEDC_CH, maxDuty);
}

static void recalcPump2() {
    bool anyDeflating = false;
    for (const auto& ch : chambers)
        if (ch.deflating) { anyDeflating = true; break; }
    ledcWrite(PUMP2_LEDC_CH, anyDeflating ? 255 : 0);
}

static void stopChamber(int n) {
    setValve(n, 0, false);   // close inflate valve
    setValve(n, 1, false);   // close deflate valve
    chambers[n] = Chamber{};
}

static void sendStatus(int chamber, int pressure_adc) {
    if (!gatewayKnown) return;
    int pct = pressure_adc * 100 / MAX_PRESSURE_ADC;
    char buf[48];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}",
                       chamber, pct);
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
}

// ---------------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------------

static void cmdInflate(int n, uint8_t duty, int target) {
    // Close deflate side first
    if (chambers[n].deflating) {
        setValve(n, 1, false);
        chambers[n].deflating = false;
        recalcPump2();
        delay(VALVE_SETTLE_MS);
    }
    chambers[n].inflating       = true;
    chambers[n].duty            = duty;
    chambers[n].target_pressure = target;
    setValve(n, 0, true);    // open inflate valve
    recalcPump1();
}

static void cmdDeflate(int n, int target) {
    // Close inflate side first
    if (chambers[n].inflating) {
        setValve(n, 0, false);
        chambers[n].inflating = false;
        recalcPump1();
        delay(VALVE_SETTLE_MS);
    }
    chambers[n].deflating       = true;
    chambers[n].target_pressure = target;
    setValve(n, 1, true);    // open deflate valve
    recalcPump2();
}

static void cmdStop(int n) {
    stopChamber(n);
    recalcPump1();
    recalcPump2();
}

// ---------------------------------------------------------------------------
// ESP-NOW callbacks
// ---------------------------------------------------------------------------

static void onSent(const uint8_t* /*mac*/, esp_now_send_status_t /*s*/) {}

static void onReceived(const uint8_t* mac_addr,
                       const uint8_t* data, int len) {
    // Learn gateway MAC on first contact
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

    if (strcmp(cmd, "ping") == 0) {
        // Fixed response — no heap allocation
        static const char pong[] = "{\"type\":\"pong\"}";
        esp_now_send(gatewayMac,
                     reinterpret_cast<const uint8_t*>(pong),
                     sizeof(pong) - 1);
        return;
    }

    if (strcmp(cmd, "inflate") == 0) {
        int n = doc["chamber"] | -1;
        if (n < 0 || n > 2) return;
        int delta_pct = doc["delta"] | 10;
        int current   = readPressure(PSENSOR_PINS[n]);
        int delta_adc = delta_pct * MAX_PRESSURE_ADC / 100;
        int target    = min(current + delta_adc, chambers[n].max_pressure_adc);
        cmdInflate(n, DEFAULT_INFLATE_DUTY, target);
        return;
    }

    if (strcmp(cmd, "deflate") == 0) {
        int n = doc["chamber"] | -1;
        if (n < 0 || n > 2) return;
        int delta_pct = doc["delta"] | 10;
        int current   = readPressure(PSENSOR_PINS[n]);
        int delta_adc = delta_pct * MAX_PRESSURE_ADC / 100;
        int target    = max(current - delta_adc, 0);
        cmdDeflate(n, target);
        return;
    }

    if (strcmp(cmd, "set_pressure") == 0) {
        int n = doc["chamber"] | -1;
        if (n < 0 || n > 2) return;
        int value_pct = doc["value"] | 0;
        int target    = value_pct * MAX_PRESSURE_ADC / 100;
        target        = max(0, min(target, chambers[n].max_pressure_adc));
        int current   = readPressure(PSENSOR_PINS[n]);
        if (current < target)
            cmdInflate(n, DEFAULT_INFLATE_DUTY, target);
        else if (current > target)
            cmdDeflate(n, target);
        return;
    }

    if (strcmp(cmd, "set_max_pressure") == 0) {
        int n = doc["chamber"] | -1;
        if (n < 0 || n > 2) return;
        int value_pct = doc["value"] | 100;
        chambers[n].max_pressure_adc = max(0, min(value_pct * MAX_PRESSURE_ADC / 100, MAX_PRESSURE_ADC));
        return;
    }

    if (strcmp(cmd, "hold") == 0) {
        int n = doc["chamber"] | -1;
        if (n < 0 || n > 2) return;
        cmdStop(n);
        recalcPump1();
        recalcPump2();
        return;
    }
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);

    // Valve outputs
    for (int i = 0; i < 3; i++) {
        pinMode(VALVE_PINS[i][0], OUTPUT); digitalWrite(VALVE_PINS[i][0], LOW);
        pinMode(VALVE_PINS[i][1], OUTPUT); digitalWrite(VALVE_PINS[i][1], LOW);
    }

    // Pump PWM via LEDC (channel-based API, compatible with all ESP32 core versions)
    ledcSetup(PUMP1_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP2_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP1_PIN, PUMP1_LEDC_CH);
    ledcAttachPin(PUMP2_PIN, PUMP2_LEDC_CH);

    // Pressure sensor ADC pins are input-only by default on ESP32 (34,35,36)

    // ESP-NOW
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    if (esp_now_init() != ESP_OK) {
        Serial.println(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }
    esp_now_register_send_cb(onSent);
    esp_now_register_recv_cb(onReceived);

    Serial.println(F("{\"status\":\"node_ready\"}"));
}

void loop() {
    uint32_t now = millis();

    // ---- Pressure safety check (only when at least one chamber is active) ----
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        bool anyActive = false;
        for (int i = 0; i < 3; i++)
            if (chambers[i].inflating || chambers[i].deflating) { anyActive = true; break; }

        if (anyActive) {
            for (int i = 0; i < 3; i++) {
                int adc = readPressure(PSENSOR_PINS[i]);
                // Safety: stop inflate if at target OR exceeds per-chamber max
                if (chambers[i].inflating &&
                    (adc >= chambers[i].target_pressure || adc >= chambers[i].max_pressure_adc)) {
                    setValve(i, 0, false);
                    chambers[i].inflating = false;
                    recalcPump1();
                }
                if (chambers[i].deflating && adc <= chambers[i].target_pressure) {
                    setValve(i, 1, false);
                    chambers[i].deflating = false;
                    recalcPump2();
                }
            }
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < 3; i++)
            sendStatus(i, readPressure(PSENSOR_PINS[i]));
    }
}

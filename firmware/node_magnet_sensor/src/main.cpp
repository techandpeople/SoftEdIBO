/**
 * SoftEdIBO — node_magnet_sensor firmware
 *
 * 4x MLX90393 magnetometers (+1 optional 5th on a second I2C bus) acting as a
 * touch-sensing board for a soft skin. A small magnet sits above each sensor in
 * the silicone; pressing the skin moves the magnet and changes the field.
 *
 * Emits the node_magnet_sensor protocol over ESP-NOW via the shared se_espnow.h, so it
 * drops straight into the SoftEdIBO PC (QuadrantDetector / touch tracking):
 *   boot:   {"status":"node_magnet_sensor_ready","sensors":N,"variant":"mlx90393"}
 *   stream: {"type":"magnet","mag":[mT..],"adj":[0..1..],"act":[idx..]}
 *
 * Sensor order matters: S0..S3 map to quadrants Q1(TL) Q2(TR) Q3(BL) Q4(BR),
 * which is the order of the I2C addresses below. The PC's QuadrantDetector
 * consumes the first 4 sensors; the optional 5th is appended after them.
 *
 * Each sensor auto-zeros (baseline) at boot. Re-zero at runtime by sending
 * {"cmd":"rebaseline"} (e.g. after the silicone settles). Normalisation scale
 * and activation level are tunable with {"cmd":"configure",...}.
 *
 * Adapted from the thesis MLX90393 live-stream firmware. The offline
 * calibration protocol (CSV) was dropped: the SoftEdIBO runtime detects touch
 * with thresholds on the normalised values, not a calibrated model.
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MLX90393.h>
#include <ArduinoJson.h>
#include <math.h>
#include <esp_ota_ops.h>

#include "se_espnow.h"
#include "se_ota.h"

namespace {

// ---------------------------------------------------------------------------
// Hardware config (from the thesis board)
// ---------------------------------------------------------------------------

constexpr uint8_t I2C_SDA   = 21, I2C_SCL   = 22;   // primary bus (S0..S3)
constexpr uint8_t EXTRA_SDA = 16, EXTRA_SCL = 17;   // secondary bus (optional S4)

constexpr size_t  NUM_PRIMARY = 4;
constexpr uint8_t PRIMARY_ADDR[NUM_PRIMARY] = {0x18, 0x19, 0x1A, 0x1B};
constexpr uint8_t EXTRA_ADDR  = 0x1A;
constexpr size_t  MAX_SENSORS = NUM_PRIMARY + 1;    // +1 optional extra

constexpr mlx90393_gain_t         GAIN   = MLX90393_GAIN_2X;
constexpr mlx90393_oversampling_t OSR    = MLX90393_OSR_2;
constexpr mlx90393_filter_t       FILTER = MLX90393_FILTER_3;

constexpr uint16_t BASELINE_SAMPLES   = 70;   // running-average samples to auto-zero
constexpr uint32_t STREAM_INTERVAL_MS = 35;   // ~28 Hz

// ---------------------------------------------------------------------------
// Tunables (overridable at runtime via "configure")
// ---------------------------------------------------------------------------

float fullscaleMt  = 1000.0f;  // |delta| mapped to adj = 1.0
float actThreshold = 0.3f;    // adj level at/above which a sensor is "active"
#ifdef DEBUG_BUILD
bool  debugMode    = true;
#else
bool  debugMode    = false;
#endif

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

struct Vec3 { float x, y, z; };

Adafruit_MLX90393 mlx[MAX_SENSORS];
TwoWire           extraWire = TwoWire(1);
bool              ready[MAX_SENSORS] = {};
size_t            streamCount = NUM_PRIMARY;   // 4, or 5 if the extra is present

Vec3     baseline[MAX_SENSORS] = {};
bool     baselineReady = false;
uint16_t baselineN     = 0;
uint32_t lastStreamMs    = 0;
uint32_t lastAnnounceMs  = 0;
char     announceMsg[80] = {};

constexpr uint32_t ANNOUNCE_INTERVAL_MS = 2000;

inline float vmag(const Vec3& v) { return sqrtf(v.x * v.x + v.y * v.y + v.z * v.z); }

bool readSensor(size_t i, Vec3& out) {
    if (!ready[i]) return false;
    return mlx[i].readData(&out.x, &out.y, &out.z);
}

void initSensor(size_t i, uint8_t addr, TwoWire* bus) {
    ready[i] = mlx[i].begin_I2C(addr, bus);
    if (ready[i]) {
        mlx[i].setGain(GAIN);
        mlx[i].setOversampling(OSR);
        mlx[i].setFilter(FILTER);
    }
}

void resetBaseline() {
    baselineReady = false;
    baselineN     = 0;
    for (auto& b : baseline) b = {0.0f, 0.0f, 0.0f};
}

// ---------------------------------------------------------------------------
// ESP-NOW command handling
// ---------------------------------------------------------------------------

void onReceived(const uint8_t* mac, const uint8_t* data, int len) {
    se::node::learnGateway(mac);   // remember gateway + add peer on first msg
    if (se::ota::tryHandle(data, len)) return;   // firmware update over ESP-NOW

    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return;

    const char* cmd = doc["cmd"] | "";
    if (strcmp(cmd, "ping") == 0) {
        se::node::toGateway("{\"type\":\"pong\"}");
    } else if (strcmp(cmd, "rebaseline") == 0) {
        resetBaseline();
    } else if (strcmp(cmd, "configure") == 0) {
        if (!doc["fullscale_mt"].isNull())  fullscaleMt  = doc["fullscale_mt"].as<float>();
        if (!doc["act_threshold"].isNull()) actThreshold = doc["act_threshold"].as<float>();
    }
}

// ---------------------------------------------------------------------------
// Streaming
// ---------------------------------------------------------------------------

// Accumulate a running-average baseline over the first BASELINE_SAMPLES reads.
void accumulateBaseline(const Vec3* samples, const bool* valid) {
    baselineN++;
    const float a = 1.0f / static_cast<float>(baselineN);
    for (size_t i = 0; i < streamCount; ++i) {
        if (!valid[i]) continue;
        if (baselineN == 1) {
            baseline[i] = samples[i];
        } else {
            baseline[i].x = (1.0f - a) * baseline[i].x + a * samples[i].x;
            baseline[i].y = (1.0f - a) * baseline[i].y + a * samples[i].y;
            baseline[i].z = (1.0f - a) * baseline[i].z + a * samples[i].z;
        }
    }
    if (baselineN >= BASELINE_SAMPLES) baselineReady = true;
}

// Build {"type":"magnet","mag":[..],"adj":[..],"act":[..]} into buf.
void buildImuMessage(const Vec3* samples, const bool* valid, char* buf, size_t cap) {
    int pos = snprintf(buf, cap, "{\"type\":\"magnet\",\"mag\":[");
    for (size_t i = 0; i < streamCount; ++i) {
        float m = valid[i] ? vmag({samples[i].x - baseline[i].x,
                                    samples[i].y - baseline[i].y,
                                    samples[i].z - baseline[i].z}) : 0.0f;
        pos += snprintf(buf + pos, cap - pos, "%s%.3f", i ? "," : "", m);
    }
    pos += snprintf(buf + pos, cap - pos, "],\"adj\":[");
    bool active[MAX_SENSORS] = {};
    for (size_t i = 0; i < streamCount; ++i) {
        float m = valid[i] ? vmag({samples[i].x - baseline[i].x,
                                    samples[i].y - baseline[i].y,
                                    samples[i].z - baseline[i].z}) : 0.0f;
        float adj = fullscaleMt > 0.0f ? m / fullscaleMt : 0.0f;
        if (adj > 1.0f) adj = 1.0f;
        active[i] = adj >= actThreshold;
        pos += snprintf(buf + pos, cap - pos, "%s%.3f", i ? "," : "", adj);
    }
    pos += snprintf(buf + pos, cap - pos, "],\"act\":[");
    bool first = true;
    for (size_t i = 0; i < streamCount; ++i) {
        if (!active[i]) continue;
        pos += snprintf(buf + pos, cap - pos, "%s%u", first ? "" : ",", (unsigned)i);
        first = false;
    }
    snprintf(buf + pos, cap - pos, "]}");
}

}  // namespace

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.begin(115200);

    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(400000);
    extraWire.begin(EXTRA_SDA, EXTRA_SCL);
    extraWire.setClock(400000);
    delay(800);

    // TODO(scale): supporting ~12 sensors needs more I2C than the MLX90393's
    // 4 addresses/bus allow — add a TCA9548A I2C mux and a {channel,address}
    // sensor table. See README.md "Planned / TODO" before adding.

    for (size_t i = 0; i < NUM_PRIMARY; ++i) initSensor(i, PRIMARY_ADDR[i], &Wire);
    initSensor(NUM_PRIMARY, EXTRA_ADDR, &extraWire);

    // The PC's QuadrantDetector consumes the first 4 sensors; include the 5th
    // only when it actually responded so it lands after the quadrant sensors.
    streamCount = ready[NUM_PRIMARY] ? MAX_SENSORS : NUM_PRIMARY;

    if (!se::begin(onReceived)) {
        Serial.println(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }

    snprintf(announceMsg, sizeof(announceMsg),
             "{\"status\":\"node_magnet_sensor_ready\",\"sensors\":%u,\"variant\":\"mlx90393\"}",
             (unsigned)streamCount);
    se::broadcast(announceMsg);
    Serial.println(announceMsg);
}

void loop() {
    uint32_t now = millis();

    if (!se::node::gatewayKnown && now - lastAnnounceMs >= ANNOUNCE_INTERVAL_MS) {
        lastAnnounceMs = now;
        se::broadcast(announceMsg);
    }

    if (now - lastStreamMs < STREAM_INTERVAL_MS) return;
    lastStreamMs = now;

    Vec3 samples[MAX_SENSORS];
    bool valid[MAX_SENSORS];
    bool any = false;
    for (size_t i = 0; i < streamCount; ++i) {
        valid[i] = readSensor(i, samples[i]) &&
                   !isnan(samples[i].x) && !isnan(samples[i].y) && !isnan(samples[i].z);
        any |= valid[i];
    }
    if (!any) return;

    if (!baselineReady) {
        accumulateBaseline(samples, valid);
        return;
    }

    char msg[256];
    buildImuMessage(samples, valid, msg, sizeof(msg));
    se::node::toGateway(msg);
    if (debugMode) Serial.println(msg);
}

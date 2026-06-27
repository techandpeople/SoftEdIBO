#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MLX90393.h>
#include <ArduinoJson.h>
#include <math.h>

#include "se_espnow.h"
#include "se_ota.h"
#include "pins.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// Optional MLX90393 magnet/touch sensing, folded into node_direct.
//
// 4x MLX90393 magnetometers on the shared I2C bus (MAGNET_SDA/MAGNET_SCL) act
// as a touch board: a magnet above each sensor in the silicone moves when the
// skin is pressed, changing the field. This is the same protocol the
// standalone node_magnet_sensor firmware speaks, so the SoftEdIBO PC
// (QuadrantDetector / touch tracking) consumes it unchanged:
//   boot:   {"status":"node_magnet_sensor_ready","sensors":N,"variant":"mlx90393"}
//   stream: {"type":"magnet","mag":[mT..],"adj":[0..1..],"act":[idx..]}
//
// The module is auto-detecting and self-disabling: if no MLX90393 answers on
// the bus at boot (no sensors wired), `present` stays false and the board runs
// exactly as a plain node_direct — no announce, no stream, no I2C traffic.
//
// Each sensor auto-zeros (baseline) over the first BASELINE_SAMPLES reads.
// Re-zero at runtime with {"cmd":"rebaseline"}; tune normalisation/activation
// (and opt into adaptive baseline) with {"cmd":"configure",...}. Both are
// dispatched from commands.h, matching what the PC's touch tuning panel sends.
// ---------------------------------------------------------------------------

namespace magnet {

constexpr size_t  NUM_SENSORS = 4;                        // S0..S3 → Q1..Q4
constexpr uint8_t ADDR[NUM_SENSORS] = {0x18, 0x19, 0x1A, 0x1B};

constexpr mlx90393_gain_t         GAIN   = MLX90393_GAIN_2X;
constexpr mlx90393_oversampling_t OSR    = MLX90393_OSR_2;
constexpr mlx90393_filter_t       FILTER = MLX90393_FILTER_3;

constexpr uint16_t BASELINE_SAMPLES     = 70;     // running-average reads to auto-zero
constexpr uint32_t STREAM_INTERVAL_MS   = 35;     // ~28 Hz
constexpr uint32_t ANNOUNCE_INTERVAL_MS = 2000;   // re-announce until gateway known

// Tunables (overridable at runtime via "configure")
inline float fullscaleMt      = 1000.0f;  // |delta| mapped to adj = 1.0
inline float actThreshold     = 0.3f;     // adj at/above which a sensor is "active"
inline bool  adaptiveBaseline = false;    // continuously track slow drift (opt-in)
inline float baselineTauMs    = 2000.0f;  // adaptive-baseline time constant (ms)

struct Vec3 { float x, y, z; };

inline Adafruit_MLX90393 mlx[NUM_SENSORS];
inline bool     ready[NUM_SENSORS] = {};
inline bool     present       = false;   // any sensor detected → module active
inline Vec3     baseline[NUM_SENSORS] = {};
inline bool     baselineReady = false;
inline uint16_t baselineN     = 0;
inline uint32_t lastStreamMs  = 0;
inline uint32_t lastAnnounceMs = 0;
inline char     announceMsg[80] = {};

inline float vmag(const Vec3& v) { return sqrtf(v.x * v.x + v.y * v.y + v.z * v.z); }

inline bool readSensor(size_t i, Vec3& out) {
    if (!ready[i]) return false;
    return mlx[i].readData(&out.x, &out.y, &out.z);
}

inline void resetBaseline() {
    baselineReady = false;
    baselineN     = 0;
    for (auto& b : baseline) b = {0.0f, 0.0f, 0.0f};
}

// Apply runtime tunables from a "configure" command (all fields optional).
inline void applyConfigure(const JsonDocument& doc) {
    if (!doc["fullscale_mt"].isNull())      fullscaleMt      = doc["fullscale_mt"].as<float>();
    if (!doc["act_threshold"].isNull())     actThreshold     = doc["act_threshold"].as<float>();
    if (!doc["adaptive_baseline"].isNull()) adaptiveBaseline = doc["adaptive_baseline"].as<bool>();
    if (!doc["baseline_tau_ms"].isNull())   baselineTauMs    = doc["baseline_tau_ms"].as<float>();
}

// Probe the bus; only enable the module if at least one MLX90393 responds.
inline void hardware_init() {
    Wire.begin(MAGNET_SDA, MAGNET_SCL);
    Wire.setClock(400000);
    delay(800);   // let the sensors power up before the first transaction

    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        ready[i] = mlx[i].begin_I2C(ADDR[i], &Wire);
        if (ready[i]) {
            mlx[i].setGain(GAIN);
            mlx[i].setOversampling(OSR);
            mlx[i].setFilter(FILTER);
            present = true;
        }
        DBG_PRINT("magnet: S%u @0x%02X %s\n",
                  (unsigned)i, ADDR[i], ready[i] ? "ok" : "MISSING");
    }
    if (!present) {
        DBG_PRINTLN("magnet: no MLX90393 found — module disabled");
        return;
    }

    // The PC's QuadrantDetector consumes 4 sensors; always advertise NUM_SENSORS
    // so quadrant mapping stays stable even if one sensor is briefly unwired.
    // NB: only BUILD the message here — it is broadcast by announce(), not now.
    // hardware_init() runs before se::begin() in setup(), so broadcasting at this
    // point hits an uninitialised radio: the send is lost and can crash the stack,
    // which left the whole node invisible ("no nodes found") whenever sensors were
    // wired (present == true) but worked fine when they were not.
    snprintf(announceMsg, sizeof(announceMsg),
             "{\"status\":\"node_magnet_sensor_ready\",\"sensors\":%u,\"variant\":\"mlx90393\"}",
             (unsigned)NUM_SENSORS);
}

// Broadcast the magnet board's ready message. Must be called from setup() AFTER
// se::begin() has brought ESP-NOW up. No-op when no sensors are present.
inline void announce() {
    if (!present) return;
    se::broadcast(announceMsg);
    LOG("%s\n", announceMsg);
}

// Accumulate a running-average baseline over the first BASELINE_SAMPLES reads.
inline void accumulateBaseline(const Vec3* samples, const bool* valid) {
    baselineN++;
    const float a = 1.0f / static_cast<float>(baselineN);
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
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

// Nudge the baseline toward the current reading for sensors that are NOT being
// touched, so slow drift (silicone settling, a chamber inflating and pushing
// the magnet) is absorbed while fast touches still stand out. Opt-in via
// {"cmd":"configure","adaptive_baseline":true}. EWMA step is dt/tau.
inline void updateAdaptiveBaseline(const Vec3* samples, const bool* valid, float dtMs) {
    if (!adaptiveBaseline || baselineTauMs <= 0.0f) return;
    float a = dtMs / baselineTauMs;
    if (a > 1.0f) a = 1.0f;
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        if (!valid[i]) continue;
        float m = vmag({samples[i].x - baseline[i].x,
                        samples[i].y - baseline[i].y,
                        samples[i].z - baseline[i].z});
        float adj = fullscaleMt > 0.0f ? m / fullscaleMt : 0.0f;
        if (adj >= actThreshold) continue;   // touch in progress — freeze this sensor
        baseline[i].x += a * (samples[i].x - baseline[i].x);
        baseline[i].y += a * (samples[i].y - baseline[i].y);
        baseline[i].z += a * (samples[i].z - baseline[i].z);
    }
}

// Build {"type":"magnet","mag":[..],"adj":[..],"act":[..]} into buf.
inline void buildMessage(const Vec3* samples, const bool* valid, char* buf, size_t cap) {
    int pos = snprintf(buf, cap, "{\"type\":\"magnet\",\"mag\":[");
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        float m = valid[i] ? vmag({samples[i].x - baseline[i].x,
                                    samples[i].y - baseline[i].y,
                                    samples[i].z - baseline[i].z}) : 0.0f;
        pos += snprintf(buf + pos, cap - pos, "%s%.3f", i ? "," : "", m);
    }
    pos += snprintf(buf + pos, cap - pos, "],\"adj\":[");
    bool active[NUM_SENSORS] = {};
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
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
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        if (!active[i]) continue;
        pos += snprintf(buf + pos, cap - pos, "%s%u", first ? "" : ",", (unsigned)i);
        first = false;
    }
    snprintf(buf + pos, cap - pos, "]}");
}

inline void tick(uint32_t now) {
    if (!present) return;

    // Stay off the air while a firmware update streams in: ~28 Hz broadcasts plus
    // blocking I2C reads starve the ota_ack replies and fail the transfer.
    if (se::ota::active) return;

    // Re-announce until the gateway is known so a late-connecting PC still
    // captures the magnet geometry.
    if (!se::node::gatewayKnown && now - lastAnnounceMs >= ANNOUNCE_INTERVAL_MS) {
        lastAnnounceMs = now;
        se::broadcast(announceMsg);
    }

    if (now - lastStreamMs < STREAM_INTERVAL_MS) return;
    const float dtMs = (float)(now - lastStreamMs);
    lastStreamMs = now;

    Vec3 samples[NUM_SENSORS];
    bool valid[NUM_SENSORS];
    bool any = false;
    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        valid[i] = readSensor(i, samples[i]) &&
                   !isnan(samples[i].x) && !isnan(samples[i].y) && !isnan(samples[i].z);
        any |= valid[i];
    }
    if (!any) return;

    if (!baselineReady) {
        accumulateBaseline(samples, valid);
        return;
    }

    updateAdaptiveBaseline(samples, valid, dtMs);

    char msg[256];
    buildMessage(samples, valid, msg, sizeof(msg));
    se::node::toGateway(msg);
    DBG_PRINTLN(msg);   // echo the magnet stream over Serial in debug builds
}

}  // namespace magnet

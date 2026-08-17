#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MLX90393.h>
#include <ArduinoJson.h>
#include <math.h>

#include "se_espnow.h"
#include "se_ota.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// se_magnet.h - shared MLX90393 magnet/touch sensing module.
//
// One implementation of the node_magnet_sensor protocol, used by both boards
// that carry touch sensors: the standalone node_magnet_sensor firmware and the
// direct actuator board (node_actuator [env:direct*], which folds the module
// in). A magnet above each sensor in the silicone moves when the skin is
// pressed, changing the field. The SoftEdIBO PC (QuadrantDetector / touch
// tracking) consumes the stream unchanged:
//   boot:   {"status":"node_magnet_sensor_ready","sensors":N,"variant":"mlx90393"}
//   stream: {"type":"magnet","mag":[uT..],"act":[idx..]}
//
// Callers wire it up with addSensor() per MLX90393 (any bus), then
// finishInit(streamIntervalMs) - which only BUILDS the announce message; it is
// broadcast by announce()/tick() AFTER se::begin(), because a broadcast on an
// uninitialised radio is lost and can crash the stack (this once left a whole
// node invisible whenever sensors were wired). If no sensor answers,
// `present` stays false and the module self-disables (unless the caller forces
// it, as the standalone board does to stay visible for debugging).
//
// Each sensor auto-zeros (baseline) over the first BASELINE_SAMPLES reads.
// Re-zero at runtime with {"cmd":"rebaseline"}; tune the activation threshold
// in uT (and opt into adaptive baseline) with {"cmd":"configure",...}.
//
// 3-axis streaming: when `streamVec` is on, each stream message also carries
// "vec":[[dx,dy,dz],...] - the per-sensor baseline-subtracted field delta in
// whole uT (direction information the magnitude collapses away; enables
// vector touch compensation, see docs/TOUCH_COUPLING.md) - and the announce
// gains "vec":1. Off by default; the -DMAG_VECTOR build flag turns it on at
// boot, and {"cmd":"configure","stream_vec":true|false} toggles it at runtime
// (so any build can stream vectors during a calibration sweep, no reflash).
// ---------------------------------------------------------------------------

namespace se {
namespace magnet {

constexpr size_t   MAX_SENSORS       = 5;
constexpr uint16_t BASELINE_SAMPLES  = 70;     // running-average reads to auto-zero
constexpr uint32_t ANNOUNCE_INTERVAL_MS = 2000;  // re-announce until gateway known

constexpr mlx90393_gain_t         GAIN   = MLX90393_GAIN_2X;
constexpr mlx90393_oversampling_t OSR    = MLX90393_OSR_2;
constexpr mlx90393_filter_t       FILTER = MLX90393_FILTER_3;

// Tunables (overridable at runtime via "configure")
inline float actThresholdUt   = 300.0f;   // |field delta| uT at/above which a sensor is "active"
inline bool  adaptiveBaseline = false;    // continuously track slow drift (opt-in)
inline float baselineTauMs    = 2000.0f;  // adaptive-baseline time constant (ms)
#ifdef MAG_VECTOR
inline bool  streamVec        = true;     // 3-axis "vec" rows in every stream message
#else
inline bool  streamVec        = false;
#endif

struct Vec3 { float x, y, z; };

inline Adafruit_MLX90393 mlx[MAX_SENSORS];
inline bool     ready[MAX_SENSORS] = {};
inline size_t   count         = 0;        // slots wired via addSensor()
inline bool     present       = false;    // any sensor detected -> module active
inline uint32_t streamIntervalMs = 35;    // set by finishInit()

inline Vec3     baseline[MAX_SENSORS] = {};
inline bool     baselineReady = false;
inline uint16_t baselineN     = 0;
inline uint32_t lastStreamMs   = 0;
inline uint32_t lastAnnounceMs = 0;
// Sensor retune asked for from the RX callback; applied by tick() on the main
// task (I2C is not safe to touch from the callback). -1 = nothing pending.
inline volatile int pendingOsr    = -1;
inline volatile int pendingFilter = -1;
inline char     announceMsg[128] = {};

inline float vmag(const Vec3& v) { return sqrtf(v.x * v.x + v.y * v.y + v.z * v.z); }

// Probe one MLX90393 and reserve its slot (S0.. in call order - order matters:
// the PC's QuadrantDetector maps the first 4 to quadrants). A missing sensor
// still holds its slot so quadrant indices stay stable - unless `optional`
// (the standalone board's extra 5th sensor), which is only appended when it
// actually responds.
inline bool addSensor(uint8_t addr, TwoWire* bus, bool optional = false) {
    if (count >= MAX_SENSORS) return false;
    const size_t i = count;
    bool ok = mlx[i].begin_I2C(addr, bus);
    if (ok) {
        mlx[i].setGain(GAIN);
        mlx[i].setOversampling(OSR);
        mlx[i].setFilter(FILTER);
        present = true;
    } else if (optional) {
        return false;                       // don't reserve a slot for it
    }
    ready[i] = ok;
    ++count;
    DBG_PRINT("magnet: S%u @0x%02X %s\n", (unsigned)i, addr, ok ? "ok" : "MISSING");
    return ok;
}

inline void buildAnnounce() {
    // "fw" is the flash marker (docs rule: confirm a build actually landed
    // before judging it) - bump it whenever this module's behaviour changes.
    snprintf(announceMsg, sizeof(announceMsg),
             "{\"status\":\"node_magnet_sensor_ready\",\"sensors\":%u%s,"
             "\"variant\":\"mlx90393\",\"fw\":\"magvec-1\"}",
             (unsigned)count, streamVec ? ",\"vec\":1" : "");
}

// Finish wiring: stream cadence + the announce message (built now, broadcast
// later - see the header comment). `forcePresent` keeps a board with dead
// sensors announcing (standalone magnet board: visible in scans for debugging).
inline void finishInit(uint32_t intervalMs, bool forcePresent = false) {
    streamIntervalMs = intervalMs;
    present = present || forcePresent;
    buildAnnounce();
    if (!present) DBG_PRINTLN("magnet: no MLX90393 found - module disabled");
}

inline void resetBaseline() {
    baselineReady = false;
    baselineN     = 0;
    for (auto& b : baseline) b = {0.0f, 0.0f, 0.0f};
}

// Apply runtime tunables from a "configure" command (all fields optional).
inline void applyConfigure(const JsonDocument& doc) {
    if (!doc["act_threshold_ut"].isNull()) {
        actThresholdUt = doc["act_threshold_ut"].as<float>();
    } else if (!doc["act_threshold"].isNull()) {
        // Back-compat: the old activation level was a fraction of a full-scale
        // (default 1000 uT), so convert it to the uT it used to mean.
        float fs = doc["fullscale_mt"].isNull() ? 1000.0f : doc["fullscale_mt"].as<float>();
        actThresholdUt = doc["act_threshold"].as<float>() * fs;
    }
    if (!doc["adaptive_baseline"].isNull()) adaptiveBaseline = doc["adaptive_baseline"].as<bool>();
    if (!doc["baseline_tau_ms"].isNull())   baselineTauMs    = doc["baseline_tau_ms"].as<float>();
    if (!doc["stream_vec"].isNull()) {
        streamVec = doc["stream_vec"].as<bool>();
        buildAnnounce();                    // keep the advertised capability honest
    }
    // Sensor operating point (position-ML lever: trade stream cadence for
    // noise). Values are the Adafruit enum indices: osr 0-3, filter 0-7.
    // Retuning changes the conversion characteristics, so the baseline is
    // reset - keep hands off the skin until it re-settles.
    //
    // We run inside the ESP-NOW receive callback, so the blocking I2C writes
    // cannot happen here - they would race tick()'s readSensor() on the main
    // task. Park the request and let tick() apply it.
    if (!doc["osr"].isNull())
        pendingOsr = constrain(doc["osr"].as<int>(), 0, 3);
    if (!doc["filter"].isNull())
        pendingFilter = constrain(doc["filter"].as<int>(), 0, 7);
}

// Broadcast the ready message. Must be called from setup() AFTER se::begin()
// has brought ESP-NOW up. No-op when the module is disabled.
inline void announce() {
    if (!present) return;
    se::broadcast(announceMsg);
    LOG("%s\n", announceMsg);
}

inline bool readSensor(size_t i, Vec3& out) {
    if (!ready[i]) return false;
    return mlx[i].readData(&out.x, &out.y, &out.z);
}

// Accumulate a running-average baseline over the first BASELINE_SAMPLES reads.
inline void accumulateBaseline(const Vec3* samples, const bool* valid) {
    baselineN++;
    const float a = 1.0f / static_cast<float>(baselineN);
    for (size_t i = 0; i < count; ++i) {
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
// the magnet) is absorbed while fast touches still stand out. Active sensors
// are frozen so the touch itself is never averaged into the baseline. Opt-in:
// {"cmd":"configure","adaptive_baseline":true}. The EWMA step is dt/tau, where
// dt is the real time since the last stream so the time constant stays honest.
inline void updateAdaptiveBaseline(const Vec3* samples, const bool* valid, float dtMs) {
    if (!adaptiveBaseline || baselineTauMs <= 0.0f) return;
    float a = dtMs / baselineTauMs;
    if (a > 1.0f) a = 1.0f;             // clamp if a read was badly delayed
    for (size_t i = 0; i < count; ++i) {
        if (!valid[i]) continue;
        float m = vmag({samples[i].x - baseline[i].x,
                        samples[i].y - baseline[i].y,
                        samples[i].z - baseline[i].z});
        if (m >= actThresholdUt) continue;   // touch in progress - freeze this sensor
        baseline[i].x += a * (samples[i].x - baseline[i].x);
        baseline[i].y += a * (samples[i].y - baseline[i].y);
        baseline[i].z += a * (samples[i].z - baseline[i].z);
    }
}

// Build the stream message into buf: mag/act always; vec rows when streamVec
// (whole uT - keeps the frame well under the 250-byte ESP-NOW payload limit
// even with 5 sensors).
inline void buildMessage(const Vec3* samples, const bool* valid, char* buf, size_t cap) {
    int pos = snprintf(buf, cap, "{\"type\":\"magnet\",\"mag\":[");
    bool active[MAX_SENSORS] = {};
    for (size_t i = 0; i < count; ++i) {
        float m = valid[i] ? vmag({samples[i].x - baseline[i].x,
                                    samples[i].y - baseline[i].y,
                                    samples[i].z - baseline[i].z}) : 0.0f;
        active[i] = m >= actThresholdUt;
        pos += snprintf(buf + pos, cap - pos, "%s%.3f", i ? "," : "", m);
    }
    pos += snprintf(buf + pos, cap - pos, "],\"act\":[");
    bool first = true;
    for (size_t i = 0; i < count; ++i) {
        if (!active[i]) continue;
        pos += snprintf(buf + pos, cap - pos, "%s%u", first ? "" : ",", (unsigned)i);
        first = false;
    }
    if (streamVec) {
        pos += snprintf(buf + pos, cap - pos, "],\"vec\":[");
        for (size_t i = 0; i < count; ++i) {
            Vec3 d = valid[i] ? Vec3{samples[i].x - baseline[i].x,
                                     samples[i].y - baseline[i].y,
                                     samples[i].z - baseline[i].z}
                              : Vec3{0.0f, 0.0f, 0.0f};
            // Tenth-uT precision for small deltas: quantizing to whole uT
            // would bury the weak far-sensor responses the position ML feeds
            // on. Large deltas keep whole uT so the frame stays under the
            // ESP-NOW payload cap even with 5 sensors.
            pos += snprintf(buf + pos, cap - pos, "%s[", i ? "," : "");
            const float comps[3] = {d.x, d.y, d.z};
            for (int a = 0; a < 3; ++a) {
                pos += snprintf(buf + pos, cap - pos,
                                fabsf(comps[a]) < 100.0f ? "%s%.1f" : "%s%.0f",
                                a ? "," : "", comps[a]);
            }
            pos += snprintf(buf + pos, cap - pos, "]");
        }
    }
    snprintf(buf + pos, cap - pos, "]}");
}

inline void tick(uint32_t now) {
    if (!present) return;

    // Stay off the air while a firmware update streams in: the broadcasts plus
    // blocking I2C reads starve the ota_ack replies and fail the transfer.
    if (se::ota::active) return;

    // Re-announce until the gateway is known so a late-connecting PC still
    // captures the magnet geometry.
    if (!se::node::gatewayKnown && now - lastAnnounceMs >= ANNOUNCE_INTERVAL_MS) {
        lastAnnounceMs = now;
        se::broadcast(announceMsg);
    }

    // Apply a retune parked by the RX callback, before this round's reads.
    if (pendingOsr >= 0 || pendingFilter >= 0) {
        const int osr = pendingOsr, flt = pendingFilter;
        pendingOsr = pendingFilter = -1;
        for (size_t i = 0; i < count; ++i) {
            if (!ready[i]) continue;
            if (osr >= 0) mlx[i].setOversampling((mlx90393_oversampling_t)osr);
            if (flt >= 0) mlx[i].setFilter((mlx90393_filter_t)flt);
        }
        resetBaseline();
    }

    if (now - lastStreamMs < streamIntervalMs) return;
    const float dtMs = (float)(now - lastStreamMs);   // real elapsed time, for the EWMA
    lastStreamMs = now;

    Vec3 samples[MAX_SENSORS];
    bool valid[MAX_SENSORS];
    bool any = false;
    for (size_t i = 0; i < count; ++i) {
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
}  // namespace se

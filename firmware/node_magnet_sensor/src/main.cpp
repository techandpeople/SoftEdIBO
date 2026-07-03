/**
 * SoftEdIBO — node_magnet_sensor firmware
 *
 * 4x MLX90393 magnetometers (+1 optional 5th on a second I2C bus) acting as a
 * touch-sensing board for a soft skin. All the sensing/streaming logic lives in
 * the shared firmware/common/se_magnet.h (also used by the direct actuator
 * board, which folds the same module in); this file only wires the buses, the
 * ESP-NOW command dispatch and OTA.
 *
 * Protocol (see se_magnet.h):
 *   boot:   {"status":"node_magnet_sensor_ready","sensors":N,"variant":"mlx90393"}
 *   stream: {"type":"magnet","mag":[uT..],"act":[idx..]}   (+"vec" when enabled)
 *
 * Sensor order matters: S0..S3 map to quadrants Q1(TL) Q2(TR) Q3(BL) Q4(BR),
 * which is the order of the I2C addresses below. The PC's QuadrantDetector
 * consumes the first 4 sensors; the optional 5th is appended after them.
 *
 * Commands: {"cmd":"rebaseline"}, {"cmd":"configure","act_threshold_ut":...,
 * "adaptive_baseline":...,"baseline_tau_ms":...,"stream_vec":...}.
 *
 * 3-axis streaming: build [env:vector] (-DMAG_VECTOR) to stream "vec" from
 * boot, or toggle at runtime with {"cmd":"configure","stream_vec":true}.
 * [env:release] + no configure = byte-for-byte the previous scalar protocol.
 */

#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <esp_ota_ops.h>

#include "se_espnow.h"
#include "se_magnet.h"
#include "se_ota.h"

namespace {

constexpr uint8_t I2C_SDA   = 21, I2C_SCL   = 22;   // primary bus (S0..S3)
constexpr uint8_t EXTRA_SDA = 16, EXTRA_SCL = 17;   // secondary bus (optional S4)

constexpr size_t  NUM_PRIMARY = 4;
constexpr uint8_t PRIMARY_ADDR[NUM_PRIMARY] = {0x18, 0x19, 0x1A, 0x1B};
constexpr uint8_t EXTRA_ADDR  = 0x1A;

constexpr uint32_t STREAM_INTERVAL_MS = 35;   // ~28 Hz (dedicated board — no
                                              // actuation commands to crowd out)

TwoWire extraWire = TwoWire(1);

void onReceived(const uint8_t* mac, const uint8_t* data, int len) {
    se::node::learnGateway(mac);   // remember gateway + add peer on first msg
    if (se::ota::tryHandle(data, len)) return;   // firmware update over ESP-NOW

    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return;

    const char* cmd = doc["cmd"] | "";
    if (strcmp(cmd, "ping") == 0) {
        se::node::toGateway("{\"type\":\"pong\"}");
    } else if (strcmp(cmd, "rebaseline") == 0) {
        se::magnet::resetBaseline();
    } else if (strcmp(cmd, "configure") == 0) {
        se::magnet::applyConfigure(doc);
    }
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
    delay(800);   // let the sensors power up before the first transaction

    // TODO(scale): supporting ~12 sensors needs more I2C than the MLX90393's
    // 4 addresses/bus allow — add a TCA9548A I2C mux and a {channel,address}
    // sensor table. See README.md "Planned / TODO" before adding.

    for (size_t i = 0; i < NUM_PRIMARY; ++i) {
        se::magnet::addSensor(PRIMARY_ADDR[i], &Wire);
    }
    // The optional 5th sensor only takes a slot when it actually responds, so
    // it always lands after the quadrant sensors.
    se::magnet::addSensor(EXTRA_ADDR, &extraWire, /*optional=*/true);
    // forcePresent: a dedicated magnet board with dead sensors must still
    // announce, so it shows up in scans and the fault is visible.
    se::magnet::finishInit(STREAM_INTERVAL_MS, /*forcePresent=*/true);

    if (!se::begin(onReceived)) {
        Serial.println(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }

    // Announce a WiFi OTA that just completed (no actuators here, so no shutdown
    // hook — see se_ota.h).
    se::ota::checkBootDone();

    se::magnet::announce();
}

void loop() {
    // Run a pending WiFi OTA from the main task (never returns if it starts —
    // the node reboots into the new firmware).
    se::ota::pollWifi();

    se::magnet::tick(millis());
}

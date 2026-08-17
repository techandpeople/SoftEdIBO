#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>

#include "se_magnet.h"
#include "pins.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// Optional MLX90393 magnet/touch sensing, folded into node_direct.
//
// Thin adapter over the shared firmware/common/se_magnet.h (the same module
// the standalone node_magnet_sensor firmware runs), wiring it to this board's
// I2C pins and cadence. The module is auto-detecting and self-disabling: if no
// MLX90393 answers on the bus at boot (no sensors wired), it stays inert and
// the board runs exactly as a plain node_direct - no announce, no stream, no
// I2C traffic. Commands (rebaseline / configure, incl. "stream_vec" for 3-axis
// streaming) are dispatched from commands.h.
// ---------------------------------------------------------------------------

namespace magnet {

constexpr size_t  NUM_SENSORS = 4;                        // S0..S3 -> Q1..Q4
constexpr uint8_t ADDR[NUM_SENSORS] = {0x18, 0x19, 0x1A, 0x1B};

constexpr uint32_t STREAM_INTERVAL_MS = 100;   // ~10 Hz (was 35/28 Hz - the
                                               // flood was crowding the radio
                                               // and dropping actuation commands)

// Probe the bus; the module only enables itself if at least one MLX90393
// responds. Announce is built here but broadcast later (see se_magnet.h).
inline void hardware_init() {
    Wire.begin(MAGNET_SDA, MAGNET_SCL);
    Wire.setClock(400000);
    delay(800);   // let the sensors power up before the first transaction

    for (size_t i = 0; i < NUM_SENSORS; ++i) {
        se::magnet::addSensor(ADDR[i], &Wire);
    }
    se::magnet::finishInit(STREAM_INTERVAL_MS);
}

inline void announce()                              { se::magnet::announce(); }
inline void tick(uint32_t now)                      { se::magnet::tick(now); }
inline void resetBaseline()                         { se::magnet::resetBaseline(); }
inline void applyConfigure(const JsonDocument& doc) { se::magnet::applyConfigure(doc); }

}  // namespace magnet

/**
 * SoftEdIBO — ESP-NOW Gateway Firmware (Arduino, ESP32-WROOM-32)
 *
 * The original gateway target: a classic ESP32 DevKit talking to the PC over a
 * USB-UART bridge (CH340/CP2102). Kept alongside the ESP-IDF gateway
 * (main.cpp, Seeed XIAO ESP32-C6) so either board can be used — both share the
 * ESP-NOW / MAC / radio layer in firmware/common/se_espnow.h and speak the
 * identical newline-terminated JSON serial protocol.
 *
 * Build: pio run -e esp32dev
 *
 * PC => Gateway:  {"target":"AA:BB:..:01","cmd":"inflate","chamber":0,"delta":20}
 * Gateway => PC:  {"source":"AA:BB:..:01","type":"status","chamber":0,"pressure":75}
 *                 {"status":"gateway_ready","mac":"AA:BB:..:00"}
 */

#include <Arduino.h>
#include <ArduinoJson.h>

#include "se_espnow.h"

static constexpr uint32_t SERIAL_BAUD    = 115200;
static constexpr size_t   SERIAL_BUF_LEN = 256;   // max bytes per JSON line

// ---------------------------------------------------------------------------
// ESP-NOW receive: forward node payloads to the PC with a "source" field added.
// (Runs in the WiFi task; the original gateway also serialised straight to
// Serial here, which is fine at this message rate.)
// ---------------------------------------------------------------------------

static void onReceived(const uint8_t mac_addr[6], const uint8_t* data, int len) {
    char mac[18];
    se::formatMac(mac_addr, mac);

    // Null-terminated copy so a non-JSON payload can be wrapped safely as "raw".
    char buf[251];
    int n = len < (int)sizeof(buf) - 1 ? len : (int)sizeof(buf) - 1;
    if (n < 0) n = 0;
    memcpy(buf, data, n);
    buf[n] = '\0';

    JsonDocument doc;
    if (deserializeJson(doc, buf, n) != DeserializationError::Ok) {
        doc.clear();
        doc["source"] = mac;
        doc["raw"]    = buf;
    } else {
        doc["source"] = mac;
    }
    serializeJson(doc, Serial);
    Serial.println();
}

// ---------------------------------------------------------------------------
// Serial command processing: PC line -> ESP-NOW
// ---------------------------------------------------------------------------

static void processLine(const char* line, size_t len) {
    JsonDocument doc;
    if (deserializeJson(doc, line, len) != DeserializationError::Ok) return;

    const char* targetStr = doc["target"] | "";
    uint8_t target[6];
    if (!se::parseMac(targetStr, target)) return;
    if (!se::ensurePeer(target)) return;

    doc.remove("target");   // nodes receive only the command fields
    char payload[SERIAL_BUF_LEN];
    size_t plen = serializeJson(doc, payload, sizeof(payload));
    se::send(target, reinterpret_cast<uint8_t*>(payload), plen);
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(SERIAL_BAUD);

    if (!se::begin(onReceived)) {
        Serial.println(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }

    JsonDocument ready;
    char mac[18];
    se::ownMac(mac);
    ready["status"] = "gateway_ready";
    ready["mac"]    = mac;
    serializeJson(ready, Serial);
    Serial.println();
}

void loop() {
    static char   lineBuf[SERIAL_BUF_LEN];
    static size_t lineLen = 0;

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (lineLen > 0) {
                lineBuf[lineLen] = '\0';
                processLine(lineBuf, lineLen);
                lineLen = 0;
            }
        } else if (lineLen < SERIAL_BUF_LEN - 1) {
            lineBuf[lineLen++] = c;
        }
    }
}

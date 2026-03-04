/**
 * SoftEdIBO — ESP-NOW Gateway Firmware
 * Target: ESP32-WROOM-32 (esp32dev)
 *
 * Bridges JSON commands from the PC (USB/serial) to remote ESP32 nodes via
 * ESP-NOW, and forwards replies from nodes back to the PC.
 *
 * PC => Gateway (serial, newline-terminated JSON):
 *   {"target":"AA:BB:CC:DD:EE:01","cmd":"inflate","chamber":0,"value":255}
 *   {"target":"FF:FF:FF:FF:FF:FF","cmd":"ping"}   ← broadcast scan
 *
 * Gateway => PC (serial, newline-terminated JSON):
 *   {"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":128}
 *   {"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

static constexpr uint32_t SERIAL_BAUD    = 115200;
static constexpr size_t   SERIAL_BUF_LEN = 256;    // max bytes per JSON line from PC

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static bool parseMac(const char* str, uint8_t* out) {
    return sscanf(str,
        "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
        &out[0], &out[1], &out[2], &out[3], &out[4], &out[5]) == 6;
}

static void formatMac(const uint8_t* mac, char* buf /* ≥18 */) {
    snprintf(buf, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static bool ensurePeer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return true;
    esp_now_peer_info_t peer{};
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = 0;       // follow current WiFi channel
    peer.encrypt = false;
    return esp_now_add_peer(&peer) == ESP_OK;
}

// ---------------------------------------------------------------------------
// ESP-NOW callbacks
// ---------------------------------------------------------------------------

static void onSent(const uint8_t* /*mac*/, esp_now_send_status_t /*status*/) {
    // Nothing to do — fire-and-forget for now.
}

static void onReceived(const uint8_t* mac_addr,
                       const uint8_t* data, int len) {
    char mac[18];
    formatMac(mac_addr, mac);

    // Nodes send JSON payloads; forward them to PC with a "source" field added.
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, data, len);
    if (err) {
        // Non-JSON payload — wrap it in a generic envelope.
        doc.clear();
        doc["source"] = mac;
        doc["raw"]    = (const char*)data;
    } else {
        doc["source"] = mac;
    }

    serializeJson(doc, Serial);
    Serial.println();
}

// ---------------------------------------------------------------------------
// Command processing
// ---------------------------------------------------------------------------

static void processLine(const char* line, size_t len) {
    JsonDocument doc;
    if (deserializeJson(doc, line, len) != DeserializationError::Ok) return;

    const char* targetStr = doc["target"] | "";
    uint8_t targetMac[6];
    if (!parseMac(targetStr, targetMac)) return;

    if (!ensurePeer(targetMac)) return;

    // Strip "target" from the payload so nodes don't need to handle it.
    doc.remove("target");

    char payload[SERIAL_BUF_LEN];
    size_t plen = serializeJson(doc, payload, sizeof(payload));

    esp_now_send(targetMac, reinterpret_cast<uint8_t*>(payload), plen);
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(SERIAL_BAUD);

    // ESP-NOW works in STA mode without being connected to an AP.
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    if (esp_now_init() != ESP_OK) {
        Serial.println(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }

    esp_now_register_send_cb(onSent);
    esp_now_register_recv_cb(onReceived);

    // Pre-register broadcast peer so scan/ping works without explicit add.
    static const uint8_t broadcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    ensurePeer(broadcast);

    // Report own MAC so the app can identify the gateway.
    JsonDocument ready;
    ready["status"] = "gateway_ready";
    ready["mac"]    = WiFi.macAddress();
    serializeJson(ready, Serial);
    Serial.println();
}

void loop() {
    // Fixed-size stack buffer — avoids String heap allocation on every serial line
    static char lineBuf[SERIAL_BUF_LEN];
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

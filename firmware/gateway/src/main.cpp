/**
 * SoftEdIBO — ESP-NOW Gateway Firmware (ESP-IDF)
 * Target: Seeed XIAO ESP32-C6 (RISC-V), USB-Serial/JTAG to the PC.
 *
 * Bridges JSON commands from the PC (USB serial) to remote ESP32 nodes via
 * ESP-NOW, and forwards replies from nodes back to the PC.
 *
 * The ESP-NOW / MAC / radio plumbing lives in the shared se_espnow.h, which
 * also backs the Arduino node firmwares — change ESP-NOW behaviour there once.
 *
 * PC => Gateway (serial, newline-terminated JSON):
 *   {"target":"AA:BB:CC:DD:EE:01","cmd":"inflate","chamber":0,"delta":20}
 *   {"target":"FF:FF:FF:FF:FF:FF","cmd":"ping"}   <- broadcast scan
 *
 * Gateway => PC (serial, newline-terminated JSON):
 *   {"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":75}
 *   {"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}
 */

#include <cstring>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/usb_serial_jtag.h"
#include "cJSON.h"

#include "se_espnow.h"

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

static constexpr size_t SERIAL_BUF_LEN = 256;   // max bytes per JSON line from PC
static constexpr int    ESPNOW_MAXLEN  = 250;   // max ESP-NOW payload

// Received ESP-NOW messages are handed from the WiFi task (recv callback) to a
// dedicated task via this queue, so serialization + USB writes never block the
// WiFi stack.
struct RxMsg {
    uint8_t mac[6];
    int     len;
    uint8_t data[ESPNOW_MAXLEN + 1];
};
static QueueHandle_t s_rxQueue;

// ---------------------------------------------------------------------------
// USB-Serial/JTAG I/O
// ---------------------------------------------------------------------------

static void usbWrite(const char* s, size_t len) {
    usb_serial_jtag_write_bytes(reinterpret_cast<const uint8_t*>(s), len, portMAX_DELAY);
}

static void usbWriteLine(const char* s) {
    usbWrite(s, strlen(s));
    usbWrite("\n", 1);
}

// ---------------------------------------------------------------------------
// ESP-NOW receive: WiFi-task callback enqueues, rxTask serializes to USB
// ---------------------------------------------------------------------------

static void onRecv(const uint8_t mac[6], const uint8_t* data, int len) {
    if (len <= 0 || len > ESPNOW_MAXLEN) return;
    RxMsg m;
    memcpy(m.mac, mac, 6);
    m.len = len;
    memcpy(m.data, data, len);
    m.data[len] = '\0';            // so non-JSON payloads can be wrapped as "raw"
    xQueueSend(s_rxQueue, &m, 0);  // drop if full rather than stall the WiFi task
}

static void rxTask(void*) {
    RxMsg m;
    char  mac[18];
    for (;;) {
        if (xQueueReceive(s_rxQueue, &m, portMAX_DELAY) != pdTRUE) continue;
        se::formatMac(m.mac, mac);

        // Nodes send JSON; forward with a "source" field added.
        cJSON* doc = cJSON_ParseWithLength(reinterpret_cast<const char*>(m.data), m.len);
        if (!doc) {
            // Non-JSON payload — wrap it in a generic envelope.
            doc = cJSON_CreateObject();
            cJSON_AddStringToObject(doc, "source", mac);
            cJSON_AddStringToObject(doc, "raw", reinterpret_cast<const char*>(m.data));
        } else {
            cJSON_AddStringToObject(doc, "source", mac);
        }

        char* out = cJSON_PrintUnformatted(doc);
        if (out) {
            usbWriteLine(out);
            cJSON_free(out);
        }
        cJSON_Delete(doc);
    }
}

// ---------------------------------------------------------------------------
// Serial command processing: PC line -> ESP-NOW
// ---------------------------------------------------------------------------

static void processLine(const char* line, size_t len) {
    cJSON* doc = cJSON_ParseWithLength(line, len);
    if (!doc) return;

    cJSON* target = cJSON_GetObjectItemCaseSensitive(doc, "target");
    uint8_t mac[6];
    if (cJSON_IsString(target) && se::parseMac(target->valuestring, mac) &&
        se::ensurePeer(mac)) {
        // Strip "target" so nodes receive only the command fields.
        cJSON_DeleteItemFromObjectCaseSensitive(doc, "target");
        char* payload = cJSON_PrintUnformatted(doc);
        if (payload) {
            size_t plen = strlen(payload);
            if (plen <= ESPNOW_MAXLEN)
                se::send(mac, reinterpret_cast<const uint8_t*>(payload), plen);
            cJSON_free(payload);
        }
    }
    cJSON_Delete(doc);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

extern "C" void app_main(void) {
    usb_serial_jtag_driver_config_t ucfg = {
        .tx_buffer_size = 1024,
        .rx_buffer_size = 1024,
    };
    usb_serial_jtag_driver_install(&ucfg);

    s_rxQueue = xQueueCreate(16, sizeof(RxMsg));

    if (!se::begin(onRecv)) {
        usbWriteLine("{\"error\":\"esp_now_init_failed\"}");
        return;
    }

    xTaskCreate(rxTask, "espnow_rx", 4096, nullptr, 5, nullptr);

    // Report own MAC so the app can identify the gateway.
    char mac[18];
    se::ownMac(mac);
    char ready[64];
    snprintf(ready, sizeof(ready),
             "{\"status\":\"gateway_ready\",\"mac\":\"%s\"}", mac);
    usbWriteLine(ready);

    // Read serial line-by-line into a fixed stack buffer (no heap per line).
    static char line[SERIAL_BUF_LEN];
    size_t      llen = 0;
    uint8_t     ch;
    for (;;) {
        if (usb_serial_jtag_read_bytes(&ch, 1, pdMS_TO_TICKS(20)) <= 0) continue;
        if (ch == '\n' || ch == '\r') {
            if (llen > 0) {
                line[llen] = '\0';
                processLine(line, llen);
                llen = 0;
            }
        } else if (llen < SERIAL_BUF_LEN - 1) {
            line[llen++] = static_cast<char>(ch);
        }
    }
}

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
// Optional SoftAP (build flag -DGATEWAY_AP, see platformio.ini)
//
// When enabled the gateway also brings up a WiFi access point so clients such
// as the Thymio robots can associate, WITHOUT giving up the ESP-NOW link to the
// nodes. Both share a single 2.4 GHz PHY, so the AP and ESP-NOW must live on
// the SAME channel (default 1, matching the nodes). Preferred on the XIAO
// ESP32-S3 (dual-core + more RAM); the bridge runs on APP_CPU so AP traffic
// never stalls forwarding to the PC (see app_main).
// ---------------------------------------------------------------------------

#ifdef GATEWAY_AP
#include "esp_wifi.h"
#include "esp_netif.h"
#include "nvs.h"

#ifndef GATEWAY_AP_SSID
#define GATEWAY_AP_SSID    "SoftEdIBO"
#endif
#ifndef GATEWAY_AP_PASS
#define GATEWAY_AP_PASS    "softedibo"   // >=8 chars => WPA2; "" => open network
#endif
#ifndef GATEWAY_AP_CHANNEL
#define GATEWAY_AP_CHANNEL 1             // must match the nodes' ESP-NOW channel
#endif
#ifndef GATEWAY_AP_MAX_CONN
#define GATEWAY_AP_MAX_CONN 8
#endif

// Runtime AP settings. Compiled-in values are the defaults; the PC can override
// SSID/password at runtime via the "set_ap" gateway command, persisted in NVS
// (namespace "ap") so they survive reboots. Read back with "get_ap".
static char    s_apSsid[33] = GATEWAY_AP_SSID;
static char    s_apPass[65] = GATEWAY_AP_PASS;
static uint8_t s_apChannel  = GATEWAY_AP_CHANNEL;

static const char* AP_NVS_NAMESPACE = "ap";

// Overlay any NVS-stored overrides on top of the compiled defaults.
static void apLoadConfig(void) {
    nvs_handle_t h;
    if (nvs_open(AP_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
    size_t n = sizeof(s_apSsid);
    nvs_get_str(h, "ssid", s_apSsid, &n);
    n = sizeof(s_apPass);
    nvs_get_str(h, "pass", s_apPass, &n);
    uint8_t ch;
    if (nvs_get_u8(h, "chan", &ch) == ESP_OK && ch >= 1 && ch <= 13)
        s_apChannel = ch;
    nvs_close(h);
}

// Apply the current s_ap* settings to the live AP interface (also used at boot).
static void apApply(void) {
    wifi_config_t cfg = {};
    size_t slen = strlen(s_apSsid);
    if (slen > sizeof(cfg.ap.ssid)) slen = sizeof(cfg.ap.ssid);
    memcpy(cfg.ap.ssid, s_apSsid, slen);
    cfg.ap.ssid_len       = slen;
    cfg.ap.channel        = s_apChannel;
    cfg.ap.max_connection = GATEWAY_AP_MAX_CONN;
    size_t plen = strlen(s_apPass);
    if (plen >= 8) {
        if (plen > sizeof(cfg.ap.password)) plen = sizeof(cfg.ap.password);
        memcpy(cfg.ap.password, s_apPass, plen);
        cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        cfg.ap.authmode = WIFI_AUTH_OPEN;  // password too short for WPA2
    }
    esp_wifi_set_config(WIFI_IF_AP, &cfg);
    // Lock the shared radio to the AP channel so ESP-NOW peers (channel 0 =
    // "current channel") stay reachable.
    esp_wifi_set_channel(s_apChannel, WIFI_SECOND_CHAN_NONE);
}

// Persist new settings to NVS and re-apply them live. Returns false on a bad
// password (1..7 chars can't form a WPA2 key); "" (open) and >=8 are accepted.
static bool apSaveConfig(const char* ssid, const char* pass, int channel) {
    size_t plen = strlen(pass);
    if (plen > 0 && plen < 8) return false;
    if (channel < 1 || channel > 13) channel = s_apChannel;

    snprintf(s_apSsid, sizeof(s_apSsid), "%s", ssid);
    snprintf(s_apPass, sizeof(s_apPass), "%s", pass);
    s_apChannel = static_cast<uint8_t>(channel);

    nvs_handle_t h;
    if (nvs_open(AP_NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, "ssid", s_apSsid);
        nvs_set_str(h, "pass", s_apPass);
        nvs_set_u8(h, "chan", s_apChannel);
        nvs_commit(h);
        nvs_close(h);
    }
    apApply();
    return true;
}

// Bring the AP up on top of the STA interface se::begin() already started.
static void apStart(void) {
    esp_netif_create_default_wifi_ap();   // AP netif + DHCP server handlers
    esp_wifi_set_mode(WIFI_MODE_APSTA);   // keep STA (ESP-NOW) + add the AP
    apLoadConfig();
    apApply();
}
#endif  // GATEWAY_AP

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

static constexpr size_t SERIAL_BUF_LEN = 512;   // max bytes per JSON line from PC
                                                // (OTA data lines carry base64 +
                                                // a "target" MAC; 256 truncated)
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
    // Single write to avoid USB packet fragmentation splitting the line.
    size_t len = strlen(s);
    char buf[512];
    if (len + 1 < sizeof(buf)) {
        memcpy(buf, s, len);
        buf[len]     = '\n';
        buf[len + 1] = '\0';
        usbWrite(buf, len + 1);
    } else {
        usbWrite(s, len);
        usbWrite("\n", 1);
    }
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
// Gateway-local commands: lines WITHOUT a "target" are meant for the gateway
// itself (not relayed to a node). Currently used to read/set the SoftAP config.
// ---------------------------------------------------------------------------

static void handleGatewayCmd(cJSON* doc) {
    cJSON* cmd = cJSON_GetObjectItemCaseSensitive(doc, "cmd");
    if (!cJSON_IsString(cmd)) return;

#ifdef GATEWAY_AP
    if (strcmp(cmd->valuestring, "get_ap") == 0) {
        char buf[160];
        snprintf(buf, sizeof(buf),
                 "{\"type\":\"ap_config\",\"ssid\":\"%s\",\"channel\":%d,"
                 "\"secured\":%s}",
                 s_apSsid, s_apChannel, (strlen(s_apPass) >= 8) ? "true" : "false");
        usbWriteLine(buf);
        return;
    }
    if (strcmp(cmd->valuestring, "set_ap") == 0) {
        cJSON* j_ssid = cJSON_GetObjectItemCaseSensitive(doc, "ssid");
        cJSON* j_pass = cJSON_GetObjectItemCaseSensitive(doc, "pass");
        cJSON* j_chan = cJSON_GetObjectItemCaseSensitive(doc, "channel");
        const char* ssid = cJSON_IsString(j_ssid) ? j_ssid->valuestring : s_apSsid;
        const char* pass = cJSON_IsString(j_pass) ? j_pass->valuestring : s_apPass;
        int   chan       = cJSON_IsNumber(j_chan) ? j_chan->valueint     : s_apChannel;
        if (strlen(ssid) == 0) {
            usbWriteLine("{\"type\":\"ap_set\",\"ok\":false,\"reason\":\"empty_ssid\"}");
        } else if (!apSaveConfig(ssid, pass, chan)) {
            usbWriteLine("{\"type\":\"ap_set\",\"ok\":false,\"reason\":\"bad_password\"}");
        } else {
            char buf[96];
            snprintf(buf, sizeof(buf),
                     "{\"type\":\"ap_set\",\"ok\":true,\"ssid\":\"%s\"}", s_apSsid);
            usbWriteLine(buf);
        }
        return;
    }
#endif

    if (strcmp(cmd->valuestring, "get_ap") == 0 ||
        strcmp(cmd->valuestring, "set_ap") == 0)
        usbWriteLine("{\"type\":\"error\",\"reason\":\"ap_not_supported\"}");
}

// ---------------------------------------------------------------------------
// Serial command processing: PC line -> ESP-NOW (or gateway-local)
// ---------------------------------------------------------------------------

static void processLine(const char* line, size_t len) {
    cJSON* doc = cJSON_ParseWithLength(line, len);
    if (!doc) {
        // A PC command arrived unparseable — almost always serial byte loss /
        // truncation on the USB link. Report it instead of silently dropping it,
        // so a swallowed command (e.g. a missed "stop") is visible on the PC.
        cJSON* err = cJSON_CreateObject();
        if (err) {
            cJSON_AddStringToObject(err, "type", "error");
            cJSON_AddStringToObject(err, "reason", "bad_cmd_json");
            cJSON_AddNumberToObject(err, "len", (double)len);
            cJSON_AddStringToObject(err, "raw", line);
            char* out = cJSON_PrintUnformatted(err);
            if (out) { usbWriteLine(out); cJSON_free(out); }
            cJSON_Delete(err);
        }
        return;
    }

    cJSON* target = cJSON_GetObjectItemCaseSensitive(doc, "target");
    uint8_t mac[6];
    if (!cJSON_IsString(target)) {
        // No target → command for the gateway itself.
        handleGatewayCmd(doc);
    } else if (se::parseMac(target->valuestring, mac) && se::ensurePeer(mac)) {
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

#ifdef GATEWAY_AP
    apStart();
#endif

#if CONFIG_FREERTOS_NUMBER_OF_CORES > 1
    // Dual-core (S3): WiFi/AP stays on PRO_CPU (core 0, IDF default); run the
    // USB serialization task on APP_CPU (core 1) so AP traffic never stalls
    // forwarding node replies to the PC. Matters most with GATEWAY_AP active.
    xTaskCreatePinnedToCore(rxTask, "espnow_rx", 4096, nullptr, 5, nullptr, 1);
#else
    xTaskCreate(rxTask, "espnow_rx", 4096, nullptr, 5, nullptr);
#endif

    // Report own MAC so the app can identify the gateway.
    char mac[18];
    se::ownMac(mac);
    char ready[96];
#ifdef GATEWAY_AP
    snprintf(ready, sizeof(ready),
             "{\"status\":\"gateway_ready\",\"mac\":\"%s\",\"ap\":\"%s\"}",
             mac, GATEWAY_AP_SSID);
#else
    snprintf(ready, sizeof(ready),
             "{\"status\":\"gateway_ready\",\"mac\":\"%s\"}", mac);
#endif
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

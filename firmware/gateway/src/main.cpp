/**
 * SoftEdIBO - Gateway Firmware (ESP-IDF)
 * Target: Seeed XIAO ESP32-S3, USB-Serial/JTAG to the PC.
 *
 * Bridges JSON commands from the PC (USB serial) to remote ESP32 nodes via
 * ESP-NOW, and forwards replies from nodes back to the PC.
 *
 * The ESP-NOW / MAC / radio plumbing lives in the shared se_espnow.h, which
 * also backs the Arduino node firmwares - change ESP-NOW behaviour there once.
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
#ifdef GATEWAY_THYMIO
#include "driver/uart.h"
#include "driver/gpio.h"
#endif
#include "esp_log.h"
#include "esp_rom_md5.h"
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
#include "esp_http_server.h"
#include "esp_heap_caps.h"
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

// The AP credentials live on THIS gateway (NVS, Tools -> Gateway WiFi AP...), so
// inject them into any forwarded ota_wifi that doesn't carry its own - the PC
// then never needs to know or store them, and a renamed AP can't break OTA.
static void apInjectCreds(cJSON* doc) {
    cJSON* cmd = cJSON_GetObjectItemCaseSensitive(doc, "cmd");
    if (!cJSON_IsString(cmd) || strcmp(cmd->valuestring, "ota_wifi") != 0) return;
    cJSON* ssid = cJSON_GetObjectItemCaseSensitive(doc, "ssid");
    if (cJSON_IsString(ssid) && ssid->valuestring[0] != '\0') return;   // explicit override
    cJSON_DeleteItemFromObjectCaseSensitive(doc, "ssid");
    cJSON_DeleteItemFromObjectCaseSensitive(doc, "pass");
    cJSON_AddStringToObject(doc, "ssid", s_apSsid);
    cJSON_AddStringToObject(doc, "pass", s_apPass);
}
#else
static void apInjectCreds(cJSON*) {}
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

// Handles for the RX tasks (set by xTaskCreate in app_main).
static TaskHandle_t s_rxTaskHandle = nullptr;
#ifdef GATEWAY_THYMIO
static TaskHandle_t s_thymioRxHandle = nullptr;
#endif

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
            // Non-JSON payload - wrap it in a generic envelope.
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

#ifdef GATEWAY_THYMIO
// ---------------------------------------------------------------------------
// Thymio RCP link (UART to the co-processor C6 that speaks 802.15.4 to Thymios)
//
// A third route alongside gateway-local and ESP-NOW: PC lines with
// {"target":"thymio",...} are forwarded verbatim (minus "target") to the C6 over
// UART; lines the C6 sends back are tagged {"source":"thymio",...} so the PC can
// route/filter them. The gateway stays a transparent pipe - no Thymio-data
// filtering here; that belongs in the C6 (source) or the PC (sink) once the data
// shape is known. See docs/THYMIO_WIRELESS_CONTROL.md.
// ---------------------------------------------------------------------------
static constexpr uart_port_t THYMIO_UART    = UART_NUM_1;
static constexpr int         THYMIO_UART_TX  = 43;   // S3 D6 -> C6 D7/RX
static constexpr int         THYMIO_UART_RX  = 44;   // S3 D7 <- C6 D6/TX
static constexpr int         THYMIO_BAUD     = 115200;

static void thymioUartInit() {
    uart_config_t uc = {};
    uc.baud_rate  = THYMIO_BAUD;
    uc.data_bits  = UART_DATA_8_BITS;
    uc.parity     = UART_PARITY_DISABLE;
    uc.stop_bits  = UART_STOP_BITS_1;
    uc.flow_ctrl  = UART_HW_FLOWCTRL_DISABLE;
    uc.source_clk = UART_SCLK_DEFAULT;
    uart_driver_install(THYMIO_UART, 2048, 0, 0, nullptr, 0);
    uart_param_config(THYMIO_UART, &uc);
    uart_set_pin(THYMIO_UART, THYMIO_UART_TX, THYMIO_UART_RX,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
}

// PC -> C6: forward the command (minus "target") as one JSON line.
static void thymioForward(cJSON* doc) {
    cJSON_DeleteItemFromObjectCaseSensitive(doc, "target");
    char* out = cJSON_PrintUnformatted(doc);
    if (out) {
        uart_write_bytes(THYMIO_UART, out, strlen(out));
        uart_write_bytes(THYMIO_UART, "\n", 1);
        cJSON_free(out);
    }
}

// C6 -> PC: read lines from the C6 and forward them tagged with source "thymio".
static void thymioRxTask(void*) {
    // Must hold the C6's longest line: a sniff frame is ~54 chars of envelope +
    // 2 hex chars per captured byte (~320 chars at the full 127-byte PSDU), so
    // 256 truncated near-max frames into unparseable "raw" lines.
    static char line[384];
    size_t      llen = 0;
    uint8_t     rx[128];
    for (;;) {
        int got = uart_read_bytes(THYMIO_UART, rx, sizeof(rx), pdMS_TO_TICKS(20));
        if (got <= 0) continue;
        for (int i = 0; i < got; ++i) {
            uint8_t ch = rx[i];
            if (ch == '\n' || ch == '\r') {
                if (llen == 0) continue;
                line[llen] = '\0';
                cJSON* doc = cJSON_Parse(line);
                if (!doc) {
                    doc = cJSON_CreateObject();
                    cJSON_AddStringToObject(doc, "raw", line);
                }
                cJSON_AddStringToObject(doc, "source", "thymio");
                char* out = cJSON_PrintUnformatted(doc);
                if (out) { usbWriteLine(out); cJSON_free(out); }
                cJSON_Delete(doc);
                llen = 0;
            } else if (llen == 0 && ch != '{') {
                // Skip leading garbage before the JSON - e.g. a stray byte glitched
                // onto the C6's UART TX while it resets after a WiFi-OTA, which would
                // otherwise corrupt the rcp_ready "done" line.
            } else if (llen < sizeof(line) - 1) {
                line[llen++] = static_cast<char>(ch);
            }
        }
    }
}

#endif  // GATEWAY_THYMIO

#ifdef GATEWAY_AP
// ---------------------------------------------------------------------------
// WiFi-OTA image proxy (S3 + PSRAM only)
//
// Fast node firmware updates without the PC ever leaving the USB cable: the PC
// streams the image to the gateway over USB (ota_store_* commands), the gateway
// buffers it in PSRAM and serves it over HTTP on its SoftAP. The node then joins
// the AP and HTTP-downloads it (see firmware/common/se_ota.h::doWifiUpdate, and
// the PC side in src/hardware/wifi_ota_updater.py).
//
// PSRAM is required (the ~800 KB image won't fit in internal RAM). If the alloc
// fails (PSRAM not enabled / not present, e.g. the C6) we report ota_store_error
// instead of crashing, so the ESP-NOW bridge keeps working regardless.
// ---------------------------------------------------------------------------

static constexpr size_t OTA_IMAGE_MAX = 4 * 1024 * 1024;  // sanity cap

// Cumulative ack cadence for the staging stream: the gateway tells the PC how
// many bytes it has actually appended each time s_otaLen crosses a multiple of
// this, so the PC can flow-control and never outrun the USB RX buffer. Without
// it the PC blasts the whole image back-to-back, the RX buffer overflows, bytes
// are silently dropped, and ota_store_end reports size_mismatch.
static constexpr size_t OTA_ACK_EVERY = 4 * 1024;

static uint8_t*       s_otaImage   = nullptr;  // PSRAM buffer holding the node image
static size_t         s_otaCap     = 0;        // allocated capacity (= expected size)
static size_t         s_otaLen     = 0;        // bytes written so far
static char           s_otaMd5[33] = {};       // md5 hex from the PC, served as x-MD5
static bool           s_otaReady   = false;    // image complete and servable
static httpd_handle_t s_httpd      = nullptr;

// Standard-alphabet base64 decode (mirrors se_ota.h). Returns bytes, or -1.
static int otaB64Decode(const char* in, uint8_t* out, size_t outcap) {
    auto val = [](char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '+') return 62;
        if (c == '/') return 63;
        return -1;
    };
    size_t o = 0;
    int buf = 0, bits = 0;
    for (const char* p = in; *p; ++p) {
        if (*p == '=') break;
        int v = val(*p);
        if (v < 0) return -1;
        buf = (buf << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= outcap) return -1;
            out[o++] = (buf >> bits) & 0xFF;
        }
    }
    return static_cast<int>(o);
}

static esp_err_t otaHttpGet(httpd_req_t* req) {
    if (!s_otaReady || !s_otaImage) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "no image staged");
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "application/octet-stream");
    if (s_otaMd5[0]) httpd_resp_set_hdr(req, "x-MD5", s_otaMd5);
    // httpd_resp_send sets Content-Length from the length and chunks internally.
    return httpd_resp_send(req, reinterpret_cast<const char*>(s_otaImage), s_otaLen);
}

static void otaHttpStart() {
    if (s_httpd) return;
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port      = 80;
    cfg.lru_purge_enable = true;
    // A client writing the image to flash (erase+write is slow) can stop reading the
    // socket for several seconds; the 5 s default would cut it off mid-image and the
    // node's HTTPUpdate fails. Give slow clients room on a contended SoftAP.
    cfg.send_wait_timeout = 30;   // seconds
    cfg.recv_wait_timeout = 30;
    if (httpd_start(&s_httpd, &cfg) != ESP_OK) { s_httpd = nullptr; return; }
    httpd_uri_t uri = {};
    uri.uri     = "/fw";
    uri.method  = HTTP_GET;
    uri.handler = otaHttpGet;
    httpd_register_uri_handler(s_httpd, &uri);
}

static void otaStoreReset() {
    if (s_otaImage) { heap_caps_free(s_otaImage); s_otaImage = nullptr; }
    s_otaCap = s_otaLen = 0;
    s_otaReady = false;
    s_otaMd5[0] = '\0';
}

// Recompute the MD5 of the stored image and compare against the one the PC sent
// (served as x-MD5). Catches a size-preserving staging corruption here, where the
// PC can retry, instead of letting the node download bad bytes and fail its own MD5
// check opaquely. True if no md5 was provided (nothing to check) or it matches.
static bool otaImageMd5Matches() {
    if (!s_otaMd5[0]) return true;
    md5_context_t ctx;
    uint8_t digest[16];
    esp_rom_md5_init(&ctx);
    esp_rom_md5_update(&ctx, s_otaImage, s_otaLen);
    esp_rom_md5_final(digest, &ctx);
    char hex[33];
    for (int i = 0; i < 16; ++i) snprintf(hex + i * 2, 3, "%02x", digest[i]);
    return strcasecmp(hex, s_otaMd5) == 0;
}

// Handles the ota_store_* gateway-local commands; returns true if it consumed one.
static bool handleOtaStore(const char* cmd, cJSON* doc) {
    if (strcmp(cmd, "ota_store_begin") == 0) {
        cJSON* j_size = cJSON_GetObjectItemCaseSensitive(doc, "size");
        cJSON* j_md5  = cJSON_GetObjectItemCaseSensitive(doc, "md5");
        size_t size   = cJSON_IsNumber(j_size) ? (size_t)j_size->valuedouble : 0;
        otaStoreReset();
        if (size == 0 || size > OTA_IMAGE_MAX) {
            usbWriteLine("{\"type\":\"ota_store_error\",\"reason\":\"bad_size\"}");
            return true;
        }
        s_otaImage = static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_SPIRAM));
        if (!s_otaImage) {
            usbWriteLine("{\"type\":\"ota_store_error\",\"reason\":\"no_psram\"}");
            return true;
        }
        s_otaCap = size;
        if (cJSON_IsString(j_md5)) strlcpy(s_otaMd5, j_md5->valuestring, sizeof(s_otaMd5));
        otaHttpStart();
        usbWriteLine(s_httpd ? "{\"type\":\"ota_store_ready\"}"
                             : "{\"type\":\"ota_store_error\",\"reason\":\"httpd_failed\"}");
        return true;
    }

    if (strcmp(cmd, "ota_store_data") == 0) {
        // Append the chunk and emit a cumulative ack every OTA_ACK_EVERY bytes so
        // the PC can window its sends (USB is ordered but NOT lossless once the RX
        // buffer overflows). Silently ignored if not storing.
        cJSON* j_data = cJSON_GetObjectItemCaseSensitive(doc, "data");
        if (s_otaImage && cJSON_IsString(j_data)) {
            size_t before = s_otaLen;
            int n = otaB64Decode(j_data->valuestring, s_otaImage + s_otaLen,
                                 s_otaCap - s_otaLen);
            if (n > 0) s_otaLen += n;
            if (s_otaLen / OTA_ACK_EVERY != before / OTA_ACK_EVERY) {
                char buf[48];
                snprintf(buf, sizeof(buf),
                         "{\"type\":\"ota_store_ack\",\"len\":%u}",
                         (unsigned)s_otaLen);
                usbWriteLine(buf);
            }
        }
        return true;
    }

    if (strcmp(cmd, "ota_store_end") == 0) {
        if (!s_otaImage) {
            usbWriteLine("{\"type\":\"ota_store_error\",\"reason\":\"not_storing\"}");
            return true;
        }
        if (s_otaLen != s_otaCap) {
            char buf[96];
            snprintf(buf, sizeof(buf),
                     "{\"type\":\"ota_store_error\",\"reason\":\"size_mismatch\","
                     "\"got\":%u,\"want\":%u}", (unsigned)s_otaLen, (unsigned)s_otaCap);
            usbWriteLine(buf);
            otaStoreReset();
            return true;
        }
        if (!otaImageMd5Matches()) {
            usbWriteLine("{\"type\":\"ota_store_error\",\"reason\":\"md5_mismatch\"}");
            otaStoreReset();
            return true;
        }
        s_otaReady = true;
        // Build the URL from the live AP IP (default 192.168.4.1 if unavailable).
        char url[40] = "http://192.168.4.1/fw";
        esp_netif_t* ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
        esp_netif_ip_info_t ip;
        if (ap && esp_netif_get_ip_info(ap, &ip) == ESP_OK)
            snprintf(url, sizeof(url), "http://" IPSTR "/fw", IP2STR(&ip.ip));
        char buf[96];
        snprintf(buf, sizeof(buf),
                 "{\"type\":\"ota_stored\",\"ok\":true,\"size\":%u,\"url\":\"%s\"}",
                 (unsigned)s_otaLen, url);
        usbWriteLine(buf);
        return true;
    }

    return false;
}
#endif  // GATEWAY_AP

// ---------------------------------------------------------------------------
// Gateway-local commands: lines WITHOUT a "target" are meant for the gateway
// itself (not relayed to a node). Currently used to read/set the SoftAP config.
// ---------------------------------------------------------------------------

static void handleGatewayCmd(cJSON* doc) {
    cJSON* cmd = cJSON_GetObjectItemCaseSensitive(doc, "cmd");
    if (!cJSON_IsString(cmd)) return;

#ifdef GATEWAY_AP
    if (handleOtaStore(cmd->valuestring, doc)) return;

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

    // WiFi-OTA staging only exists on the SoftAP build; tell the PC plainly so it
    // can fall back to the ESP-NOW transport instead of timing out.
    if (strncmp(cmd->valuestring, "ota_store_", 10) == 0)
        usbWriteLine("{\"type\":\"ota_store_error\",\"reason\":\"ap_not_supported\"}");
}

// ---------------------------------------------------------------------------
// Serial command processing: PC line -> ESP-NOW (or gateway-local)
// ---------------------------------------------------------------------------

static void processLine(const char* line, size_t len) {
    cJSON* doc = cJSON_ParseWithLength(line, len);
    if (!doc) {
        // A PC command arrived unparseable - almost always serial byte loss /
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
        // No target -> command for the gateway itself.
        handleGatewayCmd(doc);
#ifdef GATEWAY_THYMIO
    } else if (strcmp(target->valuestring, "thymio") == 0) {
        // Thymio route -> forward to the C6 radio co-processor over UART.
        apInjectCreds(doc);
        thymioForward(doc);
#endif
    } else if (se::parseMac(target->valuestring, mac) && se::ensurePeer(mac)) {
        // Strip "target" so nodes receive only the command fields.
        apInjectCreds(doc);
        cJSON_DeleteItemFromObjectCaseSensitive(doc, "target");
        char* payload = cJSON_PrintUnformatted(doc);
        if (payload) {
            size_t plen = strlen(payload);
            if (plen <= ESPNOW_MAXLEN)
                // Paced: PC commands arrive back-to-back (a batch inflate sends
                // set_max/set_min/inflate per chamber); a bare esp_now_send burst
                // overruns the radio TX queue and drops frames, so only some
                // chambers actuate. sendPaced waits for each TX before the next.
                se::sendPaced(mac, reinterpret_cast<const uint8_t*>(payload), plen);
            cJSON_free(payload);
        }
    }
    cJSON_Delete(doc);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

extern "C" void app_main(void) {
    // The USB-Serial/JTAG port IS the JSON protocol channel, so keep IDF's own logs
    // (WiFi/lwIP driver chatter, loud during SoftAP OTA) off it - they show up on the
    // PC as "Invalid JSON" noise. Safe now that the PC read-loop no longer relies on
    // that stream to resync (see ESPNowGateway._read_loop). Rebuild without this to
    // debug at the console.
    esp_log_level_set("*", ESP_LOG_NONE);

    usb_serial_jtag_driver_config_t ucfg = {
        .tx_buffer_size = 1024,
        // Large RX buffer so the bulk OTA staging stream (PC -> gateway) has
        // headroom while a line is being parsed/decoded. Sized comfortably above
        // the PC's in-flight window (see WifiOTAUpdater.STORE_WINDOW).
        .rx_buffer_size = 16384,
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
    xTaskCreatePinnedToCore(rxTask, "espnow_rx", 4096, nullptr, 5, &s_rxTaskHandle, 1);
#else
    xTaskCreate(rxTask, "espnow_rx", 4096, nullptr, 5, &s_rxTaskHandle);
#endif

#ifdef GATEWAY_THYMIO
    // Bring up the UART link to the C6 RCP and forward its replies to the PC.
    thymioUartInit();
    xTaskCreate(thymioRxTask, "thymio_rx", 4096, nullptr, 5, &s_thymioRxHandle);
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

    // Read serial in bulk (draining the RX buffer fast keeps the OTA staging
    // stream from overflowing it), splitting into lines in a fixed stack buffer.
    static char    line[SERIAL_BUF_LEN];
    size_t         llen = 0;
    static uint8_t rx[256];
    for (;;) {
        int got = usb_serial_jtag_read_bytes(rx, sizeof(rx), pdMS_TO_TICKS(20));
        if (got <= 0) continue;
        for (int i = 0; i < got; ++i) {
            uint8_t ch = rx[i];
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
}

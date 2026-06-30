#pragma once
/**
 * SoftEdIBO — shared ESP-NOW OTA receiver (Arduino nodes only).
 *
 * Lets the PC push a new firmware image to a node wirelessly, relayed verbatim
 * by the gateway over the existing JSON/ESP-NOW pipe — no WiFi/AP needed. This
 * is the node-side counterpart of src/hardware/node_ota_updater.py.
 *
 * The PC drives the whole transfer; the node only writes flash + ACKs:
 *   PC -> node  {"cmd":"ota_begin","size":N,"md5":"<hex>","chunk":144}
 *   node -> PC  {"type":"ota_ready"}            | {"type":"ota_error","reason":..}
 *   PC -> node  {"cmd":"ota_data","seq":S,"data":"<base64>"}   (seq 0,1,2,…)
 *   node -> PC  {"type":"ota_ack","seq":S}      | {"type":"ota_error","reason":..}
 *   PC -> node  {"cmd":"ota_end"}
 *   node        verify MD5, arm the boot-done flag, reboot. The *new* firmware
 *               broadcasts {"type":"ota_done"} from checkBootDone() once it
 *               actually boots  | {"type":"ota_error","reason":"verify_failed"}
 *
 * Chunks are written inline in the ESP-NOW recv callback (WiFi task). A 144-byte
 * flash write is a few ms — acceptable, and far simpler than buffering through a
 * queue. Integrity is verified by Update via the MD5 supplied in ota_begin, and
 * success is confirmed only after the new image boots (not before the reboot),
 * so an image that passes MD5 but fails to boot is reported as a failure rather
 * than a false success.
 *
 * Requires an OTA partition layout (app0/app1/otadata). esp32dev's default
 * partition table provides it; see each node's platformio.ini.
 *
 * Usage (in each node's recv callback, after se::node::learnGateway):
 *   if (se::ota::tryHandle(data, len)) return;   // consumed an ota_* message
 */

#ifdef ARDUINO

#include <Arduino.h>
#include <Update.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include <cstring>

#include "se_espnow.h"

namespace se {
namespace ota {

inline bool     active      = false;   // an image is being received
inline uint32_t expectedSeq = 0;       // next chunk index we expect
inline size_t   total       = 0;       // image size from ota_begin
inline size_t   written     = 0;       // bytes flashed so far

// --- WiFi (fast) OTA -------------------------------------------------------
// The ESP-NOW path above is robust but slow: the gateway->node relay caps the
// payload at ~190 B, so the image crawls across as ~96-byte base64 chunks. For a
// big image it is far quicker to pull the firmware over WiFi. On the `ota_wifi`
// command the node joins the gateway's SoftAP as a station and HTTP-downloads
// the image from a tiny server the PC hosts on that network. The PC side is
// src/hardware/wifi_ota_updater.py; the gateway must run the SoftAP build
// (-DGATEWAY_AP) and the PC must be joined to that access point.
//
// A successful HTTPUpdate reboots from inside update(), so the node can't reply
// over ESP-NOW afterwards (the radio is in STA mode). We instead arm a flag in
// RTC memory before the update; the fresh boot sees it and broadcasts ota_done
// (see checkBootDone()). A node that fails to join the AP or download just
// reboots back into its current firmware, so a failed WiFi update is safe.

// Optional node-supplied hook: called right before the node leaves ESP-NOW for
// WiFi, so an actuator node can halt pumps/valves before it goes off the air.
inline void (*beforeWifiHook)() = nullptr;

// A WiFi-OTA request is handed from the recv callback (WiFi task) to the main
// loop via this flag + buffers, NOT acted on in place: the STA connect blocks
// for seconds and esp_now_deinit() tears down ESP-NOW, which is unsafe from
// inside the WiFi task that dispatches the recv callback. pollWifi() runs it
// from loop(). (Flash writes for the ESP-NOW path are quick + synchronous, so
// those stay inline; a WiFi connect needs the WiFi task to keep running.)
inline volatile bool wifiPending = false;
inline char wifiSsid[33] = {};
inline char wifiPass[65] = {};
inline char wifiUrl[160] = {};

// Armed before any OTA-triggered reboot (the WiFi HTTPUpdate path and the
// ESP-NOW ota_end path both set it), so the freshly-booted new firmware can
// confirm success from checkBootDone() — proving the image actually boots, not
// just that it verified. Survives the software reset (RTC_NOINIT memory is not
// cleared on a restart, only on power loss). Deliberately NOT inline/initialised:
// a zero-init would defeat the noinit section, and exactly one translation unit
// (a node's single main.cpp) includes this header, so there is no ODR clash.
RTC_NOINIT_ATTR uint32_t otaDoneFlag;
constexpr uint32_t OTA_DONE_MAGIC = 0xB007D02E;  // "boot done"

inline void reply(const char* s) { se::node::toGateway(s); }

inline void replyAck(uint32_t seq) {
    char buf[40];
    snprintf(buf, sizeof(buf), "{\"type\":\"ota_ack\",\"seq\":%u}", (unsigned)seq);
    reply(buf);
}

inline void fail(const char* reason) {
    if (active) { Update.abort(); active = false; }
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"type\":\"ota_error\",\"reason\":\"%s\"}", reason);
    reply(buf);
}

// Standard-alphabet base64 decode. Returns decoded byte count, or -1 on error.
inline int b64decode(const char* in, size_t inlen, uint8_t* out, size_t outcap) {
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
    for (size_t i = 0; i < inlen; i++) {
        char c = in[i];
        if (c == '=' || c == '\0') break;
        int v = val(c);
        if (v < 0) return -1;
        buf = (buf << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= outcap) return -1;
            out[o++] = (buf >> bits) & 0xFF;
        }
    }
    return (int)o;
}

// Arm the done flag, join the AP and pull the image over HTTP. Runs from the
// main task (via pollWifi), never the recv callback. Returns only on failure — a
// successful HTTPUpdate reboots from inside update(), and the fresh boot reports
// ota_done via checkBootDone(). On any failure the flag is cleared and the node
// restarts into its current firmware to recover the ESP-NOW link.
inline void doWifiUpdate(const char* ssid, const char* pass, const char* url) {
    otaDoneFlag = OTA_DONE_MAGIC;   // confirmed by checkBootDone() after reboot

    esp_now_deinit();                 // free the shared radio for dedicated STA use
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, pass);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(200);
    if (WiFi.status() != WL_CONNECTED) {
        otaDoneFlag = 0;
        ESP.restart();
    }

    WiFiClient client;
    httpUpdate.rebootOnUpdate(true);  // reboots on success; the armed flag => ota_done
    // The server sends an x-MD5 header, which HTTPUpdate verifies before booting.
    httpUpdate.update(client, url);

    // Reached only if the update failed (success rebooted inside update()).
    otaDoneFlag = 0;
    ESP.restart();
}

// Returns true if `data` was an ota_* command (and was handled), so the caller
// skips its normal command parsing. Returns false for everything else.
inline bool tryHandle(const uint8_t* data, int len) {
    // Cheap pre-filter so normal commands never pay for a JSON parse here.
    bool maybe = false;
    for (int i = 0; i + 3 < len; i++) {
        if (data[i] == 'o' && data[i + 1] == 't' &&
            data[i + 2] == 'a' && data[i + 3] == '_') { maybe = true; break; }
    }
    if (!maybe) return false;

    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) return false;
    const char* cmd = doc["cmd"] | "";
    if (strncmp(cmd, "ota_", 4) != 0) return false;

    if (strcmp(cmd, "ota_begin") == 0) {
        size_t      size = doc["size"] | 0;
        const char* md5  = doc["md5"]  | "";
        if (active) Update.abort();
        active = false; written = 0; expectedSeq = 0; total = size;
        if (size == 0 || !Update.begin(size)) { fail("begin_failed"); return true; }
        if (md5 && *md5) Update.setMD5(md5);
        active = true;
        reply("{\"type\":\"ota_ready\"}");
        return true;
    }

    if (strcmp(cmd, "ota_data") == 0) {
        if (!active) { fail("not_active"); return true; }
        uint32_t seq = doc["seq"] | 0;
        // Tolerate a sliding window: re-ACK anything we already have (duplicate /
        // reorder) so the PC can advance, and silently drop chunks from the
        // future — the PC retransmits the one we actually need on timeout. Only
        // the exactly-expected chunk is written.
        if (seq < expectedSeq) { replyAck(seq); return true; }
        if (seq > expectedSeq) { return true; }
        const char* b64 = doc["data"] | "";
        static uint8_t chunk[256];
        int n = b64decode(b64, strlen(b64), chunk, sizeof(chunk));
        if (n < 0)                                  { fail("bad_data");    return true; }
        if (Update.write(chunk, n) != (size_t)n)    { fail("write_failed"); return true; }
        written += n;
        expectedSeq++;
        replyAck(seq);
        return true;
    }

    if (strcmp(cmd, "ota_wifi") == 0) {
        const char* ssid = doc["ssid"] | "";
        const char* pass = doc["pass"] | "";
        const char* url  = doc["url"]  | "";
        if (!*ssid || !*url) { fail("bad_wifi_args"); return true; }
        strlcpy(wifiSsid, ssid, sizeof(wifiSsid));
        strlcpy(wifiPass, pass, sizeof(wifiPass));
        strlcpy(wifiUrl,  url,  sizeof(wifiUrl));
        // Ack while still on ESP-NOW so the PC knows the node accepted and is
        // going off the air; the actual connect + download runs from pollWifi().
        reply("{\"type\":\"ota_wifi_start\"}");
        wifiPending = true;
        return true;
    }

    if (strcmp(cmd, "ota_end") == 0) {
        if (!active) { fail("not_active"); return true; }
        bool ok = Update.end(true);   // true => verify MD5 + set boot partition
        active = false;
        if (!ok) { fail("verify_failed"); return true; }
        // Do NOT confirm success here: Update.end() only proves the image was
        // written and MD5-verified, not that it actually boots. Arm the done flag
        // and reboot; the freshly-booted *new* firmware broadcasts ota_done from
        // checkBootDone(). A merged/wrong-type image that passes MD5 but bricks
        // never reaches that boot, so the PC times out instead of seeing a false
        // success. (Same scheme as the WiFi path.)
        otaDoneFlag = OTA_DONE_MAGIC;
        ESP.restart();
        return true;
    }

    return true;  // unknown ota_* command — consume it anyway
}

// Call from loop(): if a WiFi OTA was requested (ota_wifi), run it now from the
// main task — safe to block and tear down ESP-NOW here, unlike the recv
// callback. Does not return on a successful update (the node reboots). A no-op
// when nothing is pending, so it is cheap to call every loop.
inline void pollWifi() {
    if (!wifiPending) return;
    wifiPending = false;
    delay(60);                        // let the ota_wifi_start ack leave first
    if (beforeWifiHook) beforeWifiHook();   // node-specific safe shutdown
    doWifiUpdate(wifiSsid, wifiPass, wifiUrl);
}

// Call once from setup() after se::begin(): if an OTA just completed (either the
// WiFi or the ESP-NOW path), the flag armed before the reboot survived in RTC
// memory — announce success to the PC and clear it. This is the *only* ota_done:
// it fires from the new firmware, so it proves the image booted, not merely that
// it verified. Broadcast (not toGateway) because a fresh boot has not learned the
// gateway MAC yet; the gateway forwards broadcasts to the PC all the same. A cold
// boot leaves the flag uninitialised, so a spurious match is ~1 in 2^32 and
// harmless (the PC only listens for ota_done during an update).
inline void checkBootDone() {
    if (otaDoneFlag != OTA_DONE_MAGIC) return;
    otaDoneFlag = 0;
    for (int i = 0; i < 4; i++) {
        se::broadcast("{\"type\":\"ota_done\"}");
        delay(100);
    }
}

}  // namespace ota
}  // namespace se

#endif  // ARDUINO

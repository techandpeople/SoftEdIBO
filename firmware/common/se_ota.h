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
 *   node -> PC  {"type":"ota_done"} then ESP.restart()  | ota_error verify_failed
 *
 * Chunks are written inline in the ESP-NOW recv callback (WiFi task). A 144-byte
 * flash write is a few ms — acceptable, and far simpler than buffering through a
 * queue. Integrity is verified by Update via the MD5 supplied in ota_begin.
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
#include <ArduinoJson.h>
#include <cstring>

#include "se_espnow.h"

namespace se {
namespace ota {

inline bool     active      = false;   // an image is being received
inline uint32_t expectedSeq = 0;       // next chunk index we expect
inline size_t   total       = 0;       // image size from ota_begin
inline size_t   written     = 0;       // bytes flashed so far

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

    if (strcmp(cmd, "ota_end") == 0) {
        if (!active) { fail("not_active"); return true; }
        bool ok = Update.end(true);   // true => verify MD5 + set boot partition
        active = false;
        if (!ok) { fail("verify_failed"); return true; }
        reply("{\"type\":\"ota_done\"}");
        delay(100);                   // let the reply leave before we reboot
        ESP.restart();
        return true;
    }

    return true;  // unknown ota_* command — consume it anyway
}

}  // namespace ota
}  // namespace se

#endif  // ARDUINO

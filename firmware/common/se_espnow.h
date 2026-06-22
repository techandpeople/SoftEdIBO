#pragma once
/**
 * SoftEdIBO — shared ESP-NOW link layer.
 *
 * Single source of truth for the ESP-NOW / MAC / radio plumbing used by every
 * firmware in this repo. Compiles under BOTH frameworks:
 *   - Arduino  (node_direct, node_multiplexed)
 *   - ESP-IDF  (gateway, e.g. Seeed XIAO ESP32-C6)
 *
 * The only framework-specific part is radio bring-up, guarded by ARDUINO.
 * Everything else is plain ESP-IDF API (esp_now_*), which Arduino re-exports,
 * so changing ESP-NOW behaviour means editing this file once.
 *
 * The ESP-NOW callback signatures changed across IDF versions; both are
 * normalised here to a single uniform form (see RecvFn / _onRecv / _onSent).
 */

#include <cstdint>
#include <cstring>
#include <cstdio>

#include "esp_now.h"
#include "esp_idf_version.h"

#ifdef ARDUINO
  #include <WiFi.h>
#else
  #include "nvs_flash.h"
  #include "esp_netif.h"
  #include "esp_event.h"
  #include "esp_wifi.h"
#endif

namespace se {

// Uniform receive callback the firmwares implement — the SDK-specific first
// argument is hidden behind the shim below.
using RecvFn = void (*)(const uint8_t mac[6], const uint8_t* data, int len);

inline RecvFn   _userRecv = nullptr;
inline uint32_t txOk      = 0;   // ESP-NOW sends that got a link-layer ACK
inline uint32_t txFail    = 0;   // ESP-NOW sends that failed

static constexpr uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---------------------------------------------------------------------------
// MAC helpers
// ---------------------------------------------------------------------------

inline void formatMac(const uint8_t* mac, char* buf /* >=18 */) {
    snprintf(buf, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

inline bool parseMac(const char* s, uint8_t* out /* [6] */) {
    return sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
                  &out[0], &out[1], &out[2], &out[3], &out[4], &out[5]) == 6;
}

// ---------------------------------------------------------------------------
// Callback signature shims
//
// recv: gained an esp_now_recv_info_t* first arg in IDF 5.0 (Arduino core 3.x).
// send: first arg became wifi_tx_info_t* in IDF 5.4.
// Both are normalised to the classic (mac, ...) form.
// ---------------------------------------------------------------------------

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
inline void _onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
    if (_userRecv) _userRecv(info->src_addr, data, len);
}
#else
inline void _onRecv(const uint8_t* mac, const uint8_t* data, int len) {
    if (_userRecv) _userRecv(mac, data, len);
}
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)
inline void _onSent(const wifi_tx_info_t*, esp_now_send_status_t status) {
#else
inline void _onSent(const uint8_t*, esp_now_send_status_t status) {
#endif
    if (status == ESP_NOW_SEND_SUCCESS) txOk++;
    else                                txFail++;
}

// ---------------------------------------------------------------------------
// Peer management
// ---------------------------------------------------------------------------

inline bool ensurePeer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return true;
    esp_now_peer_info_t peer{};
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = 0;            // follow the current WiFi channel
    peer.encrypt = false;
    peer.ifidx   = WIFI_IF_STA;  // required under pure IDF; harmless on Arduino
    return esp_now_add_peer(&peer) == ESP_OK;
}

// ---------------------------------------------------------------------------
// Radio bring-up — the only framework-specific code in this file
// ---------------------------------------------------------------------------

inline void radioInit() {
#ifdef ARDUINO
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
#else
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    esp_netif_init();
    esp_event_loop_create_default();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();
#endif
}

// ---------------------------------------------------------------------------
// One-call setup: radio + esp_now_init + recv/send callbacks + broadcast peer.
// Returns false if esp_now_init fails.
// ---------------------------------------------------------------------------

inline bool begin(RecvFn onRecv) {
    radioInit();
    if (esp_now_init() != ESP_OK) return false;
    _userRecv = onRecv;
    esp_now_register_recv_cb(_onRecv);
    esp_now_register_send_cb(_onSent);
    ensurePeer(BROADCAST);
    return true;
}

// ---------------------------------------------------------------------------
// Send helpers
// ---------------------------------------------------------------------------

inline void send(const uint8_t* mac, const uint8_t* data, size_t len) {
    esp_now_send(mac, data, len);
}
inline void send(const uint8_t* mac, const char* s) {
    esp_now_send(mac, reinterpret_cast<const uint8_t*>(s), strlen(s));
}
inline void broadcast(const char* s) { send(BROADCAST, s); }

// Own STA MAC as a string (>=18 byte buffer).
inline void ownMac(char* buf) {
    uint8_t mac[6];
#ifdef ARDUINO
    WiFi.macAddress(mac);
#else
    esp_wifi_get_mac(WIFI_IF_STA, mac);
#endif
    formatMac(mac, buf);
}

// ---------------------------------------------------------------------------
// Node-side helper: track the gateway MAC learned from the first message.
// (Unused by the gateway firmware itself.)
// ---------------------------------------------------------------------------

namespace node {

inline uint8_t gatewayMac[6] = {};
inline bool    gatewayKnown  = false;

// Call from the recv callback: on the first message received, remember the
// sender as the gateway and register it as a peer so the node can reply.
inline void learnGateway(const uint8_t* mac) {
    if (gatewayKnown) return;
    memcpy(gatewayMac, mac, 6);
    gatewayKnown = true;
    se::ensurePeer(gatewayMac);
}

inline void toGateway(const uint8_t* data, size_t len) {
    if (gatewayKnown) esp_now_send(gatewayMac, data, len);
}
inline void toGateway(const char* s) {
    if (gatewayKnown) se::send(gatewayMac, s);
}

}  // namespace node

}  // namespace se

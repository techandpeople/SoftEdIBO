/*
 * SoftEdIBO — Thymio RCP (XIAO ESP32-C6): dual-transport link + WiFi-OTA.
 *
 * Reads command lines from BOTH the USB-Serial/JTAG port (Serial) and the
 * inter-board UART (Serial1), and replies on the SAME channel — so one binary
 * works flashed-solo-on-USB and wired to the S3 host over TX/RX.
 *
 * Commands (JSON, one per line; the S3 gateway forwards {"target":"thymio",...}
 * here minus "target"):
 *   {"cmd":"ping"}                              -> {"type":"pong","src":"c6"}
 *   {"cmd":"ota_wifi","ssid":..,"pass":..,"url":..}
 *        -> joins the S3's SoftAP, HTTP-pulls the image (its /fw), self-updates
 *           via esp_ota, and reboots into the new firmware. This is why the C6
 *           never needs its USB again once this build is flashed once: all future
 *           updates come over WiFi from the S3. Needs an OTA partition table
 *           (board_build.partitions = default.csv). See docs/THYMIO_WIRELESS_CONTROL.md.
 *   {"cmd":"sniff_start"[,"ch":N]}  -> bring up the 802.15.4 radio in promiscuous
 *           mode and stream every raw frame as {"type":"frame",...} JSON over the
 *           UART (the S3 relays them to the PC). ch omitted -> hop 11..26; else lock.
 *           This is the Phase-1 Thymio-protocol capture (docs/THYMIO_WIRELESS_CONTROL.md)
 *           done on the *boxed* C6: no separate sniffer firmware, so we never lose the
 *           WiFi-OTA path. Never armed at boot -> a radio hiccup is always recoverable
 *           by a reboot (plain RCP again).
 *   {"cmd":"sniff_ch","n":N}        -> lock onto channel N (0 = resume hopping)
 *   {"cmd":"sniff_stop"}            -> radio off, back to a plain RCP
 *   {"cmd":"tx","ch":N,"data":"<hex>"} -> transmit a raw 802.15.4 frame on channel N
 *           (Phase 1: replay/forge a Thymio SET_VARIABLES motor command). Pass the PSDU
 *           WITHOUT its 2-byte FCS — the radio appends it. See the protocol notes below:
 *           motor.left.target=0x56, motor.right.target=0x57, msg type SET_VARIABLES=0xA00C.
 * A bare "PING <n>" line (not JSON) still gets "PONG <n>" for the bring-up link test.
 *
 * Wiring (4 wires): C6 D6/TX(16)->S3 D7/RX(44); C6 D7/RX(17)<-S3 D6/TX(43);
 *   GND-GND; C6 5V<-S3 5V.  Build/flash: pio run -e rcp_c6 -t upload.
 */
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include "esp_ota_ops.h"
#include "esp_ieee802154.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static constexpr int      LINK_TX   = 16;      // D6 on the XIAO ESP32-C6
static constexpr int      LINK_RX   = 17;      // D7
static constexpr uint32_t LINK_BAUD = 115200;
static constexpr uint32_t WIFI_TIMEOUT_MS = 15000;
static constexpr int      OTA_ATTEMPTS    = 4;   // retries — the shared-radio AP can
                                                 // drop a sustained bulk download

// Join the S3's SoftAP and pull the staged image from its /fw. httpUpdate reboots
// into the new firmware on success, so this only returns on failure.
static void doWifiOta(const char* ssid, const char* pass, const char* url, Print& io) {
    io.println("{\"type\":\"ota_wifi_start\",\"src\":\"c6\"}");
    WiFi.persistent(false);                     // don't rewrite creds to NVS each begin
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                        // no modem sleep — steadier bulk download
    WiFi.begin(ssid, pass);
    const uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_MS) delay(200);
    if (WiFi.status() != WL_CONNECTED) {
        io.println("{\"type\":\"ota_wifi_fail\",\"src\":\"c6\",\"reason\":\"wifi\"}");
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
        return;
    }

    // Retry the pull (OTA_ATTEMPTS): the AP link (shared radio, contended by
    // ESP-NOW) can drop a sustained download. A successful attempt never returns
    // (we reboot into the new image), so the loop only runs on failure — except an
    // ACTIVATE failure, which is not a transport drop and is handled below instead.
    // Download + write + verify via HTTPUpdate, but activate ourselves so we can
    // report the EXACT esp_err (HTTPUpdate only surfaces a generic "err 9").
    httpUpdate.rebootOnUpdate(false);
    WiFiClient client;
    int uerr = 0;
    for (int attempt = 1; attempt <= OTA_ATTEMPTS; ++attempt) {
        t_httpUpdate_return r = httpUpdate.update(client, url);
        if (r == HTTP_UPDATE_OK) {                    // written, verified AND activated
            io.println("{\"type\":\"ota_wifi_ok\",\"src\":\"c6\"}");
            delay(200);
            esp_restart();
        }
        uerr = httpUpdate.getLastError();
        if (uerr == UPDATE_ERROR_ACTIVATE || attempt == OTA_ATTEMPTS) break;
        io.printf("{\"type\":\"ota_wifi_retry\",\"src\":\"c6\",\"attempt\":%d,\"err\":%d}\n",
                  attempt, uerr);
        delay(1000);
    }
    if (uerr == UPDATE_ERROR_ACTIVATE) {
        // Image is written+verified in the inactive slot; only the boot-partition
        // switch failed. Re-do it here to get the real reason + running-slot state.
        const esp_partition_t* run  = esp_ota_get_running_partition();
        const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
        esp_ota_img_states_t rst = (esp_ota_img_states_t)0xFF;
        if (run) esp_ota_get_state_partition(run, &rst);
        esp_err_t e = next ? esp_ota_set_boot_partition(next) : ESP_ERR_NOT_FOUND;
        io.printf("{\"type\":\"ota_activate\",\"src\":\"c6\",\"esp_err\":\"%s\","
                  "\"run\":\"%s\",\"run_state\":%d,\"next\":\"%s\"}\n",
                  esp_err_to_name(e), run ? run->label : "?", (int)rst,
                  next ? next->label : "?");
        if (e == ESP_OK) {                            // worked on the retry → boot it
            io.println("{\"type\":\"ota_wifi_ok\",\"src\":\"c6\"}");
            delay(200);
            esp_restart();
        }
    }
    io.printf("{\"type\":\"ota_wifi_fail\",\"src\":\"c6\",\"reason\":\"http\",\"err\":%d}\n", uerr);
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
}

// ---- 802.15.4 promiscuous sniffer (Phase 1: capture the Thymio RF protocol) ----
// Frames are streamed as JSON over Serial1 so the S3 relays them to the PC. 127 is
// the full 802.15.4 PSDU, so nothing is ever cut; the worst-case line (~320 chars)
// needs the S3's 384-char relay buffer (gateways on the old 256-char build truncate
// near-max frames — reflash the S3 too). Never armed at boot — a radio hiccup is
// always recoverable by a reboot.
static constexpr uint8_t  SNIFF_MAX_BYTES = 127;
static constexpr uint32_t SNIFF_HOP_MS    = 1500;   // dwell per channel when hopping

struct SniffFrame {
    uint8_t len;
    uint8_t data[128];
    int8_t  rssi;
    uint8_t lqi;
    uint8_t channel;
};
// One RX queue shared by the sniffer AND the link-mode sensor reader (they never
// run at once — the poller pauses while sniffing). The ISR fills it; whichever pump
// is active drains it.
static QueueHandle_t s_rxQ      = nullptr;
static volatile bool s_sniffing = false;
static uint8_t       s_sniffCh    = 11;   // current channel
static uint8_t       s_sniffFixed = 0;    // 0 = hop 11..26, else locked channel

// The 802.15.4 radio is enabled ONCE per boot and then left up, for two reasons:
// (1) esp_ieee802154_enable() allocates the ZB_MAC interrupt; calling it again without
// a matching disable() leaks that allocation until "No free interrupt inputs for
// ZB_MAC" — after which the radio silently stops transmitting even though
// esp_ieee802154_transmit() still returns ESP_OK. So gate it.
// (2) esp_ieee802154_disable() poisons the modem for the REST OF THE BOOT: a later
// re-enable comes up deaf (only-first-discover-works) and a later WiFi STA join
// times out (OtaPark). So radioDown() must only run on paths that end in a reboot.
static bool s_radioEnabled = false;
static bool s_radioUsed    = false;   // any 15.4 enable() this boot (poisons WiFi join)
static void radioUp()   { if (!s_radioEnabled) { esp_ieee802154_enable();  s_radioEnabled = true;
                                                 s_radioUsed = true; } }
static void radioDown() { if (s_radioEnabled)  { esp_ieee802154_disable(); s_radioEnabled = false; } }

// ---- WiFi-OTA parking (radio-poisoned boots) ----
// esp_ieee802154_disable() does NOT return the C6's shared modem to a WiFi-usable
// state: once the 802.15.4 radio has been up in a boot, a later WiFi STA join times
// out (seen live: every ota_wifi that followed link/sniff/discovery use failed with
// reason "wifi"; every fresh-boot attempt joined fine). So when the radio has been
// used, ota_wifi parks the request here and reboots; setup() resumes it on the clean
// boot BEFORE anything touches the 15.4 radio. Same RTC_NOINIT pattern as the nodes'
// se_ota.h otaDoneFlag (survives esp_restart, garbage after power-on — hence magic).
struct OtaPark {
    uint32_t magic;
    char     ssid[33];
    char     pass[65];
    char     url[128];
    bool     viaUsb;                  // which transport to report on after the reboot
};
static RTC_NOINIT_ATTR OtaPark s_otaPark;
static constexpr uint32_t OTA_PARK_MAGIC = 0x4F544150;   // "OTAP"

// Set by the driver when a transmit (incl. its ACK wait) completes — lets thTx() pace
// back-to-back frames instead of bursting them into a busy radio (which drops them).
// The semaphore mirrors the flag so the pacing wait can BLOCK (yield the single core
// to the UART writer task) instead of busy-spinning up to 15 ms per frame.
static volatile bool s_txDone = true;
static SemaphoreHandle_t s_txDoneSem = nullptr;

// Call immediately before esp_ieee802154_transmit: re-arm the done latch and drain a
// stale semaphore give left by a prior transmit that completed after its txWait.
static inline void txBegin() {
    s_txDone = false;
    if (s_txDoneSem) xSemaphoreTake(s_txDoneSem, 0);
}

// Pace: wait (blocking, yields) until the transmit + its ACK are done, capped at `ms`.
static volatile uint32_t s_txTimeouts = 0;   // TXs whose ACK never came in `ms` — climbs
                                             // if the Thymio stops ACKing (radio degrading)
static void txWait(uint32_t ms) {
    if (s_txDoneSem) {
        if (!s_txDone && xSemaphoreTake(s_txDoneSem, pdMS_TO_TICKS(ms)) != pdTRUE)
            s_txTimeouts = s_txTimeouts + 1;     // timed out: no tx-done/ACK within ms
        return;
    }
    uint32_t t0 = millis();                      // pre-setup fallback: original busy-wait
    while (!s_txDone && millis() - t0 < ms) delayMicroseconds(100);
    if (!s_txDone) s_txTimeouts = s_txTimeouts + 1;
}

// Driver ISR callback: copy the frame out and hand it to loop() via the queue.
void IRAM_ATTR esp_ieee802154_receive_done(uint8_t* frame,
                                           esp_ieee802154_frame_info_t* info) {
    if (!s_rxQ) return;
    SniffFrame f;
    uint8_t len = frame[0];
    if (len > sizeof(f.data)) len = sizeof(f.data);
    f.len = len; f.rssi = info->rssi; f.lqi = info->lqi; f.channel = info->channel;
    memcpy(f.data, &frame[1], len);
    BaseType_t hp = pdFALSE;
    xQueueSendFromISR(s_rxQ, &f, &hp);
    if (hp) portYIELD_FROM_ISR();
}

// TX-done / TX-failed driver callbacks: release the pacing wait in txWait().
void esp_ieee802154_transmit_done(const uint8_t* frame, const uint8_t* ack,
                                  esp_ieee802154_frame_info_t* ack_frame_info) {
    s_txDone = true;
    BaseType_t hp = pdFALSE;
    if (s_txDoneSem) xSemaphoreGiveFromISR(s_txDoneSem, &hp);
    if (hp) portYIELD_FROM_ISR();
}
void esp_ieee802154_transmit_failed(const uint8_t* frame, esp_ieee802154_tx_error_t error) {
    s_txDone = true;
    BaseType_t hp = pdFALSE;
    if (s_txDoneSem) xSemaphoreGiveFromISR(s_txDoneSem, &hp);
    if (hp) portYIELD_FROM_ISR();
}

static void sniffTune(uint8_t channel) {
    esp_ieee802154_set_channel(channel);
    esp_ieee802154_receive();               // re-arm RX on the (new) channel
}

static void sniffStart(uint8_t fixed_ch, Print& io) {
    if (!s_rxQ) s_rxQ = xQueueCreate(24, sizeof(SniffFrame));
    s_sniffFixed = fixed_ch;
    s_sniffCh    = fixed_ch ? fixed_ch : 11;
    radioUp();
    delay(50);                       // let the radio settle — tuning RX too soon after
                                     // enable() can come up un-armed (a fixed channel
                                     // then never receives; sniffPump re-arms as backup)
    esp_ieee802154_set_promiscuous(true);
    esp_ieee802154_set_rx_when_idle(true);
    sniffTune(s_sniffCh);
    s_sniffing = true;
    io.printf("{\"type\":\"sniff\",\"src\":\"c6\",\"state\":\"start\",\"ch\":%u,\"fixed\":%u}\n",
              s_sniffCh, s_sniffFixed);
}

static void sniffStop(Print& io) {
    if (!s_sniffing) return;
    s_sniffing = false;
    // The radio STAYS up: esp_ieee802154_disable() poisons the modem for the rest
    // of the boot — the next enable comes up deaf (only-first-scan-works) and a
    // WiFi join fails outright (see OtaPark). Down only on reboot-bound paths.
    io.println("{\"type\":\"sniff\",\"src\":\"c6\",\"state\":\"stop\"}");
}

// Drain queued frames to Serial1 as JSON, and hop channels when not locked.
static void sniffPump() {
    if (!s_sniffing || !s_rxQ) return;
    SniffFrame f;
    bool got = false;
    while (xQueueReceive(s_rxQ, &f, 0) == pdTRUE) {
        got = true;
        uint8_t n = f.len < SNIFF_MAX_BYTES ? f.len : SNIFF_MAX_BYTES;
        static const char H[] = "0123456789ABCDEF";
        char hex[SNIFF_MAX_BYTES * 2 + 1];
        for (uint8_t i = 0; i < n; i++) {
            hex[i * 2]     = H[f.data[i] >> 4];
            hex[i * 2 + 1] = H[f.data[i] & 0x0F];
        }
        hex[n * 2] = '\0';
        char line[288];
        snprintf(line, sizeof(line),
                 "{\"type\":\"frame\",\"ch\":%u,\"rssi\":%d,\"lqi\":%u,\"len\":%u,"
                 "\"data\":\"%s\"}", f.channel, f.rssi, f.lqi, f.len, hex);
        Serial1.println(line);   // to the S3 (boxed C6, gateway relays to the PC)
        Serial.println(line);    // to USB (a spare C6 flashed solo, read directly)
    }
    // Keep RX continuously armed. The driver goes idle after a few frames; re-issuing
    // receive() (WITHOUT set_channel, which resets the PLL and starves RX) keeps it
    // listening. Hop mode retunes on its own timer.
    static uint32_t last_hop = 0, last_rearm = 0;
    uint32_t now = millis();
    if (!s_sniffFixed) {
        if (now - last_hop >= SNIFF_HOP_MS) {
            last_hop = now;
            s_sniffCh = (s_sniffCh >= 26) ? 11 : s_sniffCh + 1;
            sniffTune(s_sniffCh);
        }
    } else if (got || now - last_rearm >= 100) {
        last_rearm = now;
        esp_ieee802154_receive();
    }
}

// Parse a hex string (spaces ignored) into bytes; returns the count written.
static size_t hexToBytes(const char* s, uint8_t* out, size_t cap) {
    size_t n = 0;
    int hi = -1;
    for (; *s && n < cap; ++s) {
        char c = *s;
        int v;
        if (c >= '0' && c <= '9')      v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else continue;                       // skip spaces / punctuation
        if (hi < 0) { hi = v; }
        else { out[n++] = (uint8_t)((hi << 4) | v); hi = -1; }
    }
    return n;
}

// Transmit a raw 802.15.4 frame (Phase 1: replay/forge Thymio SET_VARIABLES commands).
// Pass the PSDU WITHOUT the 2-byte FCS — the radio computes and appends it. frame[0] is
// the PHR length (PSDU incl. FCS). Untested on hardware; the radio TX path is new here.
static void doTx(uint8_t ch, const char* hex, Print& io) {
    static uint8_t frame[130];
    size_t n = hexToBytes(hex, &frame[1], sizeof(frame) - 1);
    if (n == 0 || ch < 11 || ch > 26) {
        io.println("{\"type\":\"tx\",\"src\":\"c6\",\"err\":\"bad args\"}");
        return;
    }
    radioUp();                               // bring the radio up ONCE (never re-enable per
                                             // tx — that leaks the ZB_MAC interrupt)
    esp_ieee802154_set_channel(ch);
    frame[0] = (uint8_t)(n + 2);             // PHR = payload + FCS
    esp_err_t e = esp_ieee802154_transmit(frame, false);
    io.printf("{\"type\":\"tx\",\"src\":\"c6\",\"len\":%u,\"ch\":%u,\"err\":\"%s\"}\n",
              (unsigned)n, ch, esp_err_to_name(e));
    esp_ieee802154_receive();                // re-arm RX (radio stays up)
}

// ---- Continuous Thymio link (the C6 IS the dongle) ----------------------------------
// The Thymio only keeps its receive window open while it is actively polled (~the dongle's
// 10 Hz GET_VARIABLES). Driving it from the PC over serial is laggy (each command reopens
// the port → resets the C6 → seconds of dead time) and only holds the link while a script
// runs. So the poll loop lives HERE: once thymio_link is on, loop() transmits the poll +
// the held motor/LED targets every TH_POLL_MS with an incrementing seq. The PC just sets
// the targets ({"cmd":"thymio_drive",...}) — instant, and the link stays hot forever.
static const uint8_t  TH_PAN[2] = {0x81, 0x44};   // PAN 0x4481 (this dongle network)
static const uint16_t TH_SET_VARIABLES = 0xA00C;
static const uint16_t TH_GET_VARIABLES = 0xA00B;
static const uint16_t TH_SET_BYTECODE  = 0xA001;   // load a program (payload [dest][addr][words])
static const uint16_t TH_RUN           = 0xA003;   // run it (payload [dest]); NOT 0xA002 = RESET
static const uint16_t TH_VARIABLES     = 0x9005;   // node→host reply to GET_VARIABLES
static const uint16_t TH_HOST_ADDR     = 0x3237;   // our spoofed host short address
// Poll one contiguous window that fits a single 802.15.4 frame (38 words = 76 B):
//   prox.ground.delta @0x54,0x55  → value[0..1]   (table reflection; ~0 when lifted)
//   acc               @0x62..0x64 → value[14..16] (impact = deviation from rest 0,0,20)
//   mic.intensity     @0x79       → value[37]      (ambient loudness)
static const uint16_t TH_SENSE_START   = 0x0054;   // prox.ground.delta @0x54
static const uint8_t  TH_SENSE_COUNT   = 17;        // 0x54..0x64: ground.delta + acc (no mic)
static const uint32_t TH_SENSE_EVERY   = 3;         // sensor GET only every Nth poll (~3 Hz). PROVEN:
                                                    // a GET at the full 10 Hz overruns the Thymio's
                                                    // CPU and the MOTORS lag — regardless of reply
                                                    // size (even a 3-value GET at 10 Hz lagged). So
                                                    // motors win; sensors get ~3 Hz (best when idle).
static const uint8_t  TH_OFF_GROUND    = 0;         // value index of prox.ground.delta[0]
static const uint8_t  TH_OFF_ACC       = 14;        // value index of acc x (0x62 - 0x54)
static const uint32_t TH_POLL_MS = 100;           // ~10 Hz per Thymio — holds the RX window
                                                  // open (dongle-like; cooler + LED blinks)
#define TH_MAX 4                                   // up to 4 Thymios on this one C6

// One Thymio, addressed by its 802.15.4 short address (e.g. 0x6a25). Everything per-robot
// derives from it: the MAC destination is the address little-endian, the Aseba node id is
// the same bytes big-endian (verified against captures). PAN, the host src (0x3237) and the
// RF-module wrapper are shared. Capture each robot's address by sniffing it (the frame's
// bytes after PAN 8144); slot 0 defaults to the first Thymio we decoded (0x6a25).
struct ThymioSlot {
    bool     active     = false;
    uint16_t addr       = 0x6A25;
    int16_t  left       = 0, right = 0;
    int16_t  leds[3]    = {0, 0, 0};
    uint8_t  ledsResend = 0;
};
static ThymioSlot s_th[TH_MAX];
static bool       s_thLink = false;
static uint8_t    s_thCh   = 25;
static uint8_t    s_thSeq  = 0;
static uint32_t   s_thLastPoll = 0;
static uint32_t   s_thPollCount = 0;   // polls actually run — rcp_health reports the real
                                       // poll Hz (the Thymio's RF-LED blink rate, in numbers)
static uint32_t   s_thRxCount   = 0;   // full sensor replies parsed — rcp_health's rx_hz. If
                                       // it falls below the ~3 Hz we ask for, the THYMIO isn't
                                       // keeping up (its CPU is the bottleneck, not ours)
static bool       s_thRxDebug = false;   // stream raw RX frames (link bring-up only)
static bool       s_thDiscover = false;  // active discovery: broadcast LIST_NODES, report replies
static uint8_t    s_thDiscCh   = 25;
static uint32_t   s_thDiscLast = 0;

// Build + transmit one Aseba-over-802.15.4 frame for Thymio `addr` (radio up + on ch).
// `payload` is the Aseba message body AFTER the msgType and the dest-node word (which
// every message here carries): [startAddr, values…] for SET/GET_VARIABLES and
// SET_BYTECODE, or empty for RUN.
static void thSend(uint16_t addr, uint16_t msgType,
                   const int16_t* payload, uint8_t npayload) {
    static uint8_t f[96];                          // static: outlives the async transmit
    const uint8_t dstL = addr & 0xFF, dstH = addr >> 8;   // MAC dest = addr little-endian
    uint8_t* p = &f[1];                            // f[0] = PHR, filled last
    *p++ = 0x61; *p++ = 0x88; *p++ = s_thSeq++;    // FCF + incrementing seq
    *p++ = TH_PAN[0]; *p++ = TH_PAN[1];
    *p++ = dstL; *p++ = dstH;                      // dest = Thymio (little-endian)
    *p++ = 0x37; *p++ = 0x32;                      // src  = host 0x3237 (spoofed)
    *p++ = 0x83; *p++ = 0x00; *p++ = dstH; *p++ = dstL;   // wrapper: node = addr big-endian
    *p++ = 0x32; *p++ = 0x37; *p++ = 0x11;         // wrapper: host + tag
    *p++ = 0x01; *p++ = 0x00;                       // Aseba source (host)
    *p++ = msgType & 0xFF; *p++ = msgType >> 8;
    *p++ = dstH; *p++ = dstL;                       // Aseba dest node = addr big-endian
    for (uint8_t i = 0; i < npayload; i++) { *p++ = payload[i] & 0xFF; *p++ = (payload[i] >> 8) & 0xFF; }
    f[0] = (uint8_t)((p - &f[1]) + 2);             // PHR = PSDU + FCS
    txBegin();
    esp_ieee802154_transmit(f, false);
    txWait(15);                                    // pace: TX (+ACK) done before the next
}                                                  // frame — blocking wait, yields the core

// SET/GET_VARIABLES & SET_BYTECODE: body = [startAddr, values…].
static void thTx(uint16_t addr, uint16_t msgType, uint16_t startAddr,
                 const int16_t* vals, uint8_t nvals) {
    static int16_t body[48];
    body[0] = (int16_t)startAddr;
    if (nvals > 47) nvals = 47;
    for (uint8_t i = 0; i < nvals; i++) body[1 + i] = vals[i];
    thSend(addr, msgType, body, (uint8_t)(nvals + 1));
}

// RUN: body is just the dest node (already emitted by thSend), no startAddr.
static void thRun(uint16_t addr) { thSend(addr, TH_RUN, nullptr, 0); }

// Broadcast an Aseba LIST_NODES so every Thymio on the network answers with a
// NODE_PRESENT (0x900C) — how the dongle enumerates robots. This is what makes
// dongle-free discovery work: a powered-but-idle Thymio never announces itself,
// but it DOES reply to LIST_NODES. Frame bytes captured verbatim from a real RF
// dongle (docs/THYMIO_WIRELESS_CONTROL.md): a broadcast frame differs from our
// unicast in FCF (0x8841, no ACK-request), dst (0xFFFF), and the RF-module
// wrapper (0x82 tag with only the host node, no destination). The payload
// a0 01 00 05 00 = LIST_NODES(0xA011) with protocol version 5.
static void thDiscoverBroadcast() {
    static uint8_t f[24];                          // static: outlives the async transmit
    uint8_t* p = &f[1];                            // f[0] = PHR, filled last
    *p++ = 0x41; *p++ = 0x88; *p++ = s_thSeq++;    // FCF (broadcast, no ACK) + seq
    *p++ = TH_PAN[0]; *p++ = TH_PAN[1];            // PAN 0x4481
    *p++ = 0xFF; *p++ = 0xFF;                      // dst = broadcast
    *p++ = 0x37; *p++ = 0x32;                      // src = host 0x3237
    *p++ = 0x82; *p++ = 0x00; *p++ = 0x32; *p++ = 0x37; *p++ = 0x11;   // broadcast wrapper
    *p++ = 0xA0; *p++ = 0x01; *p++ = 0x00; *p++ = 0x05; *p++ = 0x00;   // LIST_NODES(proto 5)
    f[0] = (uint8_t)((p - &f[1]) + 2);             // PHR = PSDU + FCS
    txBegin();
    esp_ieee802154_transmit(f, false);
    txWait(15);                                    // pace: TX done before re-arming RX
}

// Sound: load a tiny Aseba program that calls a sound native function, then run it.
// Bytecode templates captured from thymiodirect on a real Thymio-II (word[4] and
// word[7] are the parameterised values). Loading + running our program leaves the
// motor.*.target variables untouched, so a driving robot keeps driving through a beep.
// See docs/THYMIO_WIRELESS_CONTROL.md / memory thymio-sensors-and-sound.
static void thPlaySystem(uint16_t addr, int16_t soundId) {   // soundId 0..7 (-1 stops)
    int16_t bc[] = {0x0003, (int16_t)0xffff, 0x0003, 0x2000, soundId, 0x4002,
                    0x2000, 0x0002, (int16_t)0xc026, 0x0000};   // callnat sound.system
    thTx(addr, TH_SET_BYTECODE, 0, bc, sizeof(bc) / sizeof(bc[0]));
    thRun(addr);
}
static void thPlayFreq(uint16_t addr, int16_t freqHz, int16_t dur60) {   // dur in 1/60 s
    int16_t bc[] = {0x0003, (int16_t)0xffff, 0x0003, 0x2000, freqHz, 0x4002,
                    0x2000, dur60, 0x4003, 0x2000, 0x0002, 0x2000, 0x0003,
                    (int16_t)0xc02b, 0x0000};                    // callnat sound.freq
    thTx(addr, TH_SET_BYTECODE, 0, bc, sizeof(bc) / sizeof(bc[0]));
    thRun(addr);
}
// Play a recorded track from the Thymio's microSD (sound.play(trackId) → /SD Pn.wav;
// needs a card inserted). One arg, so the bytecode mirrors thPlaySystem with the track.
// TODO(confirm via recon): sound.play's native-function INDEX on this firmware is unknown.
// Run scratchpad thymio_natives.py on the USB Thymio, read the index of "sound.play", and
// set TH_NF_SOUND_PLAY (callnat opcode = 0xC000 | index). Until then we refuse the command
// rather than callnat a wrong index (which can crash the Thymio VM). Ref: sound.system=0x26,
// sound.freq=0x2b on this firmware.
static const int16_t TH_NF_SOUND_PLAY = -1;   // -1 = not configured yet
static bool thPlayTrack(uint16_t addr, int16_t track) {
    if (TH_NF_SOUND_PLAY < 0) return false;
    int16_t bc[] = {0x0003, (int16_t)0xffff, 0x0003, 0x2000, track, 0x4002,
                    0x2000, 0x0002,
                    (int16_t)(0xC000 | (TH_NF_SOUND_PLAY & 0x01FF)), 0x0000};  // callnat sound.play
    thTx(addr, TH_SET_BYTECODE, 0, bc, sizeof(bc) / sizeof(bc[0]));
    thRun(addr);
    return true;
}

// Line-assembly buffer for the S3→C6 command UART (Serial1), at file scope so the Thymio
// poll can drain + dispatch a mid-poll command through the same buffer that loop() uses
// (a line split across the poll boundary still assembles correctly). See pumpThymioUart.
static char   s_uartBuf[256];
static size_t s_uartLen = 0;
static void   pumpThymioUart();   // defined just before loop(), after pump()/handleLine

// ---- Outbound line queue + writer task ----------------------------------------------
// High-rate telemetry (sensor frames, health reports) is NEVER written to the UART from
// the radio hot path. loop() enqueues complete JSON lines here; a small higher-priority
// writer task dequeues and does the (possibly blocking) serial writes. If the S3 stalls
// draining (PC backpressure), the WRITER blocks — and the scheduler hands the core back
// to loop(), so the 10 Hz Thymio poll stays rock-solid. This kills the failure mode
// where a blocking println in thymioRxPump froze the poll (Thymio RF LED at ~2 Hz,
// motors lagging ~10 s). Queue full → drop the OLDEST line (freshest sensor data wins);
// drops + worst write stall are reported in the periodic rcp_health frame.
struct OutLine { uint16_t len; char text[184]; };
static QueueHandle_t s_outQ = nullptr;
static volatile uint32_t s_outDrops       = 0;   // lines dropped because the queue was full
static volatile uint32_t s_uartBlockMaxMs = 0;   // worst single serial-write stall seen

static void qLine(const char* line) {
    if (!s_outQ) { Serial1.println(line); Serial.println(line); return; }   // pre-setup only
    OutLine ol;
    size_t n = strnlen(line, sizeof(ol.text) - 1);
    memcpy(ol.text, line, n);
    ol.text[n] = '\n';
    ol.len = (uint16_t)(n + 1);
    if (xQueueSend(s_outQ, &ol, 0) != pdTRUE) {
        OutLine junk;
        xQueueReceive(s_outQ, &junk, 0);           // drop the oldest…
        s_outDrops = s_outDrops + 1;
        xQueueSend(s_outQ, &ol, 0);                // …keep the newest
    }
}

static void outWriterTask(void*) {
    OutLine ol;
    for (;;) {
        if (xQueueReceive(s_outQ, &ol, portMAX_DELAY) != pdTRUE) continue;
        uint32_t t0 = millis();
        // Single write per line (newline included) so a concurrent command reply from
        // loop() can't interleave mid-line. Serial1 may block on a full TX ring — only
        // this task waits. USB Serial has tx-timeout 0 (drops when no host, never blocks).
        Serial1.write(reinterpret_cast<const uint8_t*>(ol.text), ol.len);
        Serial.write(reinterpret_cast<const uint8_t*>(ol.text), ol.len);
        uint32_t dt = millis() - t0;
        if (dt > s_uartBlockMaxMs) s_uartBlockMaxMs = dt;
    }
}

static void thymioLinkPump() {
    // The sniffer owns the radio while scanning (promiscuous / other channels):
    // polling through it would corrupt both. sniffStart zeroed our motors first.
    if (!s_thLink || s_sniffing) return;
    uint32_t now = millis();
    if (now - s_thLastPoll < TH_POLL_MS) return;
    s_thLastPoll = now;
    s_thPollCount++;
    for (int i = 0; i < TH_MAX; i++) {             // poll + assert every active Thymio
        ThymioSlot& t = s_th[i];
        if (!t.active) continue;
        // ORDER MATTERS on this half-duplex radio. Motors go FIRST — control is priority
        // and gets the lowest latency — and the sensor GET goes LAST, so the Thymio's
        // reply lands in the clean RX window right after the poll (esp_ieee802154_receive
        // below, then ~90 ms idle-RX until the next poll). The old order (GET first) had
        // the reply arrive WHILE the C6 was transmitting the motor frames — half-duplex,
        // so it couldn't hear it: that lost most sensor frames AND made the motors contend
        // with the reply. cd045bf (fast, no sensor read) is the reference this restores.
        int16_t lr[2] = {t.left, t.right};                   // motor.left+right in ONE frame,
        thTx(t.addr, TH_SET_VARIABLES, 0x0056, lr, 2);       // every poll (holds the RX window)
        if (t.ledsResend) { t.ledsResend--; thTx(t.addr, TH_SET_VARIABLES, 0x0065, t.leds, 3); }
        // Service a drive/LED command that landed on the UART. A command that flips the
        // radio (sniff / link-off / reboot) trips the guard → bail before the GET.
        pumpThymioUart();
        if (!s_thLink || s_sniffing) { esp_ieee802154_receive(); return; }
        // Sensor GET only every TH_SENSE_EVERY-th poll (~3 Hz): a GET at the full 10 Hz
        // overruns the Thymio's CPU and lags the motors (proven, any reply size). Motors
        // are the priority; sensors get ~3 Hz (reliable when idle, patchy under hard drive).
        if ((s_thPollCount % TH_SENSE_EVERY) == 0) {
            int16_t cnt = TH_SENSE_COUNT;
            thTx(t.addr, TH_GET_VARIABLES, TH_SENSE_START, &cnt, 1);
        }
    }
    esp_ieee802154_receive();                      // clean RX window for the sensor reply + ACKs
}

// Push one slot's held motor targets to its Thymio right now, off the poll cadence.
// An arriving thymio_drive would otherwise only reach the wheels at the next ~10 Hz
// poll — up to TH_POLL_MS of lag, worse if that single frame drops (weak antenna) and
// waits another poll to re-assert. Sending on the command edge makes a button press
// feel instant; the poll still re-asserts, so a dropped edge frame self-heals. No-op
// unless the link owns the radio (up + tuned to the Thymio's channel) and we're not
// mid-sniff — same guard as the poll, so we never TX on a down/retuned radio.
static void thymioAssertMotors(int idx) {
    if (!s_thLink || s_sniffing || idx < 0 || idx >= TH_MAX) return;
    ThymioSlot& t = s_th[idx];
    if (!t.active) return;
    int16_t lr[2] = {t.left, t.right};                    // both motors in ONE frame
    thTx(t.addr, TH_SET_VARIABLES, 0x0056, lr, 2);
    esp_ieee802154_receive();                             // re-arm RX after the burst
}

// Same immediate-assert for leds.top (the poll only re-sends while ledsResend > 0, and
// even the first send would otherwise wait for the next poll).
static void thymioAssertLeds(int idx) {
    if (!s_thLink || s_sniffing || idx < 0 || idx >= TH_MAX) return;
    ThymioSlot& t = s_th[idx];
    if (!t.active) return;
    thTx(t.addr, TH_SET_VARIABLES, 0x0065, t.leds, 3);    // leds.top
    esp_ieee802154_receive();
}

// Which slot a reply's Thymio short address (its MAC src) belongs to, else 0.
static int thSlotForAddr(uint16_t addr) {
    for (int i = 0; i < TH_MAX; i++)
        if (s_th[i].active && s_th[i].addr == addr) return i;
    return 0;
}

// Emit a received frame's hex (capped) for link bring-up: confirms the reply even
// arrives and lets us eyeball the Aseba offset. Off unless thymio_rx_debug asked.
static void thEmitRaw(const SniffFrame& f) {
    static const char H[] = "0123456789ABCDEF";
    uint8_t n = f.len < SNIFF_MAX_BYTES ? f.len : SNIFF_MAX_BYTES;
    char hex[SNIFF_MAX_BYTES * 2 + 1];
    for (uint8_t i = 0; i < n; i++) { hex[i * 2] = H[f.data[i] >> 4]; hex[i * 2 + 1] = H[f.data[i] & 0x0F]; }
    hex[n * 2] = '\0';
    char line[288];
    snprintf(line, sizeof(line),
             "{\"type\":\"thymio_rx\",\"rssi\":%d,\"len\":%u,\"data\":\"%s\"}",
             f.rssi, f.len, hex);
    Serial1.println(line);
    Serial.println(line);
}

// Drain received frames in link mode and turn each VARIABLES reply into a
// {"type":"thymio_sensors",...}. The Thymio answers our GET_VARIABLES poll with a
// VARIABLES (0x9005) frame addressed to the host (0x3237); its Aseba body is
// [start_addr][values…]. We poll from 0x62 (acc x/y/z) through 0x79 (mic.intensity),
// so values[0..2] = acc and values[23] = mic. The RF-module wrapper offset isn't
// fixed, so we locate the Aseba message by scanning for its msgType word (05 90 LE)
// rather than a hardcoded offset. Promiscuous RX (set on link-on) lets us hear the
// reply even though the C6 never claimed the host address. See memory
// thymio-sensors-and-sound. NOTE: unverified on hardware — thymio_rx_debug streams
// the raw frames so we can confirm the reply arrives and the parse is right.
static void thymioRxPump() {
    if (!s_thLink || s_sniffing || !s_rxQ) return;
    SniffFrame f;
    while (xQueueReceive(s_rxQ, &f, 0) == pdTRUE) {
        if (s_thRxDebug) thEmitRaw(f);
        if (f.len < 9) continue;                          // no addressing (e.g. an ACK)
        uint16_t pan = f.data[3] | (f.data[4] << 8);
        uint16_t dst = f.data[5] | (f.data[6] << 8);
        uint16_t src = f.data[7] | (f.data[8] << 8);
        if (pan != 0x4481 || dst != TH_HOST_ADDR) continue;   // only replies addressed to us
        int p = -1;                                       // find the VARIABLES msgType (05 90)
        for (int i = 9; i + 3 < f.len; i++)
            if (f.data[i] == (TH_VARIABLES & 0xFF) && f.data[i + 1] == (TH_VARIABLES >> 8)) { p = i; break; }
        if (p < 0) continue;
        uint16_t start = f.data[p + 2] | (f.data[p + 3] << 8);
        if (start != TH_SENSE_START) continue;            // not our sensor poll reply
        int base = p + 4;                                 // first value word
        if (base + 2 * TH_SENSE_COUNT > f.len) continue;  // reply truncated
        s_thRxCount++;                                    // a full sensor reply arrived (rx_hz)
        auto rd = [&](int w) -> int16_t {
            return (int16_t)(f.data[base + 2 * w] | (f.data[base + 2 * w + 1] << 8));
        };
        char line[160];
        snprintf(line, sizeof(line),
                 "{\"type\":\"thymio_sensors\",\"idx\":%d,\"acc\":[%d,%d,%d],"
                 "\"mic\":0,\"ground\":[%d,%d]}",   // mic dropped (window trimmed); ground kept
                 thSlotForAddr(src),
                 rd(TH_OFF_ACC), rd(TH_OFF_ACC + 1), rd(TH_OFF_ACC + 2),
                 rd(TH_OFF_GROUND), rd(TH_OFF_GROUND + 1));
        // Hand the line to the writer task — NEVER write serial from the radio hot path
        // (a blocking println here is what collapsed the 10 Hz poll to ~2 Hz and made
        // the motors lag by seconds). See the outbound-queue block above.
        qLine(line);
    }
}

// Active discovery: broadcast LIST_NODES ~2 Hz and turn every reply into a
// {"type":"thymio_found","addr":"XXXX"}. A reply is any frame on our PAN whose
// MAC source is a real robot (not the host, not broadcast) — its src IS the
// address the app needs. The PC de-dups and orders by first-seen. Runs on the
// same shared radio as the link/sniffer, so callers pause those first.
static void thymioDiscoverPump() {
    if (!s_thDiscover || s_sniffing) return;
    uint32_t now = millis();
    if (now - s_thDiscLast >= 100) {               // broadcast + re-arm at 10 Hz, EXACTLY
        s_thDiscLast = now;                        // the thymioLinkPump cadence (which is
        thDiscoverBroadcast();                     // rock-solid). thDiscoverBroadcast is
        esp_ieee802154_receive();                  // paced, so TX is done before receive().
        // Do NOT re-arm RX between beats: an extra esp_ieee802154_receive() mid-frame
        // aborts the reception in progress, so a faster re-arm actually DROPPED the
        // NODE_PRESENT reply (0 frames some sessions). One receive() per paced TX,
        // like the link, is what receives reliably.
    }
    if (!s_rxQ) return;
    SniffFrame f;
    while (xQueueReceive(s_rxQ, &f, 0) == pdTRUE) {
        if (s_thRxDebug) thEmitRaw(f);
        if (f.len < 9) continue;                   // no addressing (e.g. a bare ACK)
        uint16_t pan = f.data[3] | (f.data[4] << 8);
        uint16_t src = f.data[7] | (f.data[8] << 8);
        if (pan != 0x4481) continue;               // only the Thymio network
        if (src == TH_HOST_ADDR || src == 0xFFFF) continue;   // our own tx / broadcast
        char line[80];
        snprintf(line, sizeof(line),
                 "{\"type\":\"thymio_found\",\"src\":\"c6\",\"addr\":\"%04x\"}", src);
        Serial1.println(line);
        Serial.println(line);
    }
}

// Stop the wheels before the sniffer takes the radio: the pump pauses while
// sniffing and a Thymio holds its last motor target — don't let it coast blind.
static void thymioStopForSniff() {
    if (!s_thLink) return;
    int16_t zero = 0;
    for (int i = 0; i < TH_MAX; i++) {
        ThymioSlot& t = s_th[i];
        if (!t.active) continue;
        t.left = t.right = 0;
        thTx(t.addr, TH_SET_VARIABLES, 0x0056, &zero, 1);
        thTx(t.addr, TH_SET_VARIABLES, 0x0057, &zero, 1);
    }
}

// Handle one complete line from a transport; reply on that same stream.
static void handleLine(char* line, Print& io) {
    JsonDocument doc;
    if (deserializeJson(doc, line) == DeserializationError::Ok) {
        const char* cmd = doc["cmd"] | "";
        if (strcmp(cmd, "reboot") == 0) {
            // Clean-radio reset. esp_ieee802154_disable() does NOT restore the C6's
            // RX after repeated link/discover cycles (RX dies cumulatively — TX still
            // works, the Thymio's RF LED still blinks, but replies are never heard) nor
            // let WiFi rejoin (see OtaPark). A reboot is the only reliable reset, so the
            // PC reboots the C6 before a discovery pass to guarantee it can hear replies.
            io.println("{\"type\":\"rebooting\",\"src\":\"c6\"}");
            io.flush();
            delay(100);
            esp_restart();
        } else if (strcmp(cmd, "ota_wifi") == 0) {
            // The whole 802.15.4 side must be quiet before WiFi shares the
            // radio: stop the Thymio poller (wheels to zero — a Thymio holds
            // its last target), the sniffer, and the radio itself.
            bool wasLink = s_thLink;
            thymioStopForSniff();
            s_thLink = false;
            // Let the writer task drain any queued sensor lines first, so the OTA
            // progress lines below don't interleave with them on the UART.
            for (int i = 0; i < 20 && s_outQ && uxQueueMessagesWaiting(s_outQ); i++)
                delay(10);
            sniffStop(io);          // free the shared radio for WiFi before updating
            radioDown();            // ...also if a tx (not a sniff) had brought it up
            if (s_radioUsed) {
                // WiFi can't join anymore this boot (see OtaPark) — park the
                // request and reboot; setup() resumes it on the clean boot. The
                // start banner is sent NOW so the PC's updater switches to its
                // (long) done-wait instead of resending ota_wifi into the reboot.
                s_otaPark.magic  = OTA_PARK_MAGIC;
                snprintf(s_otaPark.ssid, sizeof(s_otaPark.ssid), "%s",
                         (const char*)(doc["ssid"] | ""));
                snprintf(s_otaPark.pass, sizeof(s_otaPark.pass), "%s",
                         (const char*)(doc["pass"] | ""));
                snprintf(s_otaPark.url, sizeof(s_otaPark.url), "%s",
                         (const char*)(doc["url"] | ""));
                s_otaPark.viaUsb = (&io == static_cast<Print*>(&Serial));
                io.println("{\"type\":\"ota_wifi_start\",\"src\":\"c6\"}");
                io.flush();
                delay(100);          // let the line drain onto the wire
                esp_restart();
            }
            doWifiOta(doc["ssid"] | "", doc["pass"] | "", doc["url"] | "", io);
            s_thLink = wasLink;     // only reached on failure (success reboots)
        } else if (strcmp(cmd, "ping") == 0) {
            io.println("{\"type\":\"pong\",\"src\":\"c6\"}");
        } else if (strcmp(cmd, "sniff_start") == 0) {
            thymioStopForSniff();
            sniffStart((uint8_t)(doc["ch"] | 0), io);
        } else if (strcmp(cmd, "sniff_stop") == 0) {
            sniffStop(io);
        } else if (strcmp(cmd, "sniff_ch") == 0) {
            s_sniffFixed = (uint8_t)(doc["n"] | 0);
            if (s_sniffing) {
                s_sniffCh = s_sniffFixed ? s_sniffFixed : s_sniffCh;
                sniffTune(s_sniffCh);
            }
            io.printf("{\"type\":\"sniff\",\"src\":\"c6\",\"ch\":%u,\"fixed\":%u}\n",
                      s_sniffCh, s_sniffFixed);
        } else if (strcmp(cmd, "tx") == 0) {
            doTx((uint8_t)(doc["ch"] | 0), doc["data"] | "", io);
        } else if (strcmp(cmd, "thymio_link") == 0) {
            // {"cmd":"thymio_link","on":true[,"ch":25]} — start/stop the background poller
            s_thCh = (uint8_t)(doc["ch"] | s_thCh);
            if (doc["on"] | true) {
                radioUp();
                if (!s_rxQ) s_rxQ = xQueueCreate(24, sizeof(SniffFrame));
                // Promiscuous + rx_when_idle so we hear the Thymio's VARIABLES reply
                // (addressed to the host 0x3237, which the C6 never claimed) between
                // our TX bursts — thymioRxPump parses it into thymio_sensors.
                esp_ieee802154_set_promiscuous(true);
                esp_ieee802154_set_rx_when_idle(true);
                esp_ieee802154_set_channel(s_thCh);
                esp_ieee802154_receive();
                s_thSeq = (uint8_t)millis();       // fresh seq so repeats aren't dup-dropped
                bool any = false;
                for (int i = 0; i < TH_MAX; i++) any |= s_th[i].active;
                // TODO(later, per user): drop this hardcoded 0x6a25 default — require the PC
                // to register every Thymio explicitly with thymio_set (addresses come from
                // discovery/config). Kept for now for backward-compat with the single-Thymio
                // thymio_link.py flow that doesn't send an address.
                if (!any) { s_th[0].active = true; s_th[0].addr = 0x6A25; }   // default: one
                s_thLink = true;
            } else {
                s_thLink = false;
                for (int i = 0; i < TH_MAX; i++) {
                    s_th[i].active = false; s_th[i].left = s_th[i].right = 0;
                }
            }
            io.printf("{\"type\":\"thymio_link\",\"src\":\"c6\",\"on\":%d,\"ch\":%u}\n",
                      s_thLink ? 1 : 0, s_thCh);
        } else if (strcmp(cmd, "thymio_discover") == 0) {
            // {"cmd":"thymio_discover","on":true[,"ch":25]} — dongle-free discovery:
            // broadcast LIST_NODES and report every Thymio that replies (thymio_found).
            // Shares the radio with the link/sniffer, so pause the poller first.
            s_thDiscCh = (uint8_t)(doc["ch"] | s_thDiscCh);
            if (doc["on"] | true) {
                thymioStopForSniff();          // zero wheels if the link was driving
                s_thLink = false;
                s_sniffing = false;
                if (!s_rxQ) s_rxQ = xQueueCreate(24, sizeof(SniffFrame));
                // NO down→up "fresh radio" here: esp_ieee802154_disable() poisons
                // the modem for the rest of the boot, so re-enabling gave a DEAF
                // radio every scan after the first (and the same poisoning is why
                // a WiFi join fails after any 15.4 use — see OtaPark). Once up the
                // radio stays up; a scan just re-tunes and re-arms RX.
                bool fresh = !s_radioEnabled;
                radioUp();
                if (fresh) delay(50);          // settle before arming RX (see sniffStart)
                esp_ieee802154_set_promiscuous(true);   // hear replies addressed to the host
                esp_ieee802154_set_rx_when_idle(true);
                esp_ieee802154_set_channel(s_thDiscCh);
                esp_ieee802154_receive();
                s_thSeq = (uint8_t)millis();
                s_thDiscLast = 0;
                s_thDiscover = true;
            } else {
                s_thDiscover = false;          // radio stays up (a disable would poison
            }                                  // every later scan/link in this boot)
            io.printf("{\"type\":\"thymio_discover\",\"src\":\"c6\",\"on\":%d,\"ch\":%u}\n",
                      s_thDiscover ? 1 : 0, s_thDiscCh);
        } else if (strcmp(cmd, "thymio_rx_debug") == 0) {
            // {"cmd":"thymio_rx_debug","on":true} — stream raw RX frames while the
            // link is on, to confirm the Thymio's sensor reply arrives / check the
            // parse. Noisy; leave off in normal use.
            s_thRxDebug = (bool)(doc["on"] | true);
            io.printf("{\"type\":\"thymio_rx_debug\",\"src\":\"c6\",\"on\":%d}\n",
                      s_thRxDebug ? 1 : 0);
        } else if (strcmp(cmd, "thymio_set") == 0) {
            // {"cmd":"thymio_set","idx":0,"addr":"6a25"} — register a Thymio in a slot
            int idx = doc["idx"] | 0;
            if (idx >= 0 && idx < TH_MAX) {
                s_th[idx].addr = (uint16_t)strtol(doc["addr"] | "6a25", nullptr, 16);
                s_th[idx].active = true;
                s_th[idx].left = s_th[idx].right = 0;
                io.printf("{\"type\":\"thymio_set\",\"src\":\"c6\",\"idx\":%d,\"addr\":\"%04x\"}\n",
                          idx, s_th[idx].addr);
            }
        } else if (strcmp(cmd, "thymio_drive") == 0) {
            // {"cmd":"thymio_drive"[,"idx":0],"left":200,"right":-200} — held motor targets
            int idx = doc["idx"] | 0;
            if (idx >= 0 && idx < TH_MAX) {
                s_th[idx].left  = (int16_t)(int)(doc["left"]  | 0);
                s_th[idx].right = (int16_t)(int)(doc["right"] | 0);
                thymioAssertMotors(idx);   // react now, don't wait for the next poll
                // Echo the moment the C6 RECEIVED this drive, so the PC log can compare it
                // to the "Sent to thymio: thymio_drive" timestamp: if the ack lags the send,
                // the command path (PC→S3→C6) is slow; if it's instant but the robot still
                // lags, the delay is C6↔Thymio (the robot). Splits the two decisively.
                char ack[64];
                snprintf(ack, sizeof(ack),
                         "{\"type\":\"drive_ack\",\"src\":\"c6\",\"idx\":%d,\"l\":%d,\"r\":%d}",
                         idx, s_th[idx].left, s_th[idx].right);
                qLine(ack);
            }
        } else if (strcmp(cmd, "thymio_leds") == 0) {
            // {"cmd":"thymio_leds"[,"idx":0],"r":32,"g":0,"b":0} — leds.top (burst-resent)
            int idx = doc["idx"] | 0;
            if (idx >= 0 && idx < TH_MAX) {
                s_th[idx].leds[0] = (int16_t)(int)(doc["r"] | 0);
                s_th[idx].leds[1] = (int16_t)(int)(doc["g"] | 0);
                s_th[idx].leds[2] = (int16_t)(int)(doc["b"] | 0);
                s_th[idx].ledsResend = 8;
                thymioAssertLeds(idx);     // show the colour now, not at the next poll
            }
        } else if (strcmp(cmd, "thymio_sound") == 0) {
            // {"cmd":"thymio_sound"[,"idx":0],"sys":2}   → system sound 0..7 (-1 stops)
            // {"cmd":"thymio_sound"[,"idx":0],"freq":700,"dur":30}  → tone (dur in 1/60 s)
            // Needs the link on (radio up + on channel — held by the poller).
            int idx = doc["idx"] | 0;
            if (idx < 0 || idx >= TH_MAX) idx = 0;
            uint16_t addr = s_th[idx].active ? s_th[idx].addr : 0x6A25;
            if (!s_thLink || s_sniffing) {
                io.println("{\"type\":\"thymio_sound\",\"src\":\"c6\",\"err\":\"link_off\"}");
            } else if (doc["track"].is<int>()) {
                if (!thPlayTrack(addr, (int16_t)(int)(doc["track"] | 0)))
                    io.println("{\"type\":\"thymio_sound\",\"src\":\"c6\","
                               "\"err\":\"sound_play_index_unset\"}");
            } else if (doc["freq"].is<int>()) {
                thPlayFreq(addr, (int16_t)(int)(doc["freq"] | 440),
                           (int16_t)(int)(doc["dur"] | 30));
            } else {
                thPlaySystem(addr, (int16_t)(int)(doc["sys"] | 0));
            }
        }
        return;
    }
    if (strncmp(line, "PING", 4) == 0) io.printf("PONG%s\n", line + 4);   // bring-up test
}

// Accumulate a line from a transport and dispatch it (own buffer per transport).
static void pump(Stream &io, char *buf, size_t &len, size_t cap) {
    while (io.available()) {
        char c = (char)io.read();
        if (c == '\r') continue;
        if (c == '\n') {
            buf[len] = '\0';
            if (len > 0) handleLine(buf, io);
            len = 0;
        } else if (len < cap - 1) {
            buf[len++] = c;
        }
    }
}

void setup() {
    Serial.begin(115200);
    Serial.setTxTimeoutMs(0);                    // never block without a USB host
    // Bigger UART TX ring so a burst of sensor frames is buffered (and availableForWrite
    // in thymioRxPump is meaningful) rather than stalling the poll; bigger RX ring for
    // command bursts. Both must be set before begin().
    Serial1.setRxBufferSize(1024);
    Serial1.setTxBufferSize(2048);
    Serial1.begin(LINK_BAUD, SERIAL_8N1, LINK_RX, LINK_TX);
    // Radio-pacing semaphore (txWait blocks instead of busy-spinning) and the outbound
    // line queue + writer task: telemetry writes happen OFF the radio hot path, so a
    // stalled S3/PC can never freeze the 10 Hz Thymio poll. Priority 2 — above the
    // Arduino loop task (1): the writer wakes only when a line is queued, writes into
    // the serial TX ring, and blocks again (or blocks on a full ring, yielding to loop).
    s_txDoneSem = xSemaphoreCreateBinary();
    s_outQ = xQueueCreate(24, sizeof(OutLine));
    xTaskCreate(outWriterTask, "uart_out", 3072, nullptr, 2, nullptr);
    WiFi.mode(WIFI_OFF);                          // radio off until an ota_wifi asks for it
    // This Arduino build enables app rollback, so a freshly flashed / OTA'd app boots
    // in PENDING_VERIFY. Until it is confirmed, the NEXT esp_ota_set_boot_partition is
    // refused (WiFi-OTA "activate" fails with UPDATE_ERROR_ACTIVATE / err 9). Confirm
    // ourselves so OTA can activate, and so a good OTA sticks instead of rolling back.
    esp_ota_mark_app_valid_cancel_rollback();
    // A parked WiFi-OTA (see OtaPark): resume it on this clean boot, before any
    // 15.4 use can poison the WiFi join. One-shot — clear the magic FIRST so a
    // crash mid-update can't boot-loop into it. On success doWifiOta never
    // returns (reboots into the new image, whose banner is the PC's done signal);
    // on failure fall through to a normal boot (the fail line already went out).
    if (s_otaPark.magic == OTA_PARK_MAGIC) {
        s_otaPark.magic = 0;
        Print& io = s_otaPark.viaUsb ? static_cast<Print&>(Serial)
                                     : static_cast<Print&>(Serial1);
        doWifiOta(s_otaPark.ssid, s_otaPark.pass, s_otaPark.url, io);
    }
    delay(300);
    // Announce on BOTH transports. This banner doubles as the WiFi-OTA "done" signal,
    // so it MUST reach the S3 over the UART (the host reads Serial1, not the C6's USB).
    // Multi-send it: a single frame lost to the reboot glitch would otherwise hang the
    // updater at ~99% ("rebooting…") until it times out — same fix as the nodes' ota_done.
    for (int i = 0; i < 4; i++) {
        Serial.println("{\"type\":\"rcp_ready\",\"src\":\"c6\",\"fw\":\"thymio-fast-motors\"}");
        Serial1.println("{\"type\":\"rcp_ready\",\"src\":\"c6\",\"fw\":\"thymio-fast-motors\"}");
        delay(150);
    }
}

// Drain + dispatch complete command lines from the S3→C6 UART (Serial1). Shared with the
// Thymio poll (thymioLinkPump calls this between its paced TXs) through the file-scope
// s_uartBuf, so a drive/LED command that lands mid-poll is acted on immediately instead
// of waiting for the poll to finish its TX burst.
static void pumpThymioUart() { pump(Serial1, s_uartBuf, s_uartLen, sizeof(s_uartBuf)); }

void loop() {
    static char usb_buf[256]; static size_t usb_len = 0;
    pump(Serial, usb_buf, usb_len, sizeof(usb_buf));   // C6's own USB (unused for data)
    pumpThymioUart();  // S3→C6 command link (same buffer the poll drains mid-burst)
    sniffPump();       // stream any 802.15.4 frames while sniffing (no-op otherwise)
    thymioLinkPump();  // keep the Thymio link hot + assert motor/LED targets (no-op if off)
    thymioRxPump();    // parse the Thymio's VARIABLES reply → thymio_sensors (no-op if off)
    thymioDiscoverPump();  // broadcast LIST_NODES + report replies while discovering (no-op if off)

    // ---- Link health report: measurement instead of guesswork -----------------------
    // Every 5 s while the link runs, emit the REAL poll rate (the Thymio's RF-LED blink
    // rate in numbers — 10.0 = healthy, ~2 = the poll is being stalled), the worst
    // single serial-write stall the writer task saw, and how many telemetry lines were
    // dropped to protect the poll. Lands in softedibo.log as {"type":"rcp_health",...}.
    static uint32_t s_lastHealthMs = 0, s_lastPolls = 0, s_lastTxTo = 0, s_lastRx = 0;
    uint32_t nowH = millis();
    if (!s_thLink) {
        s_lastHealthMs = 0;                      // restart the window on the next link-on
    } else if (s_lastHealthMs == 0) {
        s_lastHealthMs = nowH; s_lastPolls = s_thPollCount; s_lastTxTo = s_txTimeouts;
        s_lastRx = s_thRxCount; s_uartBlockMaxMs = 0;
    } else if (nowH - s_lastHealthMs >= 5000) {
        uint32_t dt = nowH - s_lastHealthMs;
        uint32_t hzX10   = (s_thPollCount - s_lastPolls) * 10000UL / dt;
        // rx_hz = full sensor replies/s the Thymio actually returned. We ask for ~3.3 Hz;
        // if rx_hz sags below that (while poll_hz stays 10 and tx_to is 0), the THYMIO's
        // own CPU can't keep up — the bottleneck is in the robot, not our side.
        uint32_t rxHzX10 = (s_thRxCount - s_lastRx) * 10000UL / dt;
        // tx_to = TXs this window whose ACK never arrived (radio degrading).
        char h[184];
        snprintf(h, sizeof(h),
                 "{\"type\":\"rcp_health\",\"src\":\"c6\",\"poll_hz\":%u.%u,\"rx_hz\":%u.%u,"
                 "\"tx_to\":%u,\"uart_block_max_ms\":%u,\"dropped_lines\":%u,\"outq\":%u,\"heap\":%u}",
                 (unsigned)(hzX10 / 10), (unsigned)(hzX10 % 10),
                 (unsigned)(rxHzX10 / 10), (unsigned)(rxHzX10 % 10),
                 (unsigned)(s_txTimeouts - s_lastTxTo),
                 (unsigned)s_uartBlockMaxMs, (unsigned)s_outDrops,
                 (unsigned)(s_outQ ? uxQueueMessagesWaiting(s_outQ) : 0),
                 (unsigned)esp_get_free_heap_size());
        qLine(h);
        s_lastHealthMs = nowH; s_lastPolls = s_thPollCount; s_lastTxTo = s_txTimeouts;
        s_lastRx = s_thRxCount; s_uartBlockMaxMs = 0;
    }
}

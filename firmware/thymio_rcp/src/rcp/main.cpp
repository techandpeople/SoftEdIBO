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

    // Retry the pull: the AP link (shared radio, contended by ESP-NOW) can drop a
    // sustained download. httpUpdate reboots into the new image on success, so a good
    // attempt never returns; we only loop on failure.
    // Download + write + verify via HTTPUpdate, but activate ourselves so we can
    // report the EXACT esp_err (HTTPUpdate only surfaces a generic "err 9").
    httpUpdate.rebootOnUpdate(false);
    WiFiClient client;
    t_httpUpdate_return r = httpUpdate.update(client, url);
    if (r == HTTP_UPDATE_OK) {                        // written, verified AND activated
        io.println("{\"type\":\"ota_wifi_ok\",\"src\":\"c6\"}");
        delay(200);
        esp_restart();
    }
    int uerr = httpUpdate.getLastError();
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
// Frames are streamed as JSON over Serial1 so the S3 relays them to the PC. The hex
// payload is capped so a line fits the S3's 256-char relay buffer (no S3 reflash); the
// true length is still reported as "len". Never armed at boot — a radio hiccup is
// always recoverable by a reboot.
static constexpr uint8_t  SNIFF_MAX_BYTES = 96;
static constexpr uint32_t SNIFF_HOP_MS    = 1500;   // dwell per channel when hopping

struct SniffFrame {
    uint8_t len;
    uint8_t data[128];
    int8_t  rssi;
    uint8_t lqi;
    uint8_t channel;
};
static QueueHandle_t s_sniffQ   = nullptr;
static volatile bool s_sniffing = false;
static uint8_t       s_sniffCh    = 11;   // current channel
static uint8_t       s_sniffFixed = 0;    // 0 = hop 11..26, else locked channel

// The 802.15.4 radio is enabled once and left up. esp_ieee802154_enable() allocates the
// ZB_MAC interrupt; calling it again without a matching disable() leaks that allocation
// until "No free interrupt inputs for ZB_MAC" — after which the radio silently stops
// transmitting even though esp_ieee802154_transmit() still returns ESP_OK. So gate it.
static bool s_radioEnabled = false;
static void radioUp()   { if (!s_radioEnabled) { esp_ieee802154_enable();  s_radioEnabled = true;  } }
static void radioDown() { if (s_radioEnabled)  { esp_ieee802154_disable(); s_radioEnabled = false; } }

// Set by the driver when a transmit (incl. its ACK wait) completes — lets thTx() pace
// back-to-back frames instead of bursting them into a busy radio (which drops them).
static volatile bool s_txDone = true;

// Driver ISR callback: copy the frame out and hand it to loop() via the queue.
void IRAM_ATTR esp_ieee802154_receive_done(uint8_t* frame,
                                           esp_ieee802154_frame_info_t* info) {
    if (!s_sniffQ) return;
    SniffFrame f;
    uint8_t len = frame[0];
    if (len > sizeof(f.data)) len = sizeof(f.data);
    f.len = len; f.rssi = info->rssi; f.lqi = info->lqi; f.channel = info->channel;
    memcpy(f.data, &frame[1], len);
    BaseType_t hp = pdFALSE;
    xQueueSendFromISR(s_sniffQ, &f, &hp);
    if (hp) portYIELD_FROM_ISR();
}

// TX-done / TX-failed driver callbacks: just release the pacing wait in thTx().
void esp_ieee802154_transmit_done(const uint8_t* frame, const uint8_t* ack,
                                  esp_ieee802154_frame_info_t* ack_frame_info) { s_txDone = true; }
void esp_ieee802154_transmit_failed(const uint8_t* frame, esp_ieee802154_tx_error_t error) { s_txDone = true; }

static void sniffTune(uint8_t channel) {
    esp_ieee802154_set_channel(channel);
    esp_ieee802154_receive();               // re-arm RX on the (new) channel
}

static void sniffStart(uint8_t fixed_ch, Print& io) {
    if (!s_sniffQ) s_sniffQ = xQueueCreate(24, sizeof(SniffFrame));
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
    radioDown();
    io.println("{\"type\":\"sniff\",\"src\":\"c6\",\"state\":\"stop\"}");
}

// Drain queued frames to Serial1 as JSON, and hop channels when not locked.
static void sniffPump() {
    if (!s_sniffing || !s_sniffQ) return;
    SniffFrame f;
    bool got = false;
    while (xQueueReceive(s_sniffQ, &f, 0) == pdTRUE) {
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
    s_txDone = false;
    esp_ieee802154_transmit(f, false);
    uint32_t t0 = millis();                        // pace: wait TX (+ACK) done before the
    while (!s_txDone && millis() - t0 < 15) delayMicroseconds(100);   // next frame
}

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

static void thymioLinkPump() {
    // The sniffer owns the radio while scanning (promiscuous / other channels):
    // polling through it would corrupt both. sniffStart zeroed our motors first.
    if (!s_thLink || s_sniffing) return;
    uint32_t now = millis();
    if (now - s_thLastPoll < TH_POLL_MS) return;
    s_thLastPoll = now;
    for (int i = 0; i < TH_MAX; i++) {             // poll + assert every active Thymio
        ThymioSlot& t = s_th[i];
        if (!t.active) continue;
        int16_t cnt = 0x0080;
        thTx(t.addr, TH_GET_VARIABLES, 0x0000, &cnt, 1);    // keep this robot's link hot
        thTx(t.addr, TH_SET_VARIABLES, 0x0056, &t.left, 1);  // motor.left.target
        thTx(t.addr, TH_SET_VARIABLES, 0x0057, &t.right, 1); // motor.right.target
        if (t.ledsResend) { t.ledsResend--; thTx(t.addr, TH_SET_VARIABLES, 0x0065, t.leds, 3); }
    }
    esp_ieee802154_receive();                      // stay armed for the ACKs
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
        if (strcmp(cmd, "ota_wifi") == 0) {
            // The whole 802.15.4 side must be quiet before WiFi shares the
            // radio: stop the Thymio poller (wheels to zero — a Thymio holds
            // its last target), the sniffer, and the radio itself.
            bool wasLink = s_thLink;
            thymioStopForSniff();
            s_thLink = false;
            sniffStop(io);          // free the shared radio for WiFi before updating
            radioDown();            // ...also if a tx (not a sniff) had brought it up
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
            }
        } else if (strcmp(cmd, "thymio_leds") == 0) {
            // {"cmd":"thymio_leds"[,"idx":0],"r":32,"g":0,"b":0} — leds.top (burst-resent)
            int idx = doc["idx"] | 0;
            if (idx >= 0 && idx < TH_MAX) {
                s_th[idx].leds[0] = (int16_t)(int)(doc["r"] | 0);
                s_th[idx].leds[1] = (int16_t)(int)(doc["g"] | 0);
                s_th[idx].leds[2] = (int16_t)(int)(doc["b"] | 0);
                s_th[idx].ledsResend = 8;
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
    Serial1.begin(LINK_BAUD, SERIAL_8N1, LINK_RX, LINK_TX);
    WiFi.mode(WIFI_OFF);                          // radio off until an ota_wifi asks for it
    // This Arduino build enables app rollback, so a freshly flashed / OTA'd app boots
    // in PENDING_VERIFY. Until it is confirmed, the NEXT esp_ota_set_boot_partition is
    // refused (WiFi-OTA "activate" fails with UPDATE_ERROR_ACTIVATE / err 9). Confirm
    // ourselves so OTA can activate, and so a good OTA sticks instead of rolling back.
    esp_ota_mark_app_valid_cancel_rollback();
    delay(300);
    // Announce on BOTH transports. This banner doubles as the WiFi-OTA "done" signal,
    // so it MUST reach the S3 over the UART (the host reads Serial1, not the C6's USB).
    // Multi-send it: a single frame lost to the reboot glitch would otherwise hang the
    // updater at ~99% ("rebooting…") until it times out — same fix as the nodes' ota_done.
    for (int i = 0; i < 4; i++) {
        Serial.println("{\"type\":\"rcp_ready\",\"src\":\"c6\"}");
        Serial1.println("{\"type\":\"rcp_ready\",\"src\":\"c6\"}");
        delay(150);
    }
}

void loop() {
    static char usb_buf[256];  static size_t usb_len  = 0;
    static char uart_buf[256]; static size_t uart_len = 0;
    pump(Serial,  usb_buf,  usb_len,  sizeof(usb_buf));
    pump(Serial1, uart_buf, uart_len, sizeof(uart_buf));
    sniffPump();       // stream any 802.15.4 frames while sniffing (no-op otherwise)
    thymioLinkPump();  // keep the Thymio link hot + assert motor/LED targets (no-op if off)
}

/*
 * SoftEdIBO — C6 802.15.4 radio bring-up (standalone, no S3, no wiring).
 *
 * Flash this onto a XIAO ESP32-C6 over its OWN USB and watch the USB monitor.
 * It brings up the IEEE 802.15.4 radio in promiscuous mode and prints every
 * raw frame it hears, hopping across channels 11..26 so you can find which
 * channel a Wireless Thymio (or any 802.15.4 device) is talking on.
 *
 * This is the first hands-on step of Phase 1 in docs/THYMIO_WIRELESS_CONTROL.md:
 * prove the C6 radio lives and start observing the Thymio dongle's traffic
 * before reimplementing its protocol.
 *
 * NOTE: compiles clean; RUNTIME is untested on hardware (no C6 here). Promiscuous
 * RX replaces the 2-byte FCS with [RSSI, LQI]; we print frame_info's rssi/lqi.
 *
 * Build/flash: pio run -e c6_radio -t upload ; then pio device monitor.
 */
#include <Arduino.h>
#include "esp_ieee802154.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

// Set to a channel 11..26 to lock onto it; 0 = hop across the whole band.
static constexpr uint8_t FIXED_CHANNEL = 0;
static constexpr uint32_t HOP_DWELL_MS  = 1500;   // time spent per channel when hopping

struct Frame {
  uint8_t  len;          // PSDU length (frame[0])
  uint8_t  data[128];    // frame[1..len]
  int8_t   rssi;
  uint8_t  lqi;
  uint8_t  channel;
};

static QueueHandle_t s_frames;

// Called by the driver in ISR context — keep it tiny and in IRAM: copy the
// frame out and hand it to loop() via the queue. No Serial/logging here.
void IRAM_ATTR esp_ieee802154_receive_done(uint8_t *frame,
                                           esp_ieee802154_frame_info_t *info) {
  if (!s_frames) return;
  Frame f;
  uint8_t len = frame[0];
  if (len > sizeof(f.data)) len = sizeof(f.data);
  f.len     = len;
  f.rssi    = info->rssi;
  f.lqi     = info->lqi;
  f.channel = info->channel;
  memcpy(f.data, &frame[1], len);
  BaseType_t hp_woken = pdFALSE;
  xQueueSendFromISR(s_frames, &f, &hp_woken);
  if (hp_woken) portYIELD_FROM_ISR();
}

static void start_rx(uint8_t channel) {
  esp_ieee802154_set_channel(channel);
  esp_ieee802154_receive();   // re-arm RX on the (new) channel
}

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n[c6 802154] radio bring-up — promiscuous sniffer");

  s_frames = xQueueCreate(16, sizeof(Frame));

  esp_err_t err = esp_ieee802154_enable();
  Serial.printf("[c6 802154] enable -> %s\n", esp_err_to_name(err));
  esp_ieee802154_set_promiscuous(true);
  esp_ieee802154_set_rx_when_idle(true);

  const uint8_t ch = FIXED_CHANNEL ? FIXED_CHANNEL : 11;
  start_rx(ch);
  Serial.printf("[c6 802154] listening on channel %u%s\n",
                ch, FIXED_CHANNEL ? " (fixed)" : " (hopping 11..26)");
}

void loop() {
  static uint8_t  channel    = FIXED_CHANNEL ? FIXED_CHANNEL : 11;
  static uint32_t last_hop   = 0;
  static uint32_t seen_on_ch = 0;

  // Drain any received frames and print them.
  Frame f;
  while (xQueueReceive(s_frames, &f, 0) == pdTRUE) {
    seen_on_ch++;
    Serial.printf("[c6 802154] ch=%2u rssi=%4d lqi=%3u len=%3u | ",
                  f.channel, f.rssi, f.lqi, f.len);
    for (uint8_t i = 0; i < f.len; i++) Serial.printf("%02X ", f.data[i]);
    Serial.println();
  }

  // Channel hopping (skip when locked to a fixed channel).
  if (!FIXED_CHANNEL && millis() - last_hop >= HOP_DWELL_MS) {
    last_hop = millis();
    if (seen_on_ch == 0) Serial.printf("[c6 802154] ch=%2u — no frames\n", channel);
    seen_on_ch = 0;
    channel = (channel >= 26) ? 11 : channel + 1;
    start_rx(channel);
  }
}

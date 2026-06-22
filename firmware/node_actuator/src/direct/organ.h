#pragma once
#include <Arduino.h>
#include <math.h>

#include "se_espnow.h"
#include "pins.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// Organ + cover sensing.
//
// One ADC line measures both the organ identity and the cover state:
//
//   3V3 ── R_KNOWN ──●── organ resistor network ── cover contact ── GND
//                    │
//               ORGAN_SENSE_PIN
//
// Each pluggable organ is a silicone block with a known internal resistor;
// all organ slots are wired in parallel (one reading per node). The silicone
// cover rests on the contact pads by gravity and closes the return path:
//   cover off → open circuit → ADC at the 3V3 rail → {"open":true}
//   cover on  → R_total = R_KNOWN * raw / (ADC_MAX - raw)
//
// A gravity contact can bounce while children handle the cover, so an
// open/closed flip is only reported after DEBOUNCE_SAMPLES consistent
// readings. The current state is also re-sent every HEARTBEAT_MS so the PC
// recovers from missed packets (and gets the state even if it connects late).
// ---------------------------------------------------------------------------

namespace organ {

constexpr float    R_KNOWN_OHM      = 1000.0f;
constexpr int      ADC_MAX          = 4095;
constexpr int      OPEN_RAW         = 4000;   // >= this: open circuit (cover off)
constexpr int      SHORT_RAW        = 60;     // <= this: short (R ~ 0)
constexpr uint32_t SAMPLE_MS        = 100;
constexpr int      DEBOUNCE_SAMPLES = 3;      // consecutive flips before reporting
constexpr float    HYST_OHM         = 25.0f;  // resistance delta worth re-sending
constexpr uint32_t HEARTBEAT_MS     = 2000;

inline bool     open           = true;    // debounced, reported state
inline float    resistance     = -1.0f;   // last measured R (Ω), -1 when open
inline int      flipCount      = 0;       // consecutive samples disagreeing with `open`
inline float    lastSentOhm    = -1.0f;
inline bool     lastSentOpen   = true;
inline bool     sentOnce       = false;
inline uint32_t lastSampleMs   = 0;
inline uint32_t lastSendMs     = 0;

inline void hardware_init() {
    analogSetPinAttenuation(ORGAN_SENSE_PIN, ADC_11db);
}

// Average a few reads, classify open/short, convert to Ω via the divider.
inline float sample(bool& isOpen) {
    uint32_t acc = 0;
    for (int i = 0; i < 8; i++) acc += analogRead(ORGAN_SENSE_PIN);
    int raw = static_cast<int>(acc / 8);
    if (raw >= OPEN_RAW) { isOpen = true; return -1.0f; }
    isOpen = false;
    if (raw <= SHORT_RAW) return 0.0f;
    return R_KNOWN_OHM * static_cast<float>(raw)
                       / static_cast<float>(ADC_MAX - raw);
}

inline bool send() {
    using se::node::gatewayMac;
    using se::node::gatewayKnown;
    if (!gatewayKnown) return false;
    char buf[72];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"organ\",\"resistance_ohm\":%.1f,\"open\":%s}",
        open ? -1.0f : resistance, open ? "true" : "false");
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
    return true;
}

inline void tick(uint32_t now) {
    if (now - lastSampleMs < SAMPLE_MS) return;
    lastSampleMs = now;

    bool  rawOpen;
    float r = sample(rawOpen);

    // Debounce the open/closed flip; resistance updates immediately while
    // the contact state is stable.
    if (rawOpen != open) {
        if (++flipCount >= DEBOUNCE_SAMPLES) {
            open = rawOpen;
            resistance = r;
            flipCount = 0;
        }
    } else {
        flipCount = 0;
        resistance = r;
    }

    bool changed = !sentOnce
        || open != lastSentOpen
        || (!open && fabsf(resistance - lastSentOhm) > HYST_OHM);
    if (!changed && now - lastSendMs < HEARTBEAT_MS) return;

    if (send()) {
        sentOnce     = true;
        lastSentOpen = open;
        lastSentOhm  = resistance;
        lastSendMs   = now;
    }
}

}  // namespace organ

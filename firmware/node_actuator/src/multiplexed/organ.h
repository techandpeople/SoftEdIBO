#pragma once
#include <Arduino.h>
#include <math.h>

#include "se_espnow.h"
#include "mux.h"
#include "pins.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// Organ + cover sensing — multiplexed variant.
//
// Same electrical design as the direct node (see direct/organ.h):
//
//   3V3 ── R_KNOWN ──●── organ resistor network ── cover contact ── GND
//                    │
//              74HC4067 input channel
//
// but each organ circuit hangs off its own mux channel, so one node can
// serve several independent "patients" (e.g. one per Tree branch). Which
// channels carry organ circuits is runtime configuration: the gateway's
// `configure` command carries `organ_channels: [c, ...]`; index i in that
// list becomes `slot` i in the broadcasts:
//
//   {"type":"organ","slot":i,"resistance_ohm":R,"open":bool}
//
// Convention: wire organ circuits to the HIGHEST mux channels (I13..I15) so
// the boot chamber/tank autodetect — which claims low channels first — does
// not collide with them. setChannels() also scrubs the configured channels
// from any autodetected chamber/tank assignment as a backstop.
// ---------------------------------------------------------------------------

namespace organ {

constexpr int      MAX_ORGANS       = 4;
constexpr float    R_KNOWN_OHM      = 1000.0f;
constexpr int      ADC_MAX          = 4095;
constexpr int      OPEN_RAW         = 4000;   // >= this: open circuit (cover off)
constexpr int      SHORT_RAW        = 60;     // <= this: short (R ~ 0)
constexpr uint32_t SAMPLE_MS        = 100;
constexpr int      DEBOUNCE_SAMPLES = 3;      // consecutive flips before reporting
constexpr float    HYST_OHM         = 25.0f;  // resistance delta worth re-sending
constexpr uint32_t HEARTBEAT_MS     = 2000;

struct Slot {
    int      mux_ch       = -1;
    bool     open         = true;    // debounced, reported state
    float    resistance   = -1.0f;   // last measured R (Ω), -1 when open
    int      flipCount    = 0;
    float    lastSentOhm  = -1.0f;
    bool     lastSentOpen = true;
    bool     sentOnce     = false;
    uint32_t lastSendMs   = 0;
};

inline Slot     slots[MAX_ORGANS];
inline int      slotCount    = 0;
inline uint32_t lastSampleMs = 0;

inline void setChannels(const int channels[], int count) {
    slotCount = constrain(count, 0, MAX_ORGANS);
    for (int i = 0; i < MAX_ORGANS; i++) {
        slots[i] = Slot{};
        if (i < slotCount) slots[i].mux_ch = channels[i];
    }
}

// Average a few mux reads, classify open/short, convert to Ω via the divider.
inline float sample(int mux_ch, bool& isOpen) {
    uint32_t acc = 0;
    for (int i = 0; i < 8; i++) acc += mux::readRaw(mux_ch);
    int raw = static_cast<int>(acc / 8);
    if (raw >= OPEN_RAW) { isOpen = true; return -1.0f; }
    isOpen = false;
    if (raw <= SHORT_RAW) return 0.0f;
    return R_KNOWN_OHM * static_cast<float>(raw)
                       / static_cast<float>(ADC_MAX - raw);
}

inline bool send(int slot_idx) {
    using se::node::gatewayMac;
    using se::node::gatewayKnown;
    if (!gatewayKnown) return false;
    Slot& s = slots[slot_idx];
    char buf[88];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"organ\",\"slot\":%d,\"resistance_ohm\":%.1f,\"open\":%s}",
        slot_idx, s.open ? -1.0f : s.resistance, s.open ? "true" : "false");
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
    return true;
}

inline void tickSlot(int i, uint32_t now) {
    Slot& s = slots[i];
    if (s.mux_ch < 0 || s.mux_ch >= mux::MUX_CHANNELS) return;

    bool  rawOpen;
    float r = sample(s.mux_ch, rawOpen);

    // Debounce the open/closed flip (the cover rests by gravity); the
    // resistance value updates immediately while the contact is stable.
    if (rawOpen != s.open) {
        if (++s.flipCount >= DEBOUNCE_SAMPLES) {
            s.open = rawOpen;
            s.resistance = r;
            s.flipCount = 0;
        }
    } else {
        s.flipCount = 0;
        s.resistance = r;
    }

    bool changed = !s.sentOnce
        || s.open != s.lastSentOpen
        || (!s.open && fabsf(s.resistance - s.lastSentOhm) > HYST_OHM);
    if (!changed && now - s.lastSendMs < HEARTBEAT_MS) return;

    if (send(i)) {
        s.sentOnce     = true;
        s.lastSentOpen = s.open;
        s.lastSentOhm  = s.resistance;
        s.lastSendMs   = now;
    }
}

inline void tick(uint32_t now) {
    if (slotCount == 0) return;
    if (now - lastSampleMs < SAMPLE_MS) return;
    lastSampleMs = now;
    for (int i = 0; i < slotCount; i++)
        tickSlot(i, now);
}

}  // namespace organ

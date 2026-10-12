#pragma once
#include <Arduino.h>

#include "se_espnow.h"
#include "pins.h"
#include "dbg.h"

// ---------------------------------------------------------------------------
// Push button, active low.
//
//   3V3 -- pull-up --*-- button -- GND
//                    |
//               BUTTON_PIN
//
// IO36 is input-only and has NO internal pull-up, so the pull-up resistor must
// be on the board (the organ divider's R_KNOWN is one - see organ.h, which
// reads the same pin). The line is therefore sampled through the ADC, like
// organ.h does, instead of digitalRead: pressed = the line sits at GND.
//
// A press/release is reported after DEBOUNCE_SAMPLES consistent readings, as
// {"type":"button","pressed":true|false}. The node is otherwise SILENT while
// the button is released, so an idle button costs no air time:
//   - boot:     the current state, once, as soon as the gateway is known
//   - press:    pressed:true, re-sent every HEARTBEAT_MS for as long as it is
//               held (the PC treats a missing heartbeat as a release, so a
//               lost release frame cannot leave the button stuck down)
//   - release:  pressed:false, once
// An edge frame the radio reports as failed is re-sent (up to EDGE_RETRIES).
// Edge reports are spaced MIN_SEND_GAP_MS apart so a floating pin (no button
// wired) cannot flood the radio.
// ---------------------------------------------------------------------------

namespace button {

constexpr int      PRESSED_RAW      = 200;    // <= this: line at GND (pressed)
constexpr uint32_t SAMPLE_MS        = 10;
constexpr int      DEBOUNCE_SAMPLES = 3;      // consecutive flips before reporting
constexpr uint32_t MIN_SEND_GAP_MS  = 50;
constexpr uint32_t HEARTBEAT_MS     = 2000;   // while pressed only
constexpr uint32_t VERIFY_MS        = 40;     // wait for the TX result of an edge
constexpr int      EDGE_RETRIES     = 3;

inline bool     pressed      = false;   // debounced, reported state
inline int      flipCount    = 0;       // consecutive samples disagreeing with `pressed`
inline bool     lastSent     = false;
inline bool     sentOnce     = false;
inline uint32_t lastSampleMs = 0;
inline uint32_t lastSendMs   = 0;
// Delivery check of the last edge frame: the radio's failure count when it
// was sent, and how many re-sends are still allowed (0 = nothing to verify).
inline uint32_t failsAtSend  = 0;
inline int      retriesLeft  = 0;

inline void hardware_init() {
    analogSetPinAttenuation(BUTTON_PIN, ADC_11db);
}

inline bool send() {
    using se::node::gatewayMac;
    using se::node::gatewayKnown;
    if (!gatewayKnown) return false;
    char buf[48];
    int  len = snprintf(buf, sizeof(buf),
        "{\"type\":\"button\",\"pressed\":%s}", pressed ? "true" : "false");
    esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
    return true;
}

// Sample + debounce the line into `pressed`.
inline void sample() {
    bool rawPressed = analogRead(BUTTON_PIN) <= PRESSED_RAW;
    if (rawPressed != pressed) {
        if (++flipCount >= DEBOUNCE_SAMPLES) {
            pressed   = rawPressed;
            flipCount = 0;
            DBG_PRINT("BUTTON %s\n", pressed ? "pressed" : "released");
        }
    } else {
        flipCount = 0;
    }
}

// True when the state should go on air now: a change (rate-limited), the
// first report after boot, a failed edge frame, or the held-down heartbeat.
inline bool dueToSend(uint32_t now) {
    const uint32_t sinceSend = now - lastSendMs;
    if (!sentOnce) return true;
    if (pressed != lastSent) return sinceSend >= MIN_SEND_GAP_MS;
    if (retriesLeft > 0 && sinceSend >= VERIFY_MS) {
        if (se::txFail != failsAtSend) return true;   // radio dropped the edge
        retriesLeft = 0;                              // delivered
    }
    return pressed && sinceSend >= HEARTBEAT_MS;      // silent while released
}

inline void tick(uint32_t now) {
    if (now - lastSampleMs < SAMPLE_MS) return;
    lastSampleMs = now;
    sample();
    if (!dueToSend(now)) return;

    const bool edge = !sentOnce || pressed != lastSent;
    const uint32_t fails = se::txFail;
    if (!send()) return;
    if (edge)                 retriesLeft = EDGE_RETRIES;
    else if (retriesLeft > 0) --retriesLeft;
    failsAtSend = fails;
    sentOnce    = true;
    lastSent    = pressed;
    lastSendMs  = now;
}

}  // namespace button

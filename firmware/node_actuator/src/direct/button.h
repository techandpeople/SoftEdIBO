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
// {"type":"button","pressed":true|false}. The current state is also re-sent
// every HEARTBEAT_MS so the PC recovers from a missed packet (and gets the
// state even if it connects late). Edge reports are spaced MIN_SEND_GAP_MS
// apart so a floating pin (no button wired) cannot flood the radio.
// ---------------------------------------------------------------------------

namespace button {

constexpr int      PRESSED_RAW      = 200;    // <= this: line at GND (pressed)
constexpr uint32_t SAMPLE_MS        = 10;
constexpr int      DEBOUNCE_SAMPLES = 3;      // consecutive flips before reporting
constexpr uint32_t MIN_SEND_GAP_MS  = 50;
constexpr uint32_t HEARTBEAT_MS     = 2000;

inline bool     pressed      = false;   // debounced, reported state
inline int      flipCount    = 0;       // consecutive samples disagreeing with `pressed`
inline bool     lastSent     = false;
inline bool     sentOnce     = false;
inline uint32_t lastSampleMs = 0;
inline uint32_t lastSendMs   = 0;

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

inline void tick(uint32_t now) {
    if (now - lastSampleMs < SAMPLE_MS) return;
    lastSampleMs = now;

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

    bool changed = !sentOnce || pressed != lastSent;
    if (changed ? (sentOnce && now - lastSendMs < MIN_SEND_GAP_MS)
                : (now - lastSendMs < HEARTBEAT_MS)) return;

    if (send()) {
        sentOnce   = true;
        lastSent   = pressed;
        lastSendMs = now;
    }
}

}  // namespace button

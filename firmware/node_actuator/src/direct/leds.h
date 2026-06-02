#pragma once
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

#include "pins.h"

// WS2812 ring control for node_direct. Driven by the "set_led" ESP-NOW command
// (see commands.h). Animation is non-blocking: the recv callback only stores
// the target colour/pattern; loop() calls update() which refreshes the strip
// at a fixed cadence (strip.show() blocks IRQs, so we don't call it every loop).

namespace leds {

enum Pattern : uint8_t { OFF, SOLID, BLINK, PULSE, MANUAL };

inline Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRB + NEO_KHZ800);

inline uint8_t  r_ = 0, g_ = 0, b_ = 0;
inline Pattern  pattern_  = OFF;
inline uint32_t period_   = 1000;   // ms per blink/pulse cycle
inline int32_t  cycles_   = -1;     // remaining cycles; <0 = run forever
inline uint32_t start_    = 0;      // millis() when the pattern began
inline uint32_t lastShow_ = 0;

constexpr uint32_t REFRESH_MS = 25;

inline void writeAll(uint8_t r, uint8_t g, uint8_t b) {
    for (int i = 0; i < NUM_LEDS; i++) strip.setPixelColor(i, strip.Color(r, g, b));
    strip.show();
}

inline void hardware_init() {
    strip.begin();
    strip.setBrightness(255);
    writeAll(0, 0, 0);
}

inline Pattern patternFromStr(const char* s) {
    if (strcmp(s, "off")   == 0) return OFF;
    if (strcmp(s, "blink") == 0) return BLINK;
    if (strcmp(s, "pulse") == 0) return PULSE;
    return SOLID;   // "solid" / "on" / anything else
}

// Apply a new LED command. count<=0 means run forever (for blink/pulse).
inline void set(uint8_t r, uint8_t g, uint8_t b,
                Pattern p, uint32_t period, int32_t count) {
    r_ = r; g_ = g; b_ = b;
    pattern_ = p;
    period_  = period ? period : 1000;
    cycles_  = count > 0 ? count : -1;
    start_   = millis();
    if      (p == OFF)   writeAll(0, 0, 0);
    else if (p == SOLID) writeAll(r, g, b);
    // BLINK / PULSE are rendered by update().
}

// Set a single pixel (used by the LED test panel). Switches to MANUAL mode so
// update() leaves the individually-set colours alone.
inline void setPixel(int i, uint8_t r, uint8_t g, uint8_t b) {
    if (i < 0 || i >= NUM_LEDS) return;
    pattern_ = MANUAL;
    strip.setPixelColor(i, strip.Color(r, g, b));
    strip.show();
}

inline void update() {
    // Static modes (incl. per-pixel MANUAL) need no animation.
    if (pattern_ == OFF || pattern_ == SOLID || pattern_ == MANUAL) return;

    uint32_t now = millis();
    if (now - lastShow_ < REFRESH_MS) return;
    lastShow_ = now;

    uint32_t elapsed = now - start_;
    if (cycles_ >= 0 && elapsed >= static_cast<uint32_t>(cycles_) * period_) {
        pattern_ = OFF;
        writeAll(0, 0, 0);
        return;
    }

    uint32_t t = elapsed % period_;     // position within the current cycle
    float scale;
    if (pattern_ == BLINK) {
        scale = (t < period_ / 2) ? 1.0f : 0.0f;
    } else {                             // PULSE — triangle ramp 0 -> 1 -> 0
        float frac = static_cast<float>(t) / period_;
        scale = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;
    }
    writeAll(static_cast<uint8_t>(r_ * scale),
             static_cast<uint8_t>(g_ * scale),
             static_cast<uint8_t>(b_ * scale));
}

}  // namespace leds

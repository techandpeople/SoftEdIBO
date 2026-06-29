#pragma once
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

#include "pins.h"

// LED ring control for node_multiplexed. Four independent rings (one 24-LED +
// three 16-LED, see LED_PINS in pins.h) — each on its own data pin, so each is
// a separate Adafruit_NeoPixel strip with its own animation state. Driven by
// the "set_led" ESP-NOW command (see main.cpp); the "ring" field selects one,
// omitted / -1 addresses all four at once. Animation is non-blocking: the recv
// callback only stores each ring's target, and loop() calls update() which
// refreshes at a fixed cadence (strip.show() blocks IRQs, so not every loop).

namespace leds {

enum Pattern : uint8_t { OFF, SOLID, BLINK, PULSE, MANUAL };

// Pixel type is chosen at flash time. RGB rings (e.g. Adafruit 1586) send 3
// bytes/pixel; RGBW rings (e.g. Adafruit 2862, SK6812) send 4. Build the
// matching env (-DLED_RGBW) for the rings that are actually wired — the byte
// count differs, so an RGB build drives an RGBW ring with shifted colours and
// vice versa. The colour code below is unchanged: Color() leaves W at 0.
#ifdef LED_RGBW
constexpr uint16_t PIXEL_TYPE = NEO_GRBW + NEO_KHZ800;
#else
constexpr uint16_t PIXEL_TYPE = NEO_GRB + NEO_KHZ800;
#endif

inline Adafruit_NeoPixel strips[NUM_RINGS] = {
    Adafruit_NeoPixel(RING_LEDS[0], LED_PINS[0], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[1], LED_PINS[1], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[2], LED_PINS[2], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[3], LED_PINS[3], PIXEL_TYPE),
};

// Per-ring animation state — each ring animates independently of the others.
struct Ring {
    uint8_t  r = 0, g = 0, b = 0;
    Pattern  pattern = OFF;
    uint32_t period  = 1000;   // ms per blink/pulse cycle
    int32_t  cycles  = -1;     // remaining cycles; <0 = run forever
    uint32_t start   = 0;      // millis() when the pattern began
};

inline Ring     rings[NUM_RINGS];
inline uint32_t lastShow_ = 0;

constexpr uint32_t REFRESH_MS = 25;

// Build a strip colour from sRGB channels, applying perceptual gamma. The PC
// sends plain sRGB hex (what the on-screen colour picker shows), but NeoPixels
// drive a *linear* PWM, so without correction mid-tones look too bright and hues
// drift from the picker. gamma8() maps each channel back to the perceived value.
// Color() packs into a fixed format regardless of a strip's NEO_GRB(W) order, so
// any instance gives the same value — mirrors node_direct.
inline uint32_t srgbColor(uint8_t r, uint8_t g, uint8_t b) {
    return Adafruit_NeoPixel::Color(Adafruit_NeoPixel::gamma8(r),
                                    Adafruit_NeoPixel::gamma8(g),
                                    Adafruit_NeoPixel::gamma8(b));
}

// Fill one ring with a colour and push it out.
inline void showRing(int ring, uint32_t c) {
    Adafruit_NeoPixel& s = strips[ring];
    for (uint16_t i = 0; i < s.numPixels(); i++) s.setPixelColor(i, c);
    s.show();
}

inline void hardware_init() {
    for (int k = 0; k < NUM_RINGS; k++) {
        strips[k].begin();
        strips[k].setBrightness(255);
        strips[k].clear();
        strips[k].show();
    }
}

inline Pattern patternFromStr(const char* s) {
    if (strcmp(s, "off")   == 0) return OFF;
    if (strcmp(s, "blink") == 0) return BLINK;
    if (strcmp(s, "pulse") == 0) return PULSE;
    return SOLID;   // "solid" / "on" / anything else
}

// Apply a new LED command to ring k (0..NUM_RINGS-1), or to every ring when
// ring < 0. count<=0 means run forever (for blink/pulse).
inline void set(int ring, uint8_t r, uint8_t g, uint8_t b,
                Pattern p, uint32_t period, int32_t count) {
    if (ring >= NUM_RINGS) return;
    int lo = (ring < 0) ? 0 : ring;
    int hi = (ring < 0) ? NUM_RINGS : ring + 1;
    uint32_t now = millis();
    for (int k = lo; k < hi; k++) {
        Ring& R = rings[k];
        R.r = r; R.g = g; R.b = b;
        R.pattern = p;
        R.period  = period ? period : 1000;
        R.cycles  = count > 0 ? count : -1;
        R.start   = now;
        if      (p == OFF)   showRing(k, 0);
        else if (p == SOLID) showRing(k, srgbColor(r, g, b));
        // BLINK / PULSE are rendered by update().
    }
}

// Set a single pixel on ring k (used by the LED test panel). ring < 0 defaults
// to ring 0. Switches that ring to MANUAL so update() leaves it alone.
inline void setPixel(int ring, int i, uint8_t r, uint8_t g, uint8_t b) {
    int k = (ring < 0) ? 0 : ring;
    if (k < 0 || k >= NUM_RINGS) return;
    if (i < 0 || i >= static_cast<int>(strips[k].numPixels())) return;
    rings[k].pattern = MANUAL;
    strips[k].setPixelColor(i, srgbColor(r, g, b));
    strips[k].show();
}

inline void update() {
    // Animate only rings currently in BLINK/PULSE; static (OFF/SOLID) and
    // per-pixel MANUAL rings need no refresh.
    bool anyAnim = false;
    for (int k = 0; k < NUM_RINGS; k++) {
        if (rings[k].pattern == BLINK || rings[k].pattern == PULSE) { anyAnim = true; break; }
    }
    if (!anyAnim) return;

    uint32_t now = millis();
    if (now - lastShow_ < REFRESH_MS) return;
    lastShow_ = now;

    for (int k = 0; k < NUM_RINGS; k++) {
        Ring& R = rings[k];
        if (R.pattern != BLINK && R.pattern != PULSE) continue;

        uint32_t elapsed = now - R.start;
        if (R.cycles >= 0 && elapsed >= static_cast<uint32_t>(R.cycles) * R.period) {
            R.pattern = OFF;
            showRing(k, 0);
            continue;
        }

        uint32_t t = elapsed % R.period;     // position within the current cycle
        float scale;
        if (R.pattern == BLINK) {
            scale = (t < R.period / 2) ? 1.0f : 0.0f;
        } else {                              // PULSE — triangle ramp 0 -> 1 -> 0
            float frac = static_cast<float>(t) / R.period;
            scale = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;
        }
        showRing(k, srgbColor(static_cast<uint8_t>(R.r * scale),
                              static_cast<uint8_t>(R.g * scale),
                              static_cast<uint8_t>(R.b * scale)));
    }
}

}  // namespace leds

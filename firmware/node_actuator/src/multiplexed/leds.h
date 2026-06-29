#pragma once
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

#include "pins.h"

// LED ring control for node_multiplexed. Four independent rings (one 24-LED +
// three 16-LED, see LED_PINS in pins.h) — each on its own data pin, so each is a
// separate Adafruit_NeoPixel strip with its own animation state. Driven by the
// "set_led" / "set_led_halves" ESP-NOW commands (see main.cpp); the "ring" field
// selects one, omitted / -1 addresses all four at once. Rendering is non-blocking
// AND fully deferred: the recv callback only updates each ring's per-pixel target
// buffer plus its animation state, and loop()'s update() is the ONLY place show()
// runs. show() bit-bangs a strip with interrupts disabled, so driving it from the
// ESP-NOW receive task — once per pixel for a split-ring repaint — starved the
// radio and reset the node. One show() per ring per loop, off the receive task,
// keeps the link up.

namespace leds {

enum Pattern : uint8_t { STATIC, BLINK, PULSE };

// Max arcs a "set_led_halves" command may split a ring into, and the largest ring
// (the 24-LED one) — sizes the per-ring colour buffers.
constexpr int MAX_SEGMENTS  = 8;
constexpr int MAX_RING_LEDS = 24;

// Pixel type is chosen at flash time. RGB rings (e.g. Adafruit 1586) send 3
// bytes/pixel; RGBW rings (e.g. Adafruit 2862, SK6812) send 4. Build the matching
// env (-DLED_RGBW) for the rings that are actually wired — the byte count differs,
// so an RGB build drives an RGBW ring with shifted colours and vice versa. The
// colour code below is unchanged: Color() leaves W at 0.
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

// Per-ring animation state. The base buffer holds each pixel's target colour
// (plain sRGB, pre-gamma); update() renders it scaled by the current animation
// level. A whole-ring colour fills every entry the same; a split ring fills
// contiguous arcs; the test panel sets one entry.
struct Ring {
    uint8_t  baseR[MAX_RING_LEDS] = {0};
    uint8_t  baseG[MAX_RING_LEDS] = {0};
    uint8_t  baseB[MAX_RING_LEDS] = {0};
    Pattern  pattern = STATIC;
    uint32_t period  = 1000;   // ms per blink/pulse cycle
    int32_t  cycles  = -1;     // remaining cycles; <0 = run forever
    uint32_t start   = 0;      // millis() when the pattern began
    bool     dirty   = true;   // a STATIC change still owes one show()
};

inline Ring     rings[NUM_RINGS];
inline uint32_t lastShow_ = 0;

constexpr uint32_t REFRESH_MS = 25;

// Build a strip colour from sRGB channels, applying perceptual gamma. The PC sends
// plain sRGB hex (what the on-screen colour picker shows), but NeoPixels drive a
// *linear* PWM, so without correction mid-tones look too bright and hues drift from
// the picker. gamma8() maps each channel back to the perceived value. Color() packs
// into a fixed format regardless of a strip's NEO_GRB(W) order, so any instance
// gives the same value — mirrors node_direct.
inline uint32_t srgbColor(uint8_t r, uint8_t g, uint8_t b) {
    return Adafruit_NeoPixel::Color(Adafruit_NeoPixel::gamma8(r),
                                    Adafruit_NeoPixel::gamma8(g),
                                    Adafruit_NeoPixel::gamma8(b));
}

// Parse "#RRGGBB" into channels (0,0,0 on anything malformed). Shared by the
// "set_led" and "set_led_halves" command parsers.
inline void parseHexColor(const char* col, uint8_t& r, uint8_t& g, uint8_t& b) {
    r = g = b = 0;
    if (col && col[0] == '#' && strlen(col) >= 7) {
        long v = strtol(col + 1, nullptr, 16);
        r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
    }
}

// Render ring k's base buffer scaled by `scale` (0..1) and push one frame. The
// only caller of show() outside hardware_init — always reached from loop().
inline void renderRing_(int k, float scale) {
    Adafruit_NeoPixel& s = strips[k];
    Ring& R = rings[k];
    uint16_t n = s.numPixels();
    for (uint16_t i = 0; i < n; i++)
        s.setPixelColor(i, srgbColor(static_cast<uint8_t>(R.baseR[i] * scale),
                                     static_cast<uint8_t>(R.baseG[i] * scale),
                                     static_cast<uint8_t>(R.baseB[i] * scale)));
    s.show();
}

inline void hardware_init() {
    for (int k = 0; k < NUM_RINGS; k++) {
        strips[k].begin();
        strips[k].setBrightness(255);
        renderRing_(k, 0.0f);   // setup() context — the one show() outside update()
        rings[k].dirty = false;
    }
}

inline Pattern patternFromStr(const char* s) {
    if (strcmp(s, "blink") == 0) return BLINK;
    if (strcmp(s, "pulse") == 0) return PULSE;
    return STATIC;   // "off" (caller passes black) / "solid" / anything else
}

// Resolve a ring argument to the [lo,hi) range it addresses (ring < 0 = all rings;
// out-of-range = empty). Latch the animation mode + mark the range dirty.
inline void applyRange_(int ring, Pattern p, uint32_t period, int32_t count,
                        int& lo, int& hi) {
    lo = (ring < 0) ? 0 : ring;
    hi = (ring < 0) ? NUM_RINGS : ring + 1;
    if (lo < 0) lo = 0;
    if (hi > NUM_RINGS) hi = NUM_RINGS;
    uint32_t now = millis();
    for (int k = lo; k < hi; k++) {
        Ring& R = rings[k];
        R.pattern = p;
        R.period  = period ? period : 1000;
        R.cycles  = count > 0 ? count : -1;
        R.start   = now;
        R.dirty   = true;
    }
}

// Whole ring(s), one colour. ring < 0 = all rings.
inline void set(int ring, uint8_t r, uint8_t g, uint8_t b,
                Pattern p, uint32_t period, int32_t count) {
    if (ring >= NUM_RINGS) return;
    int lo, hi;
    applyRange_(ring, p, period, count, lo, hi);
    for (int k = lo; k < hi; k++) {
        uint16_t n = strips[k].numPixels();
        for (uint16_t i = 0; i < n; i++) {
            rings[k].baseR[i] = r; rings[k].baseG[i] = g; rings[k].baseB[i] = b;
        }
    }
}

// Split ring(s) into `k` equal contiguous arcs. Arc boundaries match node_direct
// (seg = i*k/n over each ring's own pixel count). ring < 0 = all rings.
inline void setSegments(int ring, const uint8_t* r, const uint8_t* g,
                        const uint8_t* b, int k, Pattern p, uint32_t period,
                        int32_t count) {
    if (ring >= NUM_RINGS) return;
    if (k < 1) k = 1;
    if (k > MAX_SEGMENTS) k = MAX_SEGMENTS;
    int lo, hi;
    applyRange_(ring, p, period, count, lo, hi);
    for (int j = lo; j < hi; j++) {
        uint16_t n = strips[j].numPixels();
        for (uint16_t i = 0; i < n; i++) {
            int seg = (int)i * k / n;
            if (seg > k - 1) seg = k - 1;
            rings[j].baseR[i] = r[seg]; rings[j].baseG[i] = g[seg]; rings[j].baseB[i] = b[seg];
        }
    }
}

// Set a single pixel on ring k (used by the LED test panel). ring < 0 defaults to
// ring 0. Static; leaves the ring's other pixels untouched.
inline void setPixel(int ring, int i, uint8_t r, uint8_t g, uint8_t b) {
    int k = (ring < 0) ? 0 : ring;
    if (k < 0 || k >= NUM_RINGS) return;
    if (i < 0 || i >= static_cast<int>(strips[k].numPixels())) return;
    rings[k].baseR[i] = r; rings[k].baseG[i] = g; rings[k].baseB[i] = b;
    rings[k].pattern = STATIC;
    rings[k].dirty   = true;
}

inline void update() {
    uint32_t now = millis();
    bool refreshDue = (now - lastShow_ >= REFRESH_MS);
    bool animated   = false;

    for (int k = 0; k < NUM_RINGS; k++) {
        Ring& R = rings[k];

        if (R.pattern == STATIC) {
            if (R.dirty) { R.dirty = false; renderRing_(k, 1.0f); }
            continue;
        }

        animated = true;
        if (!R.dirty && !refreshDue) continue;
        R.dirty = false;

        uint32_t elapsed = now - R.start;
        if (R.cycles >= 0 && elapsed >= static_cast<uint32_t>(R.cycles) * R.period) {
            // Animation finished: go dark and stay there.
            R.pattern = STATIC;
            for (int i = 0; i < MAX_RING_LEDS; i++) { R.baseR[i] = R.baseG[i] = R.baseB[i] = 0; }
            renderRing_(k, 0.0f);
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
        renderRing_(k, scale);
    }

    if (animated && refreshDue) lastShow_ = now;
}

}  // namespace leds

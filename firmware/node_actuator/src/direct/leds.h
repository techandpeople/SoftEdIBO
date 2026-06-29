#pragma once
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

#include "pins.h"

// WS2812 ring control for node_direct. Driven by the "set_led" / "set_led_halves"
// ESP-NOW commands (see commands.h). Rendering is non-blocking AND fully deferred:
// the recv callback only updates the per-pixel target buffer plus the animation
// state, and loop()'s update() is the ONLY place strip.show() runs. That matters
// because show() bit-bangs the strip with interrupts disabled (~0.7 ms for a
// 24-LED RGBW ring); driving it from the ESP-NOW receive task — once per pixel for
// a split-ring repaint — starved the radio and reset the node. One show() per loop,
// off the receive task, keeps the link up.

namespace leds {

enum Pattern : uint8_t { STATIC, BLINK, PULSE };

// Max arcs a "set_led_halves" command may split the ring into.
constexpr int MAX_SEGMENTS = 8;

// Pixel type is chosen at flash time. RGB rings (e.g. Adafruit 1586) send 3
// bytes/pixel; RGBW rings (e.g. Adafruit 2862, SK6812) send 4. Build the matching
// env (-DLED_RGBW) for the ring that's actually wired — the byte count differs, so
// an RGB build drives an RGBW ring with shifted colours and vice versa. The colour
// code below is unchanged: Color()/setPixelColor leave W at 0.
#ifdef LED_RGBW
inline Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRBW + NEO_KHZ800);
#else
inline Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRB + NEO_KHZ800);
#endif

// Per-pixel target colour (plain sRGB, pre-gamma). update() renders this buffer —
// scaled by the current animation level — to the strip. A whole-ring colour fills
// every entry the same; a split ring fills contiguous arcs; the test panel sets a
// single entry. Animation (blink/pulse) scales the whole buffer together.
inline uint8_t  baseR_[NUM_LEDS] = {0};
inline uint8_t  baseG_[NUM_LEDS] = {0};
inline uint8_t  baseB_[NUM_LEDS] = {0};
inline Pattern  pattern_  = STATIC;
inline uint32_t period_   = 1000;   // ms per blink/pulse cycle
inline int32_t  cycles_   = -1;     // remaining cycles; <0 = run forever
inline uint32_t start_    = 0;      // millis() when the pattern began
inline uint32_t lastShow_ = 0;
inline bool     dirty_    = true;   // a STATIC change still owes one show()

constexpr uint32_t REFRESH_MS = 25;

// Build a strip colour from sRGB channels, applying perceptual gamma. The PC sends
// plain sRGB hex (what the on-screen colour picker shows), but NeoPixels drive a
// *linear* PWM, so without correction mid-tones look too bright and hues drift from
// the picker. gamma8() maps each channel back to the perceived value.
inline uint32_t srgbColor(uint8_t r, uint8_t g, uint8_t b) {
    return strip.Color(Adafruit_NeoPixel::gamma8(r),
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

// Render the base buffer scaled by `scale` (0..1) and push one frame. This is the
// only caller of strip.show() outside hardware_init — always reached from loop(),
// never from the ESP-NOW receive task.
inline void render_(float scale) {
    for (int i = 0; i < NUM_LEDS; i++)
        strip.setPixelColor(i, srgbColor(static_cast<uint8_t>(baseR_[i] * scale),
                                         static_cast<uint8_t>(baseG_[i] * scale),
                                         static_cast<uint8_t>(baseB_[i] * scale)));
    strip.show();
}

inline void hardware_init() {
    strip.begin();
    strip.setBrightness(255);
    render_(0.0f);   // setup() context — the one show() outside loop()/update()
    dirty_ = false;
}

inline Pattern patternFromStr(const char* s) {
    if (strcmp(s, "blink") == 0) return BLINK;
    if (strcmp(s, "pulse") == 0) return PULSE;
    return STATIC;   // "off" (caller passes black) / "solid" / anything else
}

// Latch the animation mode for whatever colours were just written, and mark the
// buffer dirty so update() pushes them on the next loop. count<=0 = run forever.
inline void apply_(Pattern p, uint32_t period, int32_t count) {
    pattern_ = p;
    period_  = period ? period : 1000;
    cycles_  = count > 0 ? count : -1;
    start_   = millis();
    dirty_   = true;
}

// Whole ring, one colour.
inline void setAll(uint8_t r, uint8_t g, uint8_t b,
                   Pattern p, uint32_t period, int32_t count) {
    for (int i = 0; i < NUM_LEDS; i++) { baseR_[i] = r; baseG_[i] = g; baseB_[i] = b; }
    apply_(p, period, count);
}

// Split the ring into `k` equal contiguous arcs (k clamped to 1..NUM_LEDS). Arc
// boundaries match the PC's previous per-pixel mapping (seg = i*k/NUM_LEDS).
inline void setSegments(const uint8_t* r, const uint8_t* g, const uint8_t* b, int k,
                        Pattern p, uint32_t period, int32_t count) {
    if (k < 1) k = 1;
    if (k > NUM_LEDS) k = NUM_LEDS;
    for (int i = 0; i < NUM_LEDS; i++) {
        int seg = i * k / NUM_LEDS;
        if (seg > k - 1) seg = k - 1;
        baseR_[i] = r[seg]; baseG_[i] = g[seg]; baseB_[i] = b[seg];
    }
    apply_(p, period, count);
}

// Set a single pixel (used by the LED test panel). Static; leaves the other
// pixels' colours untouched so the panel can build an image one pixel at a time.
inline void setPixel(int i, uint8_t r, uint8_t g, uint8_t b) {
    if (i < 0 || i >= NUM_LEDS) return;
    baseR_[i] = r; baseG_[i] = g; baseB_[i] = b;
    pattern_ = STATIC;
    dirty_   = true;
}

inline void update() {
    uint32_t now = millis();

    // Static modes (solid / split / per-pixel) only repaint when something changed.
    if (pattern_ == STATIC) {
        if (dirty_) { dirty_ = false; render_(1.0f); }
        return;
    }

    if (!dirty_ && now - lastShow_ < REFRESH_MS) return;
    lastShow_ = now;
    dirty_ = false;

    uint32_t elapsed = now - start_;
    if (cycles_ >= 0 && elapsed >= static_cast<uint32_t>(cycles_) * period_) {
        // Animation finished: go dark and stay there.
        pattern_ = STATIC;
        for (int i = 0; i < NUM_LEDS; i++) { baseR_[i] = baseG_[i] = baseB_[i] = 0; }
        render_(0.0f);
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
    render_(scale);
}

}  // namespace leds

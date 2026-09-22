#pragma once
#include <Arduino.h>
#include <math.h>
#include <Adafruit_NeoPixel.h>

#include "pins.h"

// WS2812 strip control for node_direct. Driven by the "set_led" / "set_led_halves"
// / "set_led_pixels" / "led_config" ESP-NOW commands (see commands.h). Rendering is
// non-blocking AND fully deferred: the recv callback only updates the per-pixel
// target buffer plus the animation state, and loop()'s update() is the ONLY place
// strip.show() runs. That matters because show() bit-bangs the strip with
// interrupts disabled (~2 ms for 68 pixels); driving it from the ESP-NOW receive
// task - once per pixel for a split repaint - starved the radio and reset the
// node. One show() per loop, off the receive task, keeps the link up.
//
// Geometry: the strip is a loop of STRIP_SLOTS equal positions; pixel i sits at
// slot i and the LED_GAP_SLOTS after the last pixel are the empty seam (see
// pins.h). Everything angular (arc boundaries, comet heads, the "angle" offset)
// is computed over slots so it stays continuous across the seam; only real
// pixels are painted.
//
// Render is a 3-stage pipeline evaluated every frame (render_()):
//   1. target + pattern -> animatedTo[i]   (STATIC/BLINK/PULSE scale `base`; COMET
//      paints k rotating comets from the stored segment colours seg[])
//   2. cross-fade        -> out[i] = lerp(from[i], animatedTo[i], fadeT)
//      `from` is a snapshot of what was on screen when the last command arrived, so
//      every colour/pattern change fades in smoothly instead of snapping.
//   3. gamma + show.

namespace leds {

enum Pattern : uint8_t { STATIC, BLINK, PULSE, COMET, FADE };

// Max arcs/colours a "set_led_halves" command may carry (also the max comet count).
constexpr int MAX_SEGMENTS = 8;

// Colours a "set_led_pixels" frame may carry: its mask holds 2 bits per pixel.
constexpr int MAX_MASK_COLORS = 4;

// Default cross-fade time (ms) when a command omits "fade_ms". Matches the PC.
constexpr uint32_t DEFAULT_FADE_MS = 250;

// Pixel type is chosen at flash time. RGB rings (e.g. Adafruit 1586) send 3
// bytes/pixel; RGBW rings (e.g. Adafruit 2862, SK6812) send 4. Build the matching
// env (-DLED_RGBW) for the ring that's actually wired - the byte count differs, so
// an RGB build drives an RGBW ring with shifted colours and vice versa. The colour
// code below is unchanged: Color()/setPixelColor leave W at 0.
#ifdef LED_RGBW
inline Adafruit_NeoPixel strip(NUM_PIXELS, LED_PIN, NEO_GRBW + NEO_KHZ800);
#define LED_RGBW_JSON "true"    // reported in ready/pong so the OTA picker auto-selects the RGBW bin
#else
inline Adafruit_NeoPixel strip(NUM_PIXELS, LED_PIN, NEO_GRB + NEO_KHZ800);
#define LED_RGBW_JSON "false"
#endif

// Per-pixel target colour (plain sRGB, pre-gamma). The STATIC/BLINK/PULSE patterns
// render this buffer (a whole-ring colour fills every entry; a split ring fills
// contiguous arcs; the test panel sets a single entry). COMET ignores `base` and
// paints from seg[]/segCount_ instead.
inline uint8_t  baseR_[NUM_PIXELS] = {0};
inline uint8_t  baseG_[NUM_PIXELS] = {0};
inline uint8_t  baseB_[NUM_PIXELS] = {0};

// Segment colours (one whole-ring colour, or the k arc colours of a split ring).
// COMET paints one comet per segment colour, equally spaced around the ring.
inline uint8_t  segR_[MAX_SEGMENTS] = {0};
inline uint8_t  segG_[MAX_SEGMENTS] = {0};
inline uint8_t  segB_[MAX_SEGMENTS] = {0};
inline int      segCount_ = 1;

// Angular offset of the split/comet, as a fraction of the ring (0..1). Rotates the
// arc boundaries (so "halves" can sit top/bottom instead of left/right) and the
// comet start position. Set from the "angle" command field (degrees / 360).
inline float    segOffset_ = 0.0f;

// Second colour for the FADE pattern: the whole ring cross-fades base_ (c1)
// <-> fadeTo_ (c2) each period. Unused by the other patterns.
inline uint8_t  fadeToR_ = 0, fadeToG_ = 0, fadeToB_ = 0;

// Cross-fade source: a snapshot of the displayed output the instant a new command
// landed, and the rolling displayed buffer that feeds the next snapshot.
inline uint8_t  fromR_[NUM_PIXELS] = {0};
inline uint8_t  fromG_[NUM_PIXELS] = {0};
inline uint8_t  fromB_[NUM_PIXELS] = {0};
inline uint8_t  dispR_[NUM_PIXELS] = {0};
inline uint8_t  dispG_[NUM_PIXELS] = {0};
inline uint8_t  dispB_[NUM_PIXELS] = {0};

inline Pattern  pattern_  = STATIC;
inline uint32_t period_   = 1000;   // ms per blink/pulse cycle or comet revolution
inline int32_t  cycles_   = -1;     // remaining cycles; <0 = run forever
inline uint32_t start_    = 0;      // millis() when the pattern began
inline uint32_t fadeStart_ = 0;     // millis() when the current cross-fade began
inline uint32_t fadeMs_   = 0;      // cross-fade duration of the current change
inline bool     fadeWasActive_ = false;
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

inline uint8_t lerp8_(uint8_t a, uint8_t b, float t) {
    int v = (int)lroundf((float)a + ((int)b - (int)a) * t);
    return (uint8_t)constrain(v, 0, 255);
}

inline Pattern patternFromStr(const char* s) {
    if (strcmp(s, "blink") == 0) return BLINK;
    if (strcmp(s, "pulse") == 0) return PULSE;
    if (strcmp(s, "comet") == 0) return COMET;
    if (strcmp(s, "fade")  == 0) return FADE;
    return STATIC;   // "off" (caller passes black) / "solid" / anything else
}

// Stage 1: paint k rotating comets into the animated buffer. Comet j's head sits at
// head + j*N/k and trails a fractional 1->0 tail behind it (lower indices), coloured
// by seg[j]. A fractional head keeps the motion smooth between pixels.
inline void renderComet_(uint8_t* oR, uint8_t* oG, uint8_t* oB, uint32_t now) {
    int k = segCount_ < 1 ? 1 : (segCount_ > MAX_SEGMENTS ? MAX_SEGMENTS : segCount_);
    // Heads travel over the STRIP_SLOTS loop (so they cross the seam smoothly);
    // pixel i sits at slot i.
    float head    = (float)((now - start_) % period_) / (float)period_ * STRIP_SLOTS
                    + segOffset_ * STRIP_SLOTS;   // angular offset rotates the comet start
    float spacing = (float)STRIP_SLOTS / k;
    float tail    = spacing * 0.7f;
    if (tail > STRIP_SLOTS / 3.0f) tail = STRIP_SLOTS / 3.0f;
    if (tail < 1.0f) tail = 1.0f;
    for (int i = 0; i < NUM_PIXELS; i++) {
        float best = 0.0f; int bestj = 0;
        for (int j = 0; j < k; j++) {
            float h = head + j * spacing;
            // distance slot i sits *behind* comet head h (wrapped to [0,SLOTS))
            float d = fmodf(h - i + 2.0f * STRIP_SLOTS, (float)STRIP_SLOTS);
            float b = 1.0f - d / tail;
            if (b > best) { best = b; bestj = j; }
        }
        oR[i] = (uint8_t)(segR_[bestj] * best);
        oG[i] = (uint8_t)(segG_[bestj] * best);
        oB[i] = (uint8_t)(segB_[bestj] * best);
    }
}

// Stage 1: resolve the target colour of each pixel for the current pattern/time.
inline void computeAnimated_(uint8_t* oR, uint8_t* oG, uint8_t* oB, uint32_t now) {
    if (pattern_ == COMET) { renderComet_(oR, oG, oB, now); return; }
    if (pattern_ == FADE) {   // whole ring cross-fades base (c1) <-> fadeTo (c2)
        uint32_t t = (now - start_) % period_;
        float frac = (float)t / period_;
        float tri  = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;   // 0 -> 1 -> 0
        for (int i = 0; i < NUM_PIXELS; i++) {
            oR[i] = lerp8_(baseR_[i], fadeToR_, tri);
            oG[i] = lerp8_(baseG_[i], fadeToG_, tri);
            oB[i] = lerp8_(baseB_[i], fadeToB_, tri);
        }
        return;
    }
    float scale = 1.0f;
    if (pattern_ == BLINK || pattern_ == PULSE) {
        uint32_t t = (now - start_) % period_;
        if (pattern_ == BLINK) {
            scale = (t < period_ / 2) ? 1.0f : 0.0f;
        } else {                               // PULSE - triangle ramp 0 -> 1 -> 0
            float frac = (float)t / period_;
            scale = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;
        }
    }
    for (int i = 0; i < NUM_PIXELS; i++) {
        oR[i] = (uint8_t)(baseR_[i] * scale);
        oG[i] = (uint8_t)(baseG_[i] * scale);
        oB[i] = (uint8_t)(baseB_[i] * scale);
    }
}

inline float fadeProgress_(uint32_t now) {
    if (fadeMs_ == 0) return 1.0f;
    int32_t e = (int32_t)(now - fadeStart_);
    if (e <= 0) return 0.0f;
    return ((uint32_t)e >= fadeMs_) ? 1.0f : (float)e / (float)fadeMs_;
}

// Stages 1-3: animate, cross-fade from the snapshot, remember the output, gamma+show.
// The only caller of strip.show() outside hardware_init - always reached from loop().
inline void render_(uint32_t now) {
    uint8_t aR[NUM_PIXELS], aG[NUM_PIXELS], aB[NUM_PIXELS];
    computeAnimated_(aR, aG, aB, now);
    float ft = fadeProgress_(now);
    for (int i = 0; i < NUM_PIXELS; i++) {
        uint8_t r = lerp8_(fromR_[i], aR[i], ft);
        uint8_t g = lerp8_(fromG_[i], aG[i], ft);
        uint8_t b = lerp8_(fromB_[i], aB[i], ft);
        dispR_[i] = r; dispG_[i] = g; dispB_[i] = b;
        strip.setPixelColor(i, srgbColor(r, g, b));
    }
    strip.show();
}

inline void hardware_init() {
    strip.begin();
    strip.setBrightness(255);
    render_(millis());   // setup() context - the one show() outside loop()/update()
    dirty_ = false;
}

// Snapshot what is on screen and start a cross-fade towards whatever the caller is
// about to write. Latch the animation mode + timing. count<=0 = run forever.
inline void apply_(Pattern p, uint32_t period, int32_t count, uint32_t fadeMs) {
    for (int i = 0; i < NUM_PIXELS; i++) {
        fromR_[i] = dispR_[i]; fromG_[i] = dispG_[i]; fromB_[i] = dispB_[i];
    }
    pattern_   = p;
    period_    = period ? period : 1000;
    cycles_    = count > 0 ? count : -1;
    start_     = millis();
    fadeStart_ = start_;
    fadeMs_    = fadeMs;
    dirty_     = true;
}

// Whole ring, one colour.
inline void setAll(uint8_t r, uint8_t g, uint8_t b,
                   Pattern p, uint32_t period, int32_t count,
                   uint32_t fadeMs = DEFAULT_FADE_MS, float offset = 0.0f) {
    for (int i = 0; i < NUM_PIXELS; i++) { baseR_[i] = r; baseG_[i] = g; baseB_[i] = b; }
    segR_[0] = r; segG_[0] = g; segB_[0] = b; segCount_ = 1;
    segOffset_ = offset;
    apply_(p, period, count, fadeMs);
}

// Whole ring cross-fading between two colours: base (c1) <-> fadeTo (c2) as a
// triangle 0->1->0 over `period` (one c1->c2->c1 cycle). count<=0 = run forever;
// a bounded fade rests on c1 when done (see update()). Moves the PC's old
// per-frame colour stream onto the node - one frame instead of ~20/cycle.
inline void setFade(uint8_t r, uint8_t g, uint8_t b,
                    uint8_t r2, uint8_t g2, uint8_t b2,
                    uint32_t period, int32_t count,
                    uint32_t fadeMs = DEFAULT_FADE_MS, float offset = 0.0f) {
    for (int i = 0; i < NUM_PIXELS; i++) { baseR_[i] = r; baseG_[i] = g; baseB_[i] = b; }
    segR_[0] = r; segG_[0] = g; segB_[0] = b; segCount_ = 1;
    fadeToR_ = r2; fadeToG_ = g2; fadeToB_ = b2;
    segOffset_ = offset;
    apply_(FADE, period, count, fadeMs);
}

// Split the loop into `k` equal contiguous arcs (k clamped to 1..MAX_SEGMENTS),
// rotated by `offset` (fraction of the loop) so the split can sit at any angle.
// Arcs are measured in slots (the seam gap belongs to the last arc's tail), so
// e.g. quarters of a 69-slot strip are 17/17/17/17 pixels plus the dark seam.
// Keeps the k colours for COMET (one comet per arc colour).
inline void setSegments(const uint8_t* r, const uint8_t* g, const uint8_t* b, int k,
                        Pattern p, uint32_t period, int32_t count,
                        uint32_t fadeMs = DEFAULT_FADE_MS, float offset = 0.0f) {
    if (k < 1) k = 1;
    if (k > NUM_PIXELS) k = NUM_PIXELS;
    if (k > MAX_SEGMENTS) k = MAX_SEGMENTS;
    for (int j = 0; j < k; j++) { segR_[j] = r[j]; segG_[j] = g[j]; segB_[j] = b[j]; }
    segCount_ = k;
    segOffset_ = offset;
    int off = ((int)lroundf(offset * STRIP_SLOTS)) % STRIP_SLOTS;
    for (int i = 0; i < NUM_PIXELS; i++) {
        int idx = ((i - off) % STRIP_SLOTS + STRIP_SLOTS) % STRIP_SLOTS;
        int seg = idx * k / STRIP_SLOTS;
        if (seg > k - 1) seg = k - 1;
        baseR_[i] = r[seg]; baseG_[i] = g[seg]; baseB_[i] = b[seg];
    }
    apply_(p, period, count, fadeMs);
}

// Decode pixel i's colour index from a "set_led_pixels" mask: hex text, two
// pixels per character, pixel i in bits (i % 2) * 2 of character i / 2. A short
// or malformed mask reads as index 0 (the first colour, the background). Mirrors
// src/core/led_geometry.py encode_pixel_mask().
inline int maskCode_(const char* mask, int i) {
    if (!mask) return 0;
    int ci = i / 2;
    for (int j = 0; j <= ci; j++) if (mask[j] == '\0') return 0;
    char c = mask[ci];
    int v = (c >= '0' && c <= '9') ? c - '0'
          : (c >= 'a' && c <= 'f') ? c - 'a' + 10
          : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : 0;
    return (v >> ((i % 2) * 2)) & 3;
}

// Paint an arbitrary per-pixel selection in ONE frame: pixel i takes colour
// mask-index(i) of the k colours (an index past k goes dark). This is how the
// PC lights scattered pixels of a zone (the zone/sync fill blocks) without a
// per-pixel burst. No angle offset: the PC already resolved pixel indices from
// the skin's geometry. STATIC/BLINK/PULSE animate the whole selection.
inline void setPixels(const uint8_t* r, const uint8_t* g, const uint8_t* b, int k,
                      const char* mask, Pattern p, uint32_t period, int32_t count,
                      uint32_t fadeMs = DEFAULT_FADE_MS) {
    if (k < 1) k = 1;
    if (k > MAX_MASK_COLORS) k = MAX_MASK_COLORS;
    segR_[0] = r[0]; segG_[0] = g[0]; segB_[0] = b[0]; segCount_ = 1;
    for (int i = 0; i < NUM_PIXELS; i++) {
        int c = maskCode_(mask, i);
        if (c < k) { baseR_[i] = r[c]; baseG_[i] = g[c]; baseB_[i] = b[c]; }
        else       { baseR_[i] = 0;    baseG_[i] = 0;    baseB_[i] = 0;    }
    }
    if (p == COMET || p == FADE) p = STATIC;   // meaningless on a pixel mask
    apply_(p, period, count, fadeMs);
}

// Brightness cap (1..255) for everything rendered from now on. Board state the PC
// pushes from the skin's led_layout ("led_config"): the cut strip can draw several
// times what the old ring did, so a skin may cap it. Adafruit scales at
// setPixelColor time and render_() rewrites every pixel each frame, so the cap
// applies cleanly to the current look too.
inline void setBrightness(uint8_t b) {
    strip.setBrightness(b ? b : 1);
    dirty_ = true;
}

// Set a single pixel (used by the LED test panel). Static; leaves the other
// pixels' colours untouched so the panel can build an image one pixel at a time.
// Cross-fades the changed pixel in.
inline void setPixel(int i, uint8_t r, uint8_t g, uint8_t b,
                     uint32_t fadeMs = DEFAULT_FADE_MS) {
    if (i < 0 || i >= NUM_PIXELS) return;
    for (int j = 0; j < NUM_PIXELS; j++) {
        fromR_[j] = dispR_[j]; fromG_[j] = dispG_[j]; fromB_[j] = dispB_[j];
    }
    baseR_[i] = r; baseG_[i] = g; baseB_[i] = b;
    pattern_   = STATIC;
    fadeStart_ = millis();
    fadeMs_    = fadeMs;
    dirty_     = true;
}

inline void update() {
    uint32_t now = millis();
    // SIGNED diffs below: set_led runs on the ESP-NOW receive task, so a start
    // time can land a hair AFTER the `now` cached here.
    bool fading = fadeMs_ > 0 && (int32_t)(now - fadeStart_) < (int32_t)fadeMs_;
    if (!fading && fadeWasActive_) { fadeWasActive_ = false; dirty_ = true; }
    if (fading) fadeWasActive_ = true;

    // Static modes (solid / split / per-pixel) only repaint when something changed
    // or while a cross-fade is still running.
    if (pattern_ == STATIC && !fading) {
        if (dirty_) { dirty_ = false; render_(now); }
        return;
    }

    if (!dirty_ && now - lastShow_ < REFRESH_MS) return;
    lastShow_ = now;
    dirty_ = false;

    // A bounded animation that has run its cycles goes dark and stays there.
    if (pattern_ != STATIC && cycles_ >= 0) {
        int32_t elapsed = (int32_t)(now - start_);
        if (elapsed >= 0 && (uint32_t)elapsed >= (uint32_t)cycles_ * period_) {
            bool wasFade = (pattern_ == FADE);
            pattern_ = STATIC;
            if (!wasFade) {   // FADE rests on its base colour (c1); others go dark
                for (int i = 0; i < NUM_PIXELS; i++) { baseR_[i] = baseG_[i] = baseB_[i] = 0; }
                segCount_ = 1; segR_[0] = segG_[0] = segB_[0] = 0;
            }
            fadeMs_ = 0;   // snap to the resting colour
            render_(now);
            return;
        }
    }

    render_(now);
}

}  // namespace leds

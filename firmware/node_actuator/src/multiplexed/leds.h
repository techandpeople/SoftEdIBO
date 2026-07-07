#pragma once
#include <Arduino.h>
#include <math.h>
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
//
// Render is a 3-stage pipeline per ring evaluated every frame (renderRing_()):
//   1. target + pattern -> animatedTo[i]   (STATIC/BLINK/PULSE scale `base`; COMET
//      paints k rotating comets from the stored segment colours seg[])
//   2. cross-fade        -> out[i] = lerp(from[i], animatedTo[i], fadeT)
//      `from` is a snapshot of what was on the ring when the last command arrived,
//      so every colour/pattern change fades in smoothly instead of snapping.
//   3. gamma + show.

namespace leds {

enum Pattern : uint8_t { STATIC, BLINK, PULSE, COMET, FADE };

// Max arcs/colours a "set_led_halves" command may carry (also the max comet count),
// and the largest ring (the 24-LED one) — sizes the per-ring colour buffers.
constexpr int MAX_SEGMENTS  = 8;
constexpr int MAX_RING_LEDS = 24;

// Default cross-fade time (ms) when a command omits "fade_ms". Matches the PC.
constexpr uint32_t DEFAULT_FADE_MS = 250;

// Pixel type is chosen at flash time. RGB rings (e.g. Adafruit 1586) send 3
// bytes/pixel; RGBW rings (e.g. Adafruit 2862, SK6812) send 4. Build the matching
// env (-DLED_RGBW) for the rings that are actually wired — the byte count differs,
// so an RGB build drives an RGBW ring with shifted colours and vice versa. The
// colour code below is unchanged: Color() leaves W at 0.
#ifdef LED_RGBW
constexpr uint16_t PIXEL_TYPE = NEO_GRBW + NEO_KHZ800;
#define LED_RGBW_JSON "true"    // reported in ready/pong so the OTA picker auto-selects the RGBW bin
#else
constexpr uint16_t PIXEL_TYPE = NEO_GRB + NEO_KHZ800;
#define LED_RGBW_JSON "false"
#endif

inline Adafruit_NeoPixel strips[NUM_RINGS] = {
    Adafruit_NeoPixel(RING_LEDS[0], LED_PINS[0], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[1], LED_PINS[1], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[2], LED_PINS[2], PIXEL_TYPE),
    Adafruit_NeoPixel(RING_LEDS[3], LED_PINS[3], PIXEL_TYPE),
};

// Per-ring animation state. `base` holds each pixel's target colour for the
// STATIC/BLINK/PULSE patterns (whole-ring fill or split arcs); `seg`/`segCount`
// hold the segment colours COMET paints one comet each from. `from`/`disp` carry
// the cross-fade: `disp` is the rolling displayed output, snapshotted into `from`
// the instant a new command lands so the change fades in.
struct Ring {
    uint8_t  baseR[MAX_RING_LEDS] = {0};
    uint8_t  baseG[MAX_RING_LEDS] = {0};
    uint8_t  baseB[MAX_RING_LEDS] = {0};
    uint8_t  segR[MAX_SEGMENTS] = {0};
    uint8_t  segG[MAX_SEGMENTS] = {0};
    uint8_t  segB[MAX_SEGMENTS] = {0};
    int      segCount = 1;
    float    segOffset = 0.0f;   // split/comet rotation, as a fraction of the ring (0..1)
    uint8_t  fadeToR = 0, fadeToG = 0, fadeToB = 0;   // FADE second colour: base<->fadeTo
    uint8_t  fromR[MAX_RING_LEDS] = {0};
    uint8_t  fromG[MAX_RING_LEDS] = {0};
    uint8_t  fromB[MAX_RING_LEDS] = {0};
    uint8_t  dispR[MAX_RING_LEDS] = {0};
    uint8_t  dispG[MAX_RING_LEDS] = {0};
    uint8_t  dispB[MAX_RING_LEDS] = {0};
    Pattern  pattern   = STATIC;
    uint32_t period    = 1000;   // ms per blink/pulse cycle or comet revolution
    int32_t  cycles    = -1;     // remaining cycles; <0 = run forever
    uint32_t start     = 0;      // millis() when the pattern began
    uint32_t fadeStart = 0;      // millis() when the current cross-fade began
    uint32_t fadeMs    = 0;      // cross-fade duration of the current change
    bool     fadeWasActive = false;
    bool     dirty     = true;   // a STATIC change still owes one show()
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

// Stage 1: paint k rotating comets into the animated buffer for ring R (n pixels).
// Comet j's head sits at head + j*n/k and trails a fractional 1->0 tail behind it
// (lower indices), coloured by seg[j]. A fractional head keeps the motion smooth.
inline void renderComet_(const Ring& R, int n,
                         uint8_t* oR, uint8_t* oG, uint8_t* oB, uint32_t now) {
    int k = R.segCount < 1 ? 1 : (R.segCount > MAX_SEGMENTS ? MAX_SEGMENTS : R.segCount);
    float head    = (float)((now - R.start) % R.period) / (float)R.period * n
                    + R.segOffset * n;   // angular offset rotates the comet start
    float spacing = (float)n / k;
    float tail    = spacing * 0.7f;
    if (tail > n / 3.0f) tail = n / 3.0f;
    if (tail < 1.0f) tail = 1.0f;
    for (int i = 0; i < n; i++) {
        float best = 0.0f; int bestj = 0;
        for (int j = 0; j < k; j++) {
            float h = head + j * spacing;
            float d = fmodf(h - i + 2.0f * n, (float)n);   // pixel i behind head h
            float b = 1.0f - d / tail;
            if (b > best) { best = b; bestj = j; }
        }
        oR[i] = (uint8_t)(R.segR[bestj] * best);
        oG[i] = (uint8_t)(R.segG[bestj] * best);
        oB[i] = (uint8_t)(R.segB[bestj] * best);
    }
}

// Stage 1: resolve the target colour of each pixel of ring R for the current
// pattern/time.
inline void computeAnimated_(const Ring& R, int n,
                             uint8_t* oR, uint8_t* oG, uint8_t* oB, uint32_t now) {
    if (R.pattern == COMET) { renderComet_(R, n, oR, oG, oB, now); return; }
    if (R.pattern == FADE) {   // whole ring cross-fades base (c1) <-> fadeTo (c2)
        uint32_t t = (now - R.start) % R.period;
        float frac = (float)t / R.period;
        float tri  = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;   // 0 -> 1 -> 0
        for (int i = 0; i < n; i++) {
            oR[i] = lerp8_(R.baseR[i], R.fadeToR, tri);
            oG[i] = lerp8_(R.baseG[i], R.fadeToG, tri);
            oB[i] = lerp8_(R.baseB[i], R.fadeToB, tri);
        }
        return;
    }
    float scale = 1.0f;
    if (R.pattern == BLINK || R.pattern == PULSE) {
        uint32_t t = (now - R.start) % R.period;
        if (R.pattern == BLINK) {
            scale = (t < R.period / 2) ? 1.0f : 0.0f;
        } else {                               // PULSE — triangle ramp 0 -> 1 -> 0
            float frac = (float)t / R.period;
            scale = frac < 0.5f ? frac * 2.0f : (1.0f - frac) * 2.0f;
        }
    }
    for (int i = 0; i < n; i++) {
        oR[i] = (uint8_t)(R.baseR[i] * scale);
        oG[i] = (uint8_t)(R.baseG[i] * scale);
        oB[i] = (uint8_t)(R.baseB[i] * scale);
    }
}

inline float fadeProgress_(const Ring& R, uint32_t now) {
    if (R.fadeMs == 0) return 1.0f;
    uint32_t e = now - R.fadeStart;
    return (e >= R.fadeMs) ? 1.0f : (float)e / (float)R.fadeMs;
}

// Stages 1-3 for ring k: animate, cross-fade from the snapshot, remember the
// output, gamma+show. The only caller of show() outside hardware_init.
inline void renderRing_(int k, uint32_t now) {
    Adafruit_NeoPixel& s = strips[k];
    Ring& R = rings[k];
    int n = (int)s.numPixels();
    uint8_t aR[MAX_RING_LEDS], aG[MAX_RING_LEDS], aB[MAX_RING_LEDS];
    computeAnimated_(R, n, aR, aG, aB, now);
    float ft = fadeProgress_(R, now);
    for (int i = 0; i < n; i++) {
        uint8_t r = lerp8_(R.fromR[i], aR[i], ft);
        uint8_t g = lerp8_(R.fromG[i], aG[i], ft);
        uint8_t b = lerp8_(R.fromB[i], aB[i], ft);
        R.dispR[i] = r; R.dispG[i] = g; R.dispB[i] = b;
        s.setPixelColor(i, srgbColor(r, g, b));
    }
    s.show();
}

inline void hardware_init() {
    uint32_t now = millis();
    for (int k = 0; k < NUM_RINGS; k++) {
        strips[k].begin();
        strips[k].setBrightness(255);
        renderRing_(k, now);   // setup() context — the one show() outside update()
        rings[k].dirty = false;
    }
}

// Resolve a ring argument to the [lo,hi) range it addresses (ring < 0 = all rings;
// out-of-range = empty).
inline void ringRange_(int ring, int& lo, int& hi) {
    lo = (ring < 0) ? 0 : ring;
    hi = (ring < 0) ? NUM_RINGS : ring + 1;
    if (lo < 0) lo = 0;
    if (hi > NUM_RINGS) hi = NUM_RINGS;
}

// Snapshot ring k's screen and start a cross-fade towards the new write. Latch the
// animation mode + timing. count<=0 = run forever.
inline void apply_(Ring& R, Pattern p, uint32_t period, int32_t count,
                   uint32_t fadeMs, uint32_t now) {
    for (int i = 0; i < MAX_RING_LEDS; i++) {
        R.fromR[i] = R.dispR[i]; R.fromG[i] = R.dispG[i]; R.fromB[i] = R.dispB[i];
    }
    R.pattern   = p;
    R.period    = period ? period : 1000;
    R.cycles    = count > 0 ? count : -1;
    R.start     = now;
    R.fadeStart = now;
    R.fadeMs    = fadeMs;
    R.dirty     = true;
}

// Whole ring(s), one colour. ring < 0 = all rings.
inline void set(int ring, uint8_t r, uint8_t g, uint8_t b,
                Pattern p, uint32_t period, int32_t count,
                uint32_t fadeMs = DEFAULT_FADE_MS, float offset = 0.0f) {
    if (ring >= NUM_RINGS) return;
    int lo, hi; ringRange_(ring, lo, hi);
    uint32_t now = millis();
    for (int k = lo; k < hi; k++) {
        Ring& R = rings[k];
        uint16_t n = strips[k].numPixels();
        for (uint16_t i = 0; i < n; i++) { R.baseR[i] = r; R.baseG[i] = g; R.baseB[i] = b; }
        R.segR[0] = r; R.segG[0] = g; R.segB[0] = b; R.segCount = 1;
        R.segOffset = offset;
        apply_(R, p, period, count, fadeMs, now);
    }
}

// Whole ring(s) cross-fading between two colours: base (c1) <-> fadeTo (c2) as a
// triangle 0->1->0 over `period` (one c1->c2->c1 cycle). count<=0 = run forever;
// a bounded fade rests on c1 when done (see update()). ring < 0 = all rings.
// Moves the PC's old per-frame colour stream onto the node — one frame/cycle.
inline void setFade(int ring, uint8_t r, uint8_t g, uint8_t b,
                    uint8_t r2, uint8_t g2, uint8_t b2,
                    uint32_t period, int32_t count,
                    uint32_t fadeMs = DEFAULT_FADE_MS, float offset = 0.0f) {
    if (ring >= NUM_RINGS) return;
    int lo, hi; ringRange_(ring, lo, hi);
    uint32_t now = millis();
    for (int k = lo; k < hi; k++) {
        Ring& R = rings[k];
        uint16_t n = strips[k].numPixels();
        for (uint16_t i = 0; i < n; i++) { R.baseR[i] = r; R.baseG[i] = g; R.baseB[i] = b; }
        R.segR[0] = r; R.segG[0] = g; R.segB[0] = b; R.segCount = 1;
        R.fadeToR = r2; R.fadeToG = g2; R.fadeToB = b2;
        R.segOffset = offset;
        apply_(R, FADE, period, count, fadeMs, now);
    }
}

// Split ring(s) into `k` equal contiguous arcs. Arc boundaries match node_direct
// (seg = i*k/n over each ring's own pixel count). Keeps the k colours for COMET.
// ring < 0 = all rings.
inline void setSegments(int ring, const uint8_t* r, const uint8_t* g,
                        const uint8_t* b, int k, Pattern p, uint32_t period,
                        int32_t count, uint32_t fadeMs = DEFAULT_FADE_MS,
                        float offset = 0.0f) {
    if (ring >= NUM_RINGS) return;
    if (k < 1) k = 1;
    if (k > MAX_SEGMENTS) k = MAX_SEGMENTS;
    int lo, hi; ringRange_(ring, lo, hi);
    uint32_t now = millis();
    for (int j = lo; j < hi; j++) {
        Ring& R = rings[j];
        for (int s = 0; s < k; s++) { R.segR[s] = r[s]; R.segG[s] = g[s]; R.segB[s] = b[s]; }
        R.segCount = k;
        R.segOffset = offset;
        uint16_t n = strips[j].numPixels();
        int off = ((int)lroundf(offset * n)) % n;
        for (uint16_t i = 0; i < n; i++) {
            int idx = (((int)i - off) % n + n) % n;
            int seg = idx * k / n;
            if (seg > k - 1) seg = k - 1;
            R.baseR[i] = r[seg]; R.baseG[i] = g[seg]; R.baseB[i] = b[seg];
        }
        apply_(R, p, period, count, fadeMs, now);
    }
}

// Set a single pixel on ring k (used by the LED test panel). ring < 0 defaults to
// ring 0. Static; leaves the ring's other pixels untouched. Cross-fades it in.
inline void setPixel(int ring, int i, uint8_t r, uint8_t g, uint8_t b,
                     uint32_t fadeMs = DEFAULT_FADE_MS) {
    int k = (ring < 0) ? 0 : ring;
    if (k < 0 || k >= NUM_RINGS) return;
    if (i < 0 || i >= (int)strips[k].numPixels()) return;
    Ring& R = rings[k];
    for (int j = 0; j < MAX_RING_LEDS; j++) {
        R.fromR[j] = R.dispR[j]; R.fromG[j] = R.dispG[j]; R.fromB[j] = R.dispB[j];
    }
    R.baseR[i] = r; R.baseG[i] = g; R.baseB[i] = b;
    R.pattern   = STATIC;
    R.fadeStart = millis();
    R.fadeMs    = fadeMs;
    R.dirty     = true;
}

inline void update() {
    uint32_t now = millis();
    bool refreshDue = (now - lastShow_ >= REFRESH_MS);
    bool animated   = false;

    for (int k = 0; k < NUM_RINGS; k++) {
        Ring& R = rings[k];
        bool fading = R.fadeMs > 0 && (now - R.fadeStart) < R.fadeMs;
        if (!fading && R.fadeWasActive) { R.fadeWasActive = false; R.dirty = true; }
        if (fading) R.fadeWasActive = true;

        if (R.pattern == STATIC && !fading) {
            if (R.dirty) { R.dirty = false; renderRing_(k, now); }
            continue;
        }

        animated = true;
        if (!R.dirty && !refreshDue) continue;
        R.dirty = false;

        // A bounded animation that has run its cycles goes dark and stays there.
        if (R.pattern != STATIC && R.cycles >= 0) {
            uint32_t elapsed = now - R.start;
            if (elapsed >= (uint32_t)R.cycles * R.period) {
                bool wasFade = (R.pattern == FADE);
                R.pattern = STATIC;
                if (!wasFade) {   // FADE rests on its base colour (c1); others go dark
                    for (int i = 0; i < MAX_RING_LEDS; i++) { R.baseR[i] = R.baseG[i] = R.baseB[i] = 0; }
                    R.segCount = 1; R.segR[0] = R.segG[0] = R.segB[0] = 0;
                }
                R.fadeMs = 0;   // snap to the resting colour
                renderRing_(k, now);
                continue;
            }
        }

        renderRing_(k, now);
    }

    if (animated && refreshDue) lastShow_ = now;
}

}  // namespace leds

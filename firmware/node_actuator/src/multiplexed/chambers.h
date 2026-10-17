#pragma once
#include <Arduino.h>
#include <Preferences.h>
#include "pins.h"
#include "config.h"
#include "pca_valves.h"
#include "pumps.h"
#include "mux.h"
#include "coupled_fill.h"   // shared coupled-line fill supervisor (both boards)
#include "pump_duty.h"      // shared PWM ceiling (both boards)

// Per-chamber state + valve/pump coordination for node_multiplexed.
//
// One pump pair per skin (pumps::PUMP_GROUPS): the chambers of a group share
// that group's inflate line and its deflate line; there are no check valves. So
// while two or more valves of the same direction are open in one group those
// chambers equalise and every (mux-read) gauge reads the shared line, not its
// own chamber. Each group is therefore driven like its own node_direct: it owns
// a pair of board-agnostic coupled_fill::Engine (open the group together -> fill
// to the lowest open target -> close -> settle -> measure each isolated ->
// repeat), only with looser timings (I2C PCA9685 valve writes + a per-read mux
// settle are far slower than the direct board's GPIO + dedicated ADC). The
// groups are fully independent: one skin filling or settling never waits for,
// or pumps, another.
//
// The chambers on this board are assumed leak-free: a chamber holds its
// pressure with both valves closed, so there is no leak-compensating hold
// (hold_duty requests are ignored) and a deflate always pulls at full duty.
//
// recalcPumps() runs a group's pump only while one of that group's own valves
// of that direction is open, so a pump never dead-heads its line (the "running
// dry / forcing" failure).

namespace chambers {

constexpr uint8_t DEFAULT_DUTY = pump_duty::FULL;   // full pump speed (8-bit PWM)

enum State : uint8_t {
    IDLE, INFLATING, DEFLATING
};

// Child-safety watchdog: force a chamber off if it stays INFLATING/DEFLATING past
// this without the engine closing it (mux channel unplugged/stuck). The engine's
// own caps act sooner; this is the last-resort backstop.
constexpr uint32_t ACTUATION_TIMEOUT_MS = 10000;

struct Chamber {
    State    state      = IDLE;
    uint8_t  duty       = DEFAULT_DUTY;
    float    target_kpa = 0.0f;
    float    min_kpa    = config::DEFAULT_CHAMBER_MIN_KPA;
    float    max_kpa    = config::DEFAULT_CHAMBER_MAX_KPA;
    uint32_t since_ms   = 0;   // when INFLATING/DEFLATING began (watchdog)
};

inline Chamber state[MAX_CHAMBERS];
inline float   cachedKpa[MAX_CHAMBERS] = {};

// Telemetry window: every gauge read since the last status frame, per chamber.
// The mux reads the chambers one after another, so a status built from only the
// latest read reports one noisy sample; the window mean uses all of them.
inline float    kpaSum[MAX_CHAMBERS] = {};
inline uint16_t kpaCnt[MAX_CHAMBERS] = {};

// Store a fresh (median, ambient-zeroed) read: it becomes the live value the
// guards use and joins the telemetry window.
inline float recordKpa(int i, float k) {
    cachedKpa[i] = k;
    kpaSum[i] += k;
    if (++kpaCnt[i] == UINT16_MAX) { kpaSum[i] = k; kpaCnt[i] = 1; }
    return k;
}

// Mean of the window for the status frame, then start a new window. Falls back
// to the live value when nothing was read since the last frame.
inline float takeWindowKpa(int i) {
    float k = kpaCnt[i] ? kpaSum[i] / kpaCnt[i] : cachedKpa[i];
    kpaSum[i] = 0.0f;
    kpaCnt[i] = 0;
    return k;
}

// Per-chamber ambient zero (kPa) - the pressure sensor has no zero calibration,
// so at atmosphere it reads a few kPa of offset. Subtracting this per chamber
// makes gauge 0 == atmosphere everywhere. Captured vented by tare(), NVS-backed.
inline float   zeroKpa[MAX_CHAMBERS] = {};

inline void loadTare() {
    Preferences p;
    if (!p.begin("se_tare", true)) return;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        char key[8]; snprintf(key, sizeof key, "z%d", i);
        zeroKpa[i] = p.getFloat(key, 0.0f);
    }
    p.end();
}

inline void saveTare() {
    Preferences p;
    if (!p.begin("se_tare", false)) return;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        char key[8]; snprintf(key, sizeof key, "z%d", i);
        p.putFloat(key, zeroKpa[i]);
    }
    p.end();
}

// Capture each chamber's current RAW mux reading as its ambient zero and persist
// it. The caller must have vented every chamber to atmosphere first; the read is
// raw (bypasses zeroKpa) so re-taring is idempotent.
inline void tare() {
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        int ch = config::state.chamber_mux_ch[i];
        if (ch < 0) continue;
        float s = 0.0f;
        for (int k = 0; k < 8; k++) s += mux::readKpa(ch);
        zeroKpa[i] = s / 8.0f;
    }
    saveTare();
}

inline void stop(int n) {
    pca_valves::setChamberValve(n, false, false);
    float saved_max = state[n].max_kpa;
    float saved_min = state[n].min_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved_max;
    state[n].min_kpa = saved_min;
}

// Everything that controls one pump group's pair of lines. Engines are indexed
// by the board-wide chamber number; each only ever sees its own group's chambers.
struct Line {
    coupled_fill::Engine<MAX_CHAMBERS> inflateEng;
    coupled_fill::Engine<MAX_CHAMBERS> deflateEng;

    bool seqActive() const { return inflateEng.active() || deflateEng.active(); }
};

inline Line lines[pumps::NUM_PUMP_GROUPS];

// The line set chamber n hangs on, or nullptr for a chamber with no pump pair
// (nothing can actuate it).
inline Line* lineOf(int n) {
    int g = pumps::groupOf(n);
    return g < 0 ? nullptr : &lines[g];
}

// Chambers under a bench VENT (both valves open, no pump): excluded from the
// pump recalc so nothing pumps into a chamber that is open to atmosphere.
inline uint16_t ventMask      = 0;

// Drive the pumps from the ACTUAL open valves (the pca_valves mirror), never
// from chamber state - so a pump can only run while it has an open flow path.
// Per pump group: its inflate pump runs while one of ITS inflate valves is open,
// its deflate pump while one of ITS deflate valves is open. Suspended while a
// manual override drives the pumps directly (see main.cpp). Both pumps run at
// full duty: no reduced below-floor deflate, no hold duty (leak-free chambers).
inline void recalcPumps() {
    uint16_t openInf = 0, openDef = 0;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (ventMask & (1u << i)) continue;   // vented: no flow path for a pump
        if (pca_valves::isOpen(i, 0)) openInf |= (uint16_t)(1u << i);
        if (pca_valves::isOpen(i, 1)) openDef |= (uint16_t)(1u << i);
    }
    for (int g = 0; g < pumps::NUM_PUMP_GROUPS; g++) {
        uint16_t inf = openInf & pumps::PUMP_GROUPS[g].chambers;
        uint16_t def = openDef & pumps::PUMP_GROUPS[g].chambers;
        pumps::setGroupDuty(g, inf ? DEFAULT_DUTY : 0, def ? DEFAULT_DUTY : 0);
    }
}

// Median-of-three mux read for the control path: rejects a single-sample sensor
// spike (each mux::readKpa already averages 4 ADC samples). Falls back to the
// last cached value if this chamber has no assigned mux channel.
inline float readKpaMedian(int i) {
    int ch = config::state.chamber_mux_ch[i];
    if (ch < 0) return cachedKpa[i];
    float a = mux::readKpa(ch);
    float b = mux::readKpa(ch);
    float c = mux::readKpa(ch);
    float hi = max(a, max(b, c));
    float lo = min(a, min(b, c));
    return (a + b + c - hi - lo) - zeroKpa[i];   // middle value, ambient-zeroed
}

// ---------------------------------------------------------------------------
// Coupled-fill engine wiring. Looser timings than node_direct: I2C valve writes
// and a per-read mux settle make this board slow, so the rounds wait longer.
// ---------------------------------------------------------------------------

inline constexpr coupled_fill::Tuning TUNE_INFLATE {
    /* group_window_ms     */ 120,    // batch the runtime's per-chamber frames (more chambers, slower bus)
    /* min_round_single_ms */ 60,
    /* min_round_ms        */ 350,    // bigger manifold + I2C: longer pump-start spike to ride out
    /* settle_ms           */ 300,    // mux scan + larger manifold needs longer to settle isolated
    /* round_max_ms        */ 8000,
    /* seq_max_ms          */ 45000,  // chambers with different targets -> several rounds
    /* chamber_max_ms      */ 5000,
    /* cutoff_debounce     */ 2,      // each control tick spans more real time than direct
    /* tol_frac            */ 0.10f,
    /* hard_max_kpa        */ config::HARD_CHAMBER_MAX_KPA,
};
inline constexpr coupled_fill::Tuning TUNE_DEFLATE {
    120, 60, 350, 300, 8000, 45000, 5000, 2, 0.10f, config::HARD_CHAMBER_MAX_KPA,
};

inline void (*dbgHook)(uint8_t ev, uint16_t mask, uint8_t dir) = nullptr;

inline void enginesInit() {
    for (Line& L : lines) {
        L.inflateEng.begin(0, TUNE_INFLATE);
        L.deflateEng.begin(1, TUNE_DEFLATE);
    }
}

// Lowest ambient-zeroed reading chamber n's gauge can produce (sensor clamp at
// P_MIN shifted by the chamber's tare zero) - see node_direct.
inline float gaugeFloorKpa(int n) { return pressure::P_MIN - zeroKpa[n]; }

// Engine actuation primitive: open chamber i's valve for `dir` (closing the
// opposite side). The engine recalcs pumps after a whole round opens.
inline void engOpen(int i, uint8_t dir) {
    pca_valves::setChamberValve(i, dir == 0, dir == 1);
    state[i].state    = (dir == 0) ? INFLATING : DEFLATING;
    state[i].since_ms = millis();
}

// ---------------------------------------------------------------------------
// Command-facing API (mirrors node_direct). A request registers a target; the
// engine opens valves and runs the round/measure cycle from controlTick().
// ---------------------------------------------------------------------------

// ``cap_ms`` (optional) is the per-chamber open-time budget forwarded to the
// engine - the closing authority for a target the gauge can't see (a deflate
// below the sensor floor, timed by the PC's calibrated deflate curve).
// ``blind`` runs the chamber open-loop (no pressure sensor populated): the
// engine ignores the gauge entirely and cap_ms is the only closer.

inline void requestInflate(int n, float target, uint8_t duty, uint32_t cap_ms = 0,
                           bool blind = false) {
    Line* L = lineOf(n);
    if (!L) return;
    target = max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    L->deflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    L->inflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa, cap_ms, blind);
}

// ``tail_ms`` > 0 = a pull to the bottom: the target goes below the gauge
// floor (the min clamp is skipped - the gauge cannot see vacuum) and, once the
// reading bottoms out, the chamber pulls blind for tail_ms more.
inline void requestDeflate(int n, float target, uint8_t duty, uint32_t cap_ms = 0,
                           bool blind = false, uint32_t tail_ms = 0) {
    Line* L = lineOf(n);
    if (!L) return;
    target = tail_ms ? gaugeFloorKpa(n) - 1.0f
                     : max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    L->inflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    L->deflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa, cap_ms, blind,
                       gaugeFloorKpa(n), tail_ms);
}

inline void holdChamber(int n) {
    if (n < 0 || n >= MAX_CHAMBERS) return;
    if (Line* L = lineOf(n)) {
        L->inflateEng.drop(n, [](int i) { stop(i); });
        L->deflateEng.drop(n, [](int i) { stop(i); });
    }
    stop(n);
    recalcPumps();
}

// Drive every group's engines. Called at the chamber-control cadence from
// loop(). Keeps the engine chamber count in sync with the live config, and
// refreshes cachedKpa as a side effect of the engines' reads.
inline void controlTick(uint32_t now) {
    auto rd = [](int i) -> float { return recordKpa(i, readKpaMedian(i)); };
    auto op = [](int i, uint8_t d) { engOpen(i, d); };
    auto cl = [](int i) { stop(i); };
    auto rc = []() { recalcPumps(); };
    auto dbI = [](uint8_t ev, uint16_t mask) { if (dbgHook) dbgHook(ev, mask, 0); };
    auto dbD = [](uint8_t ev, uint16_t mask) { if (dbgHook) dbgHook(ev, mask, 1); };
    auto opsI = coupled_fill::ops(rd, op, cl, rc, dbI);
    auto opsD = coupled_fill::ops(rd, op, cl, rc, dbD);
    for (Line& L : lines) {
        L.inflateEng.count = L.deflateEng.count = (uint8_t)config::state.num_chambers;
        L.inflateEng.tick(now, opsI);
        L.deflateEng.tick(now, opsD);
    }
}

inline void closeAll() {
    for (int i = 0; i < MAX_CHAMBERS; i++) stop(i);
}

// Drop any in-progress engine sequence (manual override / emergency takes over).
inline void abortSequences() {
    for (Line& L : lines) {
        L.inflateEng.abort();
        L.deflateEng.abort();
    }
}

// Force-stop any chamber actuating past ACTUATION_TIMEOUT_MS (sensor failure
// safety net - see constant above). Call periodically from loop().
inline void actuationWatchdog(uint32_t now) {
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (state[i].state == IDLE) continue;
        // SIGNED difference: engOpen sets since_ms = millis() a hair after loop()'s
        // `now`, so an unsigned `now - since_ms` would underflow on the round-open
        // tick and force-stop the chamber the instant it opened. (See node_direct.)
        if ((int32_t)(now - state[i].since_ms) >= (int32_t)ACTUATION_TIMEOUT_MS) {
            holdChamber(i);
        }
    }
}

}  // namespace chambers

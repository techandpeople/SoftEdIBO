#pragma once
#include <Arduino.h>
#include <Preferences.h>
#include "pins.h"
#include "config.h"
#include "pca_valves.h"
#include "pumps.h"
#include "mux.h"
#include "coupled_fill.h"   // shared coupled-line fill supervisor (both boards)

// Per-chamber state + valve/pump coordination for node_multiplexed.
//
// Common manifold: the 3 pressure pumps feed ONE shared inflate line for all
// chambers, the 3 vacuum pumps ONE shared deflate line; there are no check
// valves. So while two or more valves of the same direction are open the chambers
// equalise and every (mux-read) gauge reads the shared line, not its own chamber.
// The fill targeting therefore runs in the board-agnostic coupled_fill::Engine
// (open the group together -> fill to the lowest open target -> close -> settle ->
// measure each isolated -> repeat), exactly like node_direct but with looser
// timings (I2C PCA9685 valve writes + a per-read mux settle are far slower than
// the direct board's GPIO + dedicated ADC).
//
// Pump scaling is the key multiplexed difference: with the manifold shared, only
// the NUMBER of pumps running matters, so recalcPumps() spins ceil(open/3) pumps
// of a direction - enough to keep the per-chamber fill speed roughly constant as
// more valves open, and never more than the open valves can vent (so a spare pump
// can't dead-head the manifold: the "running dry / forcing" failure).

namespace chambers {

constexpr uint8_t DEFAULT_DUTY = 255;   // full pump speed (8-bit PWM)

// One pump is enough flow for this many open valves; above it, spin another. With
// 3 pressure + 3 vacuum pumps that caps at 3 active per direction (9 valves).
constexpr int VALVES_PER_PUMP = 3;

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

// Drive the shared pumps from the ACTUAL open valves (the pca_valves mirror),
// never from chamber state - so a pump can only run while it has an open flow
// path. Count-scaled for the common manifold: ceil(open_valves/VALVES_PER_PUMP)
// pumps of each direction, capped at how many pumps that role has. Suspended
// while a manual override drives the pumps directly (see main.cpp).
inline void recalcPumps() {
    int openInf = 0, openDef = 0;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (pca_valves::isOpen(i, 0)) openInf++;
        if (pca_valves::isOpen(i, 1)) openDef++;
    }
    int pCount = openInf > 0 ? (openInf + VALVES_PER_PUMP - 1) / VALVES_PER_PUMP : 0;
    int vCount = openDef > 0 ? (openDef + VALVES_PER_PUMP - 1) / VALVES_PER_PUMP : 0;
    pumps::setRoleActiveCount(pumps::ROLE_PRESSURE, pCount, DEFAULT_DUTY);
    pumps::setRoleActiveCount(pumps::ROLE_VACUUM,   vCount, DEFAULT_DUTY);
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
    /* seq_max_ms          */ 45000,  // up to 9 chambers with different targets -> several rounds
    /* chamber_max_ms      */ 5000,
    /* cutoff_debounce     */ 2,      // each control tick spans more real time than direct
    /* tol_frac            */ 0.10f,
    /* hard_max_kpa        */ config::HARD_CHAMBER_MAX_KPA,
};
inline constexpr coupled_fill::Tuning TUNE_DEFLATE {
    120, 60, 350, 300, 8000, 45000, 5000, 2, 0.10f, config::HARD_CHAMBER_MAX_KPA,
};

inline coupled_fill::Engine<MAX_CHAMBERS> inflateEng;
inline coupled_fill::Engine<MAX_CHAMBERS> deflateEng;

inline void (*dbgHook)(uint8_t ev, uint16_t mask, uint8_t dir) = nullptr;

inline void enginesInit() {
    inflateEng.begin(0, TUNE_INFLATE);
    deflateEng.begin(1, TUNE_DEFLATE);
}

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

inline void requestInflate(int n, float target, uint8_t duty, uint32_t cap_ms = 0) {
    if (n < 0 || n >= MAX_CHAMBERS) return;
    target = max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    deflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    inflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa, cap_ms);
}

inline void requestDeflate(int n, float target, uint8_t duty, uint32_t cap_ms = 0) {
    if (n < 0 || n >= MAX_CHAMBERS) return;
    target = max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    inflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    deflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa, cap_ms);
}

inline void holdChamber(int n) {
    if (n < 0 || n >= MAX_CHAMBERS) return;
    inflateEng.drop(n, [](int i) { stop(i); });
    deflateEng.drop(n, [](int i) { stop(i); });
    stop(n);
    recalcPumps();
}

// Drive both engines. Called at the chamber-control cadence from loop(). Keeps the
// engine chamber count in sync with the live config, and refreshes cachedKpa as a
// side effect of the engine's reads.
inline void controlTick(uint32_t now) {
    inflateEng.count = deflateEng.count = (uint8_t)config::state.num_chambers;
    auto rd = [](int i) -> float { float k = readKpaMedian(i); cachedKpa[i] = k; return k; };
    auto op = [](int i, uint8_t d) { engOpen(i, d); };
    auto cl = [](int i) { stop(i); };
    auto rc = []() { recalcPumps(); };
    auto dbI = [](uint8_t ev, uint16_t mask) { if (dbgHook) dbgHook(ev, mask, 0); };
    auto dbD = [](uint8_t ev, uint16_t mask) { if (dbgHook) dbgHook(ev, mask, 1); };
    auto opsI = coupled_fill::ops(rd, op, cl, rc, dbI);
    auto opsD = coupled_fill::ops(rd, op, cl, rc, dbD);
    inflateEng.tick(now, opsI);
    deflateEng.tick(now, opsD);
}

inline bool seqActive() { return inflateEng.active() || deflateEng.active(); }

inline void closeAll() {
    for (int i = 0; i < MAX_CHAMBERS; i++) stop(i);
}

// Drop any in-progress engine sequence (manual override / emergency takes over).
inline void abortSequences() {
    inflateEng.abort();
    deflateEng.abort();
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

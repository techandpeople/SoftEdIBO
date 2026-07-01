#pragma once
#include <Arduino.h>
#include "pins.h"
#include "pressure.h"
#include "dbg.h"
#include "coupled_fill.h"   // shared coupled-line fill supervisor (both boards)

// Per-chamber state + valve/pump coordination for node_direct.
//
// The 3 chambers share ONE inflate manifold (PUMP1) and ONE deflate manifold
// (PUMP2), with no check valves — so while two or more valves of the same
// direction are open the chambers equalise and every in-line gauge reads the
// shared line, not its own chamber. The actual fill targeting therefore runs in
// the board-agnostic coupled_fill::Engine (open the group together → fill to the
// lowest open target → close → settle → measure each isolated → repeat). This
// file supplies the direct board's actuation (setValve / recalcPumps) and the two
// per-direction engine instances; the manual, bench-test and emergency-stop paths
// are unchanged and bypass the engine.

namespace chambers {

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float DEFAULT_MIN_KPA = 0.0f;
// Effectively uncapped (the unreliable gauge must not gate fills): over-pressure
// is bounded by TIME — the engine's chamber_max_ms, the manual dead-man, and the
// actuation watchdog — not by this ceiling. Kept in sync with skin_config.
constexpr float HARD_MAX_KPA    =  100.0f;
constexpr float HARD_MIN_KPA    = -100.0f;   // limit for vacuum-fed chambers
constexpr uint8_t  DEFAULT_INFLATE_DUTY = 255;
constexpr uint8_t  DEFAULT_DEFLATE_DUTY = 255;

constexpr int PUMP_PWM_FREQ = 20000;
constexpr int PUMP_PWM_RES  =     8;
constexpr int PUMP1_LEDC_CH =     0;
constexpr int PUMP2_LEDC_CH =     1;

enum State : uint8_t {
    IDLE, INFLATING, DEFLATING
};

// Child-safety watchdog: if a chamber stays INFLATING/DEFLATING longer than this
// without the engine closing it (e.g. pressure sensor unplugged or stuck), force
// it off. The engine's own caps (chamber_max_ms, seq_max_ms) act sooner; this is
// the last-resort backstop. Normal rounds finish in a few seconds.
constexpr uint32_t ACTUATION_TIMEOUT_MS = 10000;

struct Chamber {
    State    state      = IDLE;
    uint8_t  duty       = 0;
    float    target_kpa = 0.0f;
    float    min_kpa    = DEFAULT_MIN_KPA;
    float    max_kpa    = DEFAULT_MAX_KPA;
    uint32_t since_ms   = 0;   // when INFLATING/DEFLATING began (watchdog)
};

inline Chamber state[NUM_CHAMBERS];
inline float   cachedKpa[NUM_CHAMBERS] = {};

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------

// Mirror of the actual valve outputs, kept in sync by every setValve() call
// (engine, manual and bench-test paths all route through it). recalcPumps()
// drives the shared pumps from this — never from chamber state — so a pump can
// only run while it has an open flow path.
inline bool valveOpen[NUM_CHAMBERS * 2] = {};

inline void setValve(int ch, int side, bool open) {
    DBG_PRINT("VALVE ch=%d %s %s\n",
              ch, side == 0 ? "inflate" : "deflate", open ? "OPEN" : "close");
    valveOpen[ch * 2 + side] = open;
    digitalWrite(VALVE_PINS[ch * 2 + side], open ? HIGH : LOW);
}

// Drive the shared pumps PURELY from the ACTUAL open valves, never from chamber
// state: each pump runs (full duty) as long as ANY valve of its direction is
// open, and stops only once they are all closed. So a pump can never dead-head
// (no open valve of its direction ⇒ it is off — that is the "running dry" guard)
// and across the engine's round hand-offs it never drops out from under an open
// valve. Direct has one pump per direction, so it is on/off (no count scaling).
inline void recalcPumps() {
    bool anyInflateOpen = false;
    bool anyDeflateOpen = false;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (valveOpen[i * 2 + 0]) anyInflateOpen = true;
        if (valveOpen[i * 2 + 1]) anyDeflateOpen = true;
    }
    uint8_t inflateDuty = anyInflateOpen ? DEFAULT_INFLATE_DUTY : 0;
    uint8_t deflateDuty = anyDeflateOpen ? DEFAULT_DEFLATE_DUTY : 0;
    static uint8_t lastInflateDuty = 0xFF;
    static uint8_t lastDeflateDuty = 0xFF;
    if (inflateDuty != lastInflateDuty || deflateDuty != lastDeflateDuty) {
        DBG_PRINT("PUMPS inflate_duty=%u deflate_duty=%u\n", inflateDuty, deflateDuty);
        lastInflateDuty = inflateDuty;
        lastDeflateDuty = deflateDuty;
    }
    ledcWrite(PUMP1_LEDC_CH, inflateDuty);
    ledcWrite(PUMP2_LEDC_CH, deflateDuty);
}

inline void stop(int n) {
    setValve(n, 0, false);
    setValve(n, 1, false);
    float saved_max = state[n].max_kpa;
    float saved_min = state[n].min_kpa;
    state[n] = Chamber{};
    state[n].max_kpa = saved_max;
    state[n].min_kpa = saved_min;
}

// Median-of-three gauge read for the control path: rejects a single-sample sensor
// spike before it can reach the engine's cutoff (on top of the engine's
// consecutive-read debounce). Each pressure::readKpa already averages 4 ADC
// samples; three of those, median, kills a lone outlier.
inline float readKpaMedian(int ch) {
    float a = pressure::readKpa(PSENSOR_PINS[ch]);
    float b = pressure::readKpa(PSENSOR_PINS[ch]);
    float c = pressure::readKpa(PSENSOR_PINS[ch]);
    float hi = max(a, max(b, c));
    float lo = min(a, min(b, c));
    return a + b + c - hi - lo;   // the middle value
}

// ---------------------------------------------------------------------------
// Coupled-fill engine wiring. Two instances (inflate, deflate); the inflate and
// deflate manifolds are independent so they run concurrently over disjoint
// chamber sets. Direct is the fast board (dedicated ADC pins, direct-GPIO valves)
// so its timings are tight; node_multiplexed uses looser ones (I2C + mux scan).
// ---------------------------------------------------------------------------

inline constexpr coupled_fill::Tuning TUNE_INFLATE {
    /* group_window_ms   */ 50,     // batch the runtime's per-chamber frames so they open together
    /* min_round_single  */ 40,     // 1 chamber: tight cutoff (no manifold kick to ride out)
    /* min_round_ms      */ 220,    // 2+ chambers: ignore the bigger pump-start spike this long
    /* settle_ms         */ 150,    // settle closed before measuring isolated
    /* round_max_ms      */ 6000,   // per-round cap (leak / can't reach)
    /* seq_max_ms        */ 25000,  // whole-sequence safety cap
    /* chamber_max_ms    */ 5000,   // per-chamber cumulative open cap (stuck sensor)
    /* cutoff_debounce   */ 3,      // 3 over-target reads @ ~20 ms control tick ≈ 60 ms
    /* tol_frac          */ 0.10f,
    /* hard_max_kpa      */ HARD_MAX_KPA,
};
// Deflate drives the vacuum manifold; the gauge is blind below atmosphere, so a
// negative target finishes on chamber_max_ms (time), targets ≥ 0 finish on gauge.
inline constexpr coupled_fill::Tuning TUNE_DEFLATE {
    50, 40, 220, 150, 6000, 25000, 5000, 3, 0.10f, HARD_MAX_KPA,
};

inline coupled_fill::Engine<NUM_CHAMBERS> inflateEng;
inline coupled_fill::Engine<NUM_CHAMBERS> deflateEng;

// Optional wireless debug sink (set by commands.h in DEBUG_BUILD). ev is a
// coupled_fill::Event, mask the relevant chamber bitmask, dir 0/1.
inline void (*dbgHook)(uint8_t ev, uint16_t mask, uint8_t dir) = nullptr;

inline void enginesInit() {
    inflateEng.begin(0, TUNE_INFLATE);
    deflateEng.begin(1, TUNE_DEFLATE);
}

// Engine actuation primitive: open chamber i's valve for `dir` (closing the
// opposite side first). The engine recalcs pumps after a whole round opens.
inline void engOpen(int i, uint8_t dir) {
    if (dir == 0) { setValve(i, 1, false); setValve(i, 0, true);  state[i].state = INFLATING; }
    else          { setValve(i, 0, false); setValve(i, 1, true);  state[i].state = DEFLATING; }
    state[i].since_ms = millis();
}

// ---------------------------------------------------------------------------
// Command-facing API. A request only registers a target; the engine opens valves
// and runs the round/measure cycle from controlTick(). Requesting one direction
// drops the chamber from the other (a clean reversal).
// ---------------------------------------------------------------------------

inline void requestInflate(int n, float target, uint8_t duty) {
    if (n < 0 || n >= NUM_CHAMBERS) return;
    target = max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    deflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    inflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa);
}

inline void requestDeflate(int n, float target, uint8_t duty) {
    if (n < 0 || n >= NUM_CHAMBERS) return;
    target = max(state[n].min_kpa, min(target, state[n].max_kpa));
    bool reversed = false;
    inflateEng.drop(n, [&](int i) { stop(i); reversed = true; });
    if (reversed) recalcPumps();
    state[n].duty       = duty;
    state[n].target_kpa = target;
    deflateEng.request(n, target, state[n].max_kpa - state[n].min_kpa);
}

// Stop & hold a chamber wherever it is (drops it from both engines).
inline void holdChamber(int n) {
    if (n < 0 || n >= NUM_CHAMBERS) return;
    inflateEng.drop(n, [](int i) { stop(i); });
    deflateEng.drop(n, [](int i) { stop(i); });
    stop(n);
    recalcPumps();
}

// "Inflate/Deflate All": request every chamber that still needs to move; the
// engine batches them into one coupled round. ``deltaPct`` is percent of range.
inline void inflateAll(int16_t deltaPct) {
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        float delta  = (state[i].max_kpa - state[i].min_kpa)
                     * constrain((int)deltaPct, 0, 100) / 100.0f;
        float target = min(cachedKpa[i] + delta, state[i].max_kpa);
        if (cachedKpa[i] < target) requestInflate(i, target, DEFAULT_INFLATE_DUTY);
    }
}

inline void deflateAll(int16_t deltaPct) {
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        float delta  = (state[i].max_kpa - state[i].min_kpa)
                     * constrain((int)deltaPct, 0, 100) / 100.0f;
        float target = max(cachedKpa[i] - delta, state[i].min_kpa);
        if (cachedKpa[i] > target) requestDeflate(i, target, DEFAULT_DEFLATE_DUTY);
    }
}

// Drive both engines. Call every loop(); cheap when idle. The engine reads each
// chamber's gauge (median-filtered) only while a round is filling/measuring, and
// refreshes cachedKpa as a side effect so telemetry stays current.
inline void controlTick(uint32_t now) {
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

// Force-stop any chamber actuating past ACTUATION_TIMEOUT_MS (sensor failure
// safety net — see constant above). Call periodically from loop().
inline void actuationWatchdog(uint32_t now) {
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (state[i].state == IDLE) continue;
        // SIGNED difference: engOpen sets since_ms = millis(), which is a hair
        // LATER than loop()'s `now`, so on the round-open tick `now - since_ms`
        // would underflow (unsigned) to a huge value and force-stop the chamber
        // the instant it opened. The signed cast makes that small future offset a
        // small negative, so the watchdog only fires after a real timeout.
        if ((int32_t)(now - state[i].since_ms) >= (int32_t)ACTUATION_TIMEOUT_MS) {
            DBG_PRINT("WATCHDOG ch=%d stopped after %ld ms\n", i,
                      (long)(int32_t)(now - state[i].since_ms));
            holdChamber(i);
        }
    }
}

// ---------------------------------------------------------------------------
// Manual (dev/test) actuation — bypasses the engine, so it needs its own safety
// net. Two guards, enforced by manualSafetyTick() from loop():
//   1. Dead-man: any manual actuator auto-offs after MANUAL_MAX_ON_MS, so a lost
//      "off" command or a distracted operator can't leave a pump running.
//   2. HARD_MAX cutoff: the inflate pump is cut (and the offending inflate valve
//      closed) if any chamber reaches the hard pressure limit.
// At most one valve per chamber is held open at a time (inflate XOR deflate).
// These controls are for developers/teachers, never exposed to children.
// ---------------------------------------------------------------------------

constexpr uint32_t MANUAL_MAX_ON_MS = 5000;

inline bool     manualPumpOn[2]                 = {false, false};
inline uint32_t manualPumpTs[2]                 = {0, 0};
inline bool     manualValveOn[NUM_CHAMBERS * 2] = {};
inline uint32_t manualValveTs[NUM_CHAMBERS * 2] = {};

inline void setManualPump(int idx, bool on) {
    if (idx < 0 || idx > 1) return;
    manualPumpOn[idx] = on;
    manualPumpTs[idx] = on ? millis() : 0;
    ledcWrite(idx == 0 ? PUMP1_LEDC_CH : PUMP2_LEDC_CH, on ? DEFAULT_INFLATE_DUTY : 0);
}

inline void setManualValve(int ch, int side, bool open) {
    if (ch < 0 || ch >= NUM_CHAMBERS || side < 0 || side > 1) return;
    // Single side open per chamber: opening one side closes the other.
    if (open) {
        int other = ch * 2 + (1 - side);
        if (manualValveOn[other]) {
            manualValveOn[other] = false;
            manualValveTs[other] = 0;
            setValve(ch, 1 - side, false);
        }
    }
    int i = ch * 2 + side;
    manualValveOn[i] = open;
    manualValveTs[i] = open ? millis() : 0;
    setValve(ch, side, open);
}

inline void manualSafetyTick(uint32_t now) {
    // 1. Dead-man auto-off. SIGNED diff: manualPumpTs/manualValveTs are set with
    //    millis() inside the command drain, which runs AFTER loop() cached `now`,
    //    so the timestamp is a hair AHEAD of `now`. An unsigned `now - ts` would
    //    underflow on that first tick (~4e9 >= 5000) and slam the just-opened
    //    valve/pump shut — the "manual valve sometimes closes itself" bug.
    for (int i = 0; i < 2; i++)
        if (manualPumpOn[i] && (int32_t)(now - manualPumpTs[i]) >= (int32_t)MANUAL_MAX_ON_MS)
            setManualPump(i, false);
    for (int i = 0; i < NUM_CHAMBERS * 2; i++)
        if (manualValveOn[i] && (int32_t)(now - manualValveTs[i]) >= (int32_t)MANUAL_MAX_ON_MS)
            setManualValve(i / 2, i % 2, false);

    // 2. HARD limit cutoff, both directions:
    //    - inflate pump cut (+ inflate valve closed) if a chamber hits HARD_MAX;
    //    - deflate pump cut (+ deflate valve closed) if a chamber hits HARD_MIN.
    if (manualPumpOn[0]) {
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            if (cachedKpa[i] >= HARD_MAX_KPA) {
                setManualPump(0, false);
                if (manualValveOn[i * 2]) setManualValve(i, 0, false);
            }
        }
    }
    if (manualPumpOn[1]) {
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            if (cachedKpa[i] <= HARD_MIN_KPA) {
                setManualPump(1, false);
                if (manualValveOn[i * 2 + 1]) setManualValve(i, 1, false);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Continuous bench test — latch ONE pump + all of its valves wide open,
// INDEFINITELY, ignoring pressure and the manual dead-man. For verifying pump
// and valve wiring at the bench when the pressure sensor reads wrong; never used
// in normal operation or exposed to children.
//
// ``testDir`` is checked by loop(): while >= 0 it short-circuits every control
// tick (engine, dead-man, watchdog) so nothing can stop the run. The hardware is
// asserted once by testRun() and held; testStop() / STOP ALL clear it.
// ---------------------------------------------------------------------------

inline int testDir     = -1;   // -1 = off, 0 = inflate, 1 = deflate
inline int testChamber = -1;   // -1 = all chambers, else a single chamber index

// Dead-man for the continuous run: it bypasses every other safety, so it must not
// outlive the PC link. The dialog re-sends ``test_run`` ~1 Hz as a keepalive; if
// none arrives within this window (dialog gone, USB/ESP-NOW link dropped) loop()
// force-stops the run. Generous vs the ~1 s keepalive to tolerate a dropped frame.
constexpr uint32_t TEST_RUN_TIMEOUT_MS = 3000;
inline uint32_t testHeartbeatMs = 0;

inline void testStop() {
    testDir     = -1;
    testChamber = -1;
    ledcWrite(PUMP1_LEDC_CH, 0);
    ledcWrite(PUMP2_LEDC_CH, 0);
    for (int n = 0; n < NUM_CHAMBERS; n++) {
        setValve(n, 0, false);
        setValve(n, 1, false);
    }
}

// dir: 0 = inflate (PUMP1), 1 = deflate (PUMP2). ``chamber`` < 0 opens every
// valve of that direction (whole-node wiring test); a valid index opens only
// that one chamber's valve. Owns the hardware directly, so it drops any manual
// override to keep manualSafetyTick from fighting it once the test ends.
inline void testRun(int dir, int chamber = -1) {
    if (dir != 0 && dir != 1) return;
    testHeartbeatMs = millis();          // every (re)send refreshes the dead-man
    int newChamber  = (chamber >= 0 && chamber < NUM_CHAMBERS) ? chamber : -1;
    if (testDir == dir && testChamber == newChamber) return;  // already running → just refreshed
    testDir     = dir;
    testChamber = newChamber;
    for (int i = 0; i < 2; i++)                 { manualPumpOn[i] = false; manualPumpTs[i] = 0; }
    for (int i = 0; i < NUM_CHAMBERS * 2; i++)  { manualValveOn[i] = false; manualValveTs[i] = 0; }
    ledcWrite(PUMP1_LEDC_CH, dir == 0 ? DEFAULT_INFLATE_DUTY : 0);
    ledcWrite(PUMP2_LEDC_CH, dir == 1 ? 255 : 0);
    for (int n = 0; n < NUM_CHAMBERS; n++) {
        bool open = (testChamber < 0 || testChamber == n);
        setValve(n, dir,     open);
        setValve(n, 1 - dir, false);
    }
}

// ---------------------------------------------------------------------------
// Emergency stop — latch every actuator OFF until explicitly re-armed.
// ---------------------------------------------------------------------------

inline bool stopped = false;

inline void emergencyStopAll() {
    testDir     = -1;        // also cancels any continuous bench-test run
    testChamber = -1;
    inflateEng.abort();      // drop any in-progress coupled-fill sequence
    deflateEng.abort();
    // Pumps off.
    ledcWrite(PUMP1_LEDC_CH, 0);
    ledcWrite(PUMP2_LEDC_CH, 0);
    // All valves closed + clear any manual override.
    for (int i = 0; i < NUM_CHAMBERS * 2; i++) {
        manualValveOn[i] = false;
        manualValveTs[i] = 0;
    }
    manualPumpOn[0] = manualPumpOn[1] = false;
    manualPumpTs[0] = manualPumpTs[1] = 0;
    for (int n = 0; n < NUM_CHAMBERS; n++) stop(n);   // closes both valves, resets state
}

// ---------------------------------------------------------------------------
// Setup all chamber I/O. Call once from setup().
// ---------------------------------------------------------------------------

inline void hardware_init() {
    for (int i = 0; i < NUM_CHAMBERS * 2; i++) {
        pinMode(VALVE_PINS[i], OUTPUT);
        digitalWrite(VALVE_PINS[i], LOW);
    }
    ledcSetup(PUMP1_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcSetup(PUMP2_LEDC_CH, PUMP_PWM_FREQ, PUMP_PWM_RES);
    ledcAttachPin(PUMP_PINS[0], PUMP1_LEDC_CH);
    ledcAttachPin(PUMP_PINS[1], PUMP2_LEDC_CH);
    ledcWrite(PUMP1_LEDC_CH, 0);
    ledcWrite(PUMP2_LEDC_CH, 0);
    enginesInit();
}

}  // namespace chambers

#pragma once
#include <Arduino.h>
#include "pins.h"
#include "dbg.h"
#include "fill_control.h"   // shared time-based fill + idle leak-maintenance policy

// Per-chamber state machine + valve/pump coordination for node_direct.
// Pumps are shared: any chamber inflating runs PUMP1, any deflating runs PUMP2.
// Board-agnostic fill policy (time-based fill, leak maintenance, safety ceilings)
// lives in firmware/common/fill_control.h, shared with node_multiplexed.

namespace chambers {

constexpr float DEFAULT_MAX_KPA = 8.0f;
constexpr float DEFAULT_MIN_KPA = 0.0f;
// Effectively uncapped (the unreliable gauge must not gate fills): over-pressure
// is bounded by TIME — MAX_FILL_MS, the manual dead-man, and the actuation
// watchdog — not by this ceiling. Kept in sync with skin_config.MAX_ALLOWED_KPA.
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

// Child-safety watchdog: if a chamber stays INFLATING/DEFLATING longer than
// this without reaching its target (e.g. pressure sensor unplugged or stuck,
// so the cutoff in loop() never fires), force-stop it. Normal actuations on
// these small chambers finish in a few seconds.
constexpr uint32_t ACTUATION_TIMEOUT_MS = 10000;

struct Chamber {
    State    state         = IDLE;
    uint8_t  duty          = 0;
    float    target_kpa    = 0.0f;
    float    min_kpa       = DEFAULT_MIN_KPA;
    float    max_kpa       = DEFAULT_MAX_KPA;
    uint32_t since_ms      = 0;  // when INFLATING/DEFLATING began (watchdog)
    uint32_t fill_until_ms = 0;  // INFLATING: stop at this millis() (0 = pressure-based)
    float    hold_kpa      = 0.0f;  // IDLE: level to maintain against leaks (0 = none)
    uint8_t  droop_count   = 0;  // consecutive idle checks seen below hold (touch debounce)
};

inline Chamber state[NUM_CHAMBERS];
inline float   cachedKpa[NUM_CHAMBERS] = {};

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------

// Mirror of the actual valve outputs, kept in sync by every setValve() call
// (autonomous, manual and bench-test paths all route through it). recalcPumps()
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
// state: each pump runs as long as ANY valve of its direction is open, and stops
// only once they are all closed. So while inflating several chambers the inflate
// pump keeps running as the chambers finish and close their valves one by one —
// and across a hand-off where one chamber's valve closes and the next opens, it
// never drops (an open valve is always present). Both directions are symmetric;
// neither can run dead-headed (no open valve of its direction ⇒ it is off).
inline void recalcPumps() {
    // Each shared pump runs at the HIGHEST duty requested by any chamber whose
    // valve of that direction is open (a chamber's duty is set by beginInflate /
    // beginDeflate). Co-active chambers share one pump line, so the fastest one
    // wins; a chamber that wants a gentler fill gets it only while it actuates
    // alone. duty 0 -> that direction is off.
    uint8_t inflateDuty = 0;
    uint8_t deflateDuty = 0;
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (valveOpen[i * 2 + 0]) inflateDuty = max(inflateDuty, state[i].duty);
        if (valveOpen[i * 2 + 1]) deflateDuty = max(deflateDuty, state[i].duty);
    }
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

// ---------------------------------------------------------------------------
// Inflate / deflate. Each chamber has its own inflate and deflate valve, so on
// a direction reversal we just close the opposite valve and open the new one —
// the valve then stays open until stop() (target/limit reached, or hold).
// ---------------------------------------------------------------------------

// ``fill_ms`` > 0 selects time-based fill: the inflate valve stays open for that
// long (clamped to MAX_FILL_MS) regardless of pressure, with HARD_MAX_KPA as the
// only pressure cutoff (caller passes target_kpa = max_kpa). ``fill_ms`` == 0 is
// the classic pressure-target behaviour.
inline void beginInflate(int n, uint8_t duty, float target_kpa, uint32_t fill_ms = 0) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    uint32_t until = fill_control::fillUntil(fill_ms);
    if (state[n].state == INFLATING && state[n].target_kpa == target_kpa
        && state[n].fill_until_ms == 0 && until == 0) return;
    setValve(n, 1, false);              // close deflate before opening inflate
    state[n].state         = INFLATING;
    state[n].duty          = duty;
    state[n].target_kpa    = target_kpa;
    state[n].since_ms      = millis();
    state[n].fill_until_ms = until;
    setValve(n, 0, true);
    recalcPumps();
}

// Close any chamber whose time-based fill window has elapsed. Call every loop()
// (cheap; only acts on time-based INFLATING chambers). Pressure HARD_MAX and the
// actuation watchdog remain independent safety nets.
// Thin node-specific wrappers over the shared fill-control policy: they supply
// the direct board's state predicates and its stop/top-up actuation.
inline void fillTimeTick(uint32_t now) {
    fill_control::fillTimeTick(
        state, cachedKpa, NUM_CHAMBERS, now,
        [](const Chamber& ch) { return ch.state == INFLATING; },
        [](int i, float achieved) {
            DBG_PRINT("FILL ch=%d done (time)\n", i);
            stop(i);
            state[i].hold_kpa = achieved;   // maintain the level we reached
            recalcPumps();
        });
}

// Close any chamber whose deflate time window has elapsed (the active vacuum
// pump's only sensor-independent backstop). Call every loop().
inline void deflateTimeTick(uint32_t now) {
    fill_control::deflateTimeTick(
        state, NUM_CHAMBERS, now,
        [](const Chamber& ch) { return ch.state == DEFLATING; },
        [](int i) {
            DBG_PRINT("DEFLATE ch=%d done (time)\n", i);
            stop(i);
            recalcPumps();
        });
}

inline void maintainTick(uint32_t now) {
    static uint32_t last = 0;
    fill_control::maintainTick(
        state, cachedKpa, NUM_CHAMBERS, now, last,
        [](const Chamber& ch) { return ch.state == IDLE; },
        [](int i, float hold) {
            DBG_PRINT("MAINTAIN ch=%d top-up to %.2f\n", i, hold);
            beginInflate(i, DEFAULT_INFLATE_DUTY, hold);   // pressure-based top-up
        });
}

// ``deflate_ms`` bounds the active vacuum pump in time; 0 falls back to the hard
// MAX_DEFLATE_MS cap. The deadline is ALWAYS armed because the gauge sensor is
// blind below atmosphere, so pressure can't stop a runaway deflate into vacuum.
inline void beginDeflate(int n, float target_kpa, uint32_t deflate_ms = 0,
                         uint8_t duty = DEFAULT_DEFLATE_DUTY) {
    target_kpa = max(state[n].min_kpa, min(target_kpa, state[n].max_kpa));
    if (state[n].state == DEFLATING && state[n].target_kpa == target_kpa) return;
    setValve(n, 0, false);              // close inflate before opening deflate
    state[n].state         = DEFLATING;
    state[n].duty          = duty;      // drives the shared vacuum pump (recalcPumps)
    state[n].target_kpa    = target_kpa;
    state[n].since_ms      = millis();
    state[n].fill_until_ms = fill_control::deflateUntil(deflate_ms);
    setValve(n, 1, true);
    recalcPumps();
}

// ---------------------------------------------------------------------------
// "Inflate All" in two phases — fast AND correct on the shared pump line. The
// gauges sit in-line with all chambers, so while several inflate valves are open
// they all read the same common pressure: you cannot tell one chamber's true fill
// from another's, and a per-chamber cutoff fires on the LINE pressure.
//   Phase 1 COARSE  — open every chamber at once and dump air fast; the cutoff
//     closes each as the common line reaches its target, so the open set narrows
//     3→2→1. Quick, but uneven (a chamber can close while still short).
//   Phase 2 FINISH  — go chamber by chamber with ONLY its valve open, so its gauge
//     reads ITS OWN pressure, and top it up the rest of the way to target.
// Driven by inflateSeqTick() from loop(). Net: bulk air in parallel (fast) + a
// precise per-chamber finish (correct).
// ---------------------------------------------------------------------------
constexpr uint32_t COARSE_MAX_MS = 5000;   // cap on phase 1 (a leaky chamber can't hang it)

inline uint8_t  seqMask  = 0;    // chambers still to finish in phase 2
inline int8_t   seqCur   = -1;   // chamber currently finishing (-1 = none)
inline uint8_t  seqPhase = 0;    // 0 = off, 1 = coarse parallel, 2 = sequential finish
inline uint32_t seqMs    = 0;    // time-based fill window (0 = pressure target)
inline uint32_t seqP1Ms  = 0;    // millis() when phase 1 began
inline float    seqTarget[NUM_CHAMBERS] = {};   // each chamber's absolute target kPa

inline void cancelInflateSeq() { seqMask = 0; seqCur = -1; seqPhase = 0; }

// Phase 1: open every chamber that still needs filling, all together.
inline void inflateAll(int16_t deltaPct, uint32_t fill_ms) {
    seqMs   = fill_ms;
    seqCur  = -1;
    seqMask = 0;
    seqP1Ms = millis();
    bool any = false;
    for (int n = 0; n < NUM_CHAMBERS; n++) {
        float delta = (state[n].max_kpa - state[n].min_kpa)
                    * constrain((int)deltaPct, 0, 100) / 100.0f;
        seqTarget[n] = min(cachedKpa[n] + delta, state[n].max_kpa);
        if (cachedKpa[n] < seqTarget[n]) {
            seqMask |= (uint8_t)(1 << n);    // needs filling → also finish in phase 2
            beginInflate(n, DEFAULT_INFLATE_DUTY,
                         (seqMs > 0) ? state[n].max_kpa : seqTarget[n], seqMs);
            any = true;
        }
    }
    seqPhase = any ? 1 : 0;
}

// Phase 2: open the next queued chamber ALONE (isolated → its gauge reads ITS
// pressure) and fill it to its target.
inline void seqFinishNext() {
    seqCur = -1;
    if (seqMask) {
        int n = __builtin_ctz(seqMask);
        seqMask &= ~(uint8_t)(1 << n);
        seqCur = n;
        beginInflate(n, DEFAULT_INFLATE_DUTY,
                     (seqMs > 0) ? state[n].max_kpa : seqTarget[n], seqMs);
        return;
    }
    seqPhase = 0;   // every chamber finished
}

// Drive the two phases. Call every loop().
inline void inflateSeqTick(uint32_t now) {
    if (seqPhase == 1) {
        bool anyInflating = false;
        for (int n = 0; n < NUM_CHAMBERS; n++)
            if (state[n].state == INFLATING) anyInflating = true;
        if (anyInflating && now - seqP1Ms < COARSE_MAX_MS) return;   // coarse still running
        for (int n = 0; n < NUM_CHAMBERS; n++)                       // stop any straggler
            if (state[n].state == INFLATING) stop(n);
        recalcPumps();
        seqPhase = 2;
        seqFinishNext();
    } else if (seqPhase == 2) {
        if (seqCur >= 0 && state[seqCur].state != INFLATING) seqFinishNext();
    }
}

// Force-stop any chamber actuating past ACTUATION_TIMEOUT_MS (sensor failure
// safety net — see constant above). Call periodically from loop().
inline void actuationWatchdog(uint32_t now) {
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        if (state[i].state == IDLE) continue;
        if (now - state[i].since_ms >= ACTUATION_TIMEOUT_MS) {
            DBG_PRINT("WATCHDOG ch=%d stopped after %lu ms\n", i,
                      (unsigned long)(now - state[i].since_ms));
            stop(i);
            recalcPumps();
        }
    }
}

// ---------------------------------------------------------------------------
// Manual (dev/test) actuation — bypasses the chamber state machine, so it needs
// its own safety net. Two guards, enforced by manualSafetyTick() from loop():
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
    // 1. Dead-man auto-off.
    for (int i = 0; i < 2; i++)
        if (manualPumpOn[i] && now - manualPumpTs[i] >= MANUAL_MAX_ON_MS)
            setManualPump(i, false);
    for (int i = 0; i < NUM_CHAMBERS * 2; i++)
        if (manualValveOn[i] && now - manualValveTs[i] >= MANUAL_MAX_ON_MS)
            setManualValve(i / 2, i % 2, false);

    // 2. HARD limit cutoff, both directions (symmetric with node_multiplexed):
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
// tick (pressure cutoff, dead-man, watchdog) so nothing can stop the run. The
// hardware is asserted once by testRun() and held; testStop() / STOP ALL clear it.
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
// that one chamber's valve (per-chamber bench inflate/deflate that ignores
// pressure and runs until stopped). Owns the hardware directly, so it drops any
// manual override to keep manualSafetyTick from fighting it once the test ends.
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
//
// ``stopped`` is checked by loop(): while set, all autonomous ticks and new
// actuation commands are skipped, so nothing can re-open a valve or spin a
// pump. emergencyStopAll() also slams the hardware off immediately so the stop
// takes effect within the same command, not on the next tick.
// ---------------------------------------------------------------------------

inline bool stopped = false;

inline void emergencyStopAll() {
    testDir     = -1;        // also cancels any continuous bench-test run
    testChamber = -1;
    cancelInflateSeq();      // drop any in-progress two-phase inflate-all
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
}

}  // namespace chambers

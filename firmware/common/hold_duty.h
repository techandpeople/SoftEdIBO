#pragma once
#include <Arduino.h>
#include <math.h>

// ---------------------------------------------------------------------------
// Leak-compensating hold ("hold_duty") - shared by node_direct and
// node_multiplexed.
//
// THE PROBLEM: the silicone skins and their tubing leak, so a chamber "held"
// by closed valves decays off its pose within seconds. A held chamber is
// therefore REGULATED: its inflate valve stays open while the gauge reads
// below the target and the pressure pump runs continuously, with the pump
// PWM adjusted in real time so the delivery balances the leak at the target.
//
// SHARED PUMP + SHARED MANIFOLD: one pressure pump feeds all the chambers of
// a node through one common line (no check valves), so there is ONE pump
// duty for every held chamber at once, and co-open chambers equalise. Each
// chamber's own valve is the per-chamber control: it opens when the chamber
// is below its target (minus a hysteresis band) and closes once it is above
// it (plus the band) - a low-target chamber simply closes earlier than a
// high-target one on the same line, so different targets coexist without
// taking turns. The shared duty servos on the NEEDIEST open chamber (largest
// deficit): a proportional term reacts instantly to a deficit, a slow
// integral base learns the steady leak. The duty never drops below DUTY_MIN
// (150) while a held valve is open: below that the pump cannot keep up with
// the leaky tubing anyway, so the floor keeps the delivery continuous rather
// than letting the servo wind down and the pose sag. The pump is OFF only
// when NO held valve is open (all chambers above target) - a pump running
// into closed valves dead-heads the manifold, which the valves then cannot
// open against.
//
// OWNERSHIP: the coupled-fill engines, the manual/bench overrides and the
// vent own the manifold when active - this engine SUSPENDS (closes only its
// own valves, releases the pump) whenever the board reports the manifold
// busy, and resumes afterwards. Safety: every hold carries a keepalive
// dead-man (the PC re-asserts ~2 s; no refresh for KEEPALIVE_MS = hold
// dropped), and emergency stop / vent / test_run abort() the whole engine.
//
// VACUUM: deliberately NOT wired yet (design open) - the engine only drives
// the inflate side (dir 0). The structure (targets can be negative once the
// gauges are tared to ambient ~ -6 kPa floor) is ready for a vacuum variant.
// ---------------------------------------------------------------------------

namespace hold_duty {

// Keepalive dead-man: a hold not refreshed for this long is dropped (valve
// closed). The PC re-asserts holds every ~2 s.
constexpr uint32_t KEEPALIVE_MS = 6000;

// Control cadence: every step reads each held gauge, reconciles the valves
// and recomputes the shared pump duty.
constexpr uint32_t CTRL_MS = 50;

// Valve hysteresis around the target: open below target - BAND, close above
// target + BAND. Wide enough to ride the gauge noise, narrow enough that the
// pose does not visibly breathe.
constexpr float BAND_KPA = 0.25f;

// Shared pump servo. The proportional gain lifts the duty immediately on a
// deficit (PWM per kPa); the integral base moves one PWM step per INTEG_MS
// whenever the neediest open chamber sits outside DEAD_KPA of its target, so
// the steady-state delivery converges on the leak.
constexpr float    KP_PWM_PER_KPA = 25.0f;
constexpr uint32_t INTEG_MS       = 200;
constexpr float    DEAD_KPA       = 0.1f;

// Pump duty floor while any held valve is open (user requirement: the leaky
// tubing needs at least this much delivery, continuously) and ceiling.
constexpr uint8_t DUTY_MIN = 150;
constexpr uint8_t DUTY_MAX = 255;

template <int MAXN>
struct Engine {
    uint8_t  count = MAXN;         // runtime chamber count (board may use fewer)

    uint16_t activeMask = 0;       // chambers currently holding
    uint16_t openMask   = 0;       // chambers whose valve WE hold open right now
    uint16_t blindMask  = 0;       // holds with no usable gauge (pure duty)
    float    target[MAXN]  = {};   // hold target kPa (NAN = duty-only hold)
    uint8_t  duty[MAXN]    = {};   // seed / last applied duty per chamber
    uint32_t aliveMs[MAXN] = {};   // last keepalive refresh (millis)

    int      base     = DUTY_MIN;  // integral part of the shared pump duty
    uint8_t  lastDuty = 0;         // duty returned by the last control step
    uint32_t ctrlMs   = 0;         // last control step
    uint32_t integMs  = 0;         // last integral step
    bool     suspended = false;    // manifold owned by someone else

    bool active() const { return activeMask != 0; }
    bool isHolding(int i) const { return (activeMask >> i) & 1; }

    // Start or refresh a hold. ``target_kpa`` NAN (or ``blind``) disables the
    // gauge servo - the valve stays open and the duty runs as commanded
    // (sensorless boards, and the future vacuum-below-floor case). Also the
    // keepalive. The seed duty of the FIRST hold primes the integral base;
    // later joiners take the servo where it is (the proportional term covers
    // their deficit).
    void request(int i, float target_kpa, uint8_t d, bool blind) {
        if (i < 0 || i >= count) return;
        uint8_t seed = d < DUTY_MIN ? DUTY_MIN : d;
        if (d) duty[i] = seed;      // 0 = keep current
        else if (!isHolding(i)) duty[i] = DUTY_MIN;
        if (!activeMask) base = duty[i];
        target[i]  = target_kpa;
        aliveMs[i] = millis();
        if (blind || isnan(target_kpa)) blindMask |=  (uint16_t)(1u << i);
        else                            blindMask &= ~(uint16_t)(1u << i);
        activeMask |= (uint16_t)(1u << i);
    }

    // Drop one hold. The board closes the valve via ``closeFn`` if we hold it
    // open right now (pump recalc is the caller's tick's job).
    template <class CloseFn>
    void drop(int i, CloseFn closeFn) {
        if (i < 0 || i >= count) return;
        uint16_t bit = (uint16_t)(1u << i);
        activeMask &= ~bit;
        if (openMask & bit) { closeFn(i); openMask &= ~bit; }
        if (!activeMask) lastDuty = 0;
    }

    // Hard reset (emergency stop / vent / test_run took the hardware). The
    // caller has already slammed everything off; this only clears state.
    void abort() {
        activeMask = 0;
        openMask   = 0;
        lastDuty   = 0;
        suspended  = false;
    }

    // Drive the hold. Call every loop tick.
    //   busy     - manifold owned by the fill engines / manual / test / vent.
    //   openFn(i)  - open chamber i's inflate valve.
    //   closeFn(i) - close chamber i's inflate valve.
    //   readFn(i)  - gauge kPa for chamber i (only called for non-blind holds).
    // Returns the pump duty this engine wants for the pressure pump feeding
    // its open valves: 0 = none (idle, suspended, or every held chamber is
    // above target with its valve closed). The BOARD applies it in its
    // recalcPumps, and only when no non-hold valve is open.
    template <class OpenFn, class CloseFn, class ReadFn>
    uint8_t tick(uint32_t now, bool busy,
                 OpenFn openFn, CloseFn closeFn, ReadFn readFn) {
        // Keepalive dead-man: silently expired holds are dropped.
        for (int i = 0; i < count; i++)
            if (isHolding(i) &&
                (int32_t)(now - aliveMs[i]) >= (int32_t)KEEPALIVE_MS)
                drop(i, closeFn);

        if (!activeMask) { suspended = false; lastDuty = 0; return 0; }

        // Someone else owns the manifold: close only OUR valves and wait.
        if (busy) {
            if (!suspended) {
                for (int i = 0; i < count; i++)
                    if (openMask & (1u << i)) closeFn(i);
                openMask  = 0;
                suspended = true;
                lastDuty  = 0;
            }
            return 0;
        }
        suspended = false;

        // Between control steps the outputs stand.
        if ((int32_t)(now - ctrlMs) < (int32_t)CTRL_MS) return lastDuty;
        ctrlMs = now;

        // Per-chamber valve hysteresis; the neediest OPEN gauge drives the
        // shared pump. Blind holds keep their valve open unconditionally and
        // their commanded duty is a floor for the line.
        float   need      = NAN;    // largest deficit among open gauged holds
        uint8_t blindDuty = 0;
        bool    anyOpen   = false;
        for (int i = 0; i < count; i++) {
            if (!isHolding(i)) continue;
            uint16_t bit = (uint16_t)(1u << i);
            if ((blindMask & bit)) {
                if (!(openMask & bit)) { openFn(i); openMask |= bit; }
                if (duty[i] > blindDuty) blindDuty = duty[i];
                anyOpen = true;
                continue;
            }
            float err = target[i] - readFn(i);
            if (openMask & bit) {
                if (err < -BAND_KPA) { closeFn(i); openMask &= ~bit; }
            } else if (err > BAND_KPA) {
                openFn(i); openMask |= bit;
            }
            if (openMask & bit) {
                anyOpen = true;
                if (isnan(need) || err > need) need = err;
            }
        }

        if (!anyOpen) { lastDuty = 0; return 0; }   // all above target: pump off

        int d = base;
        if (!isnan(need)) {
            // Integral base: one step per INTEG_MS while the neediest open
            // chamber sits outside the dead band.
            if ((int32_t)(now - integMs) >= (int32_t)INTEG_MS) {
                integMs = now;
                if      (need >  DEAD_KPA) base++;
                else if (need < -DEAD_KPA) base--;
                if (base < DUTY_MIN) base = DUTY_MIN;
                if (base > DUTY_MAX) base = DUTY_MAX;
            }
            // Proportional lift on the deficit (never a cut below the base:
            // a chamber above target closes its own valve instead).
            d = base + (need > 0 ? (int)(need * KP_PWM_PER_KPA + 0.5f) : 0);
        }
        if (blindDuty > d) d = blindDuty;
        if (d < DUTY_MIN) d = DUTY_MIN;
        if (d > DUTY_MAX) d = DUTY_MAX;
        for (int i = 0; i < count; i++)
            if ((openMask & (1u << i)) && !((blindMask >> i) & 1))
                duty[i] = (uint8_t)d;
        lastDuty = (uint8_t)d;
        return lastDuty;
    }
};

}  // namespace hold_duty

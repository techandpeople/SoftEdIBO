#pragma once
#include <Arduino.h>
#include <math.h>

// ---------------------------------------------------------------------------
// Leak-compensating hold ("hold_duty") - shared by node_direct and
// node_multiplexed.
//
// THE PROBLEM: the silicone skins leak slowly, so a chamber "held" by closed
// valves decays off its pose within tens of seconds. Instead of pulsed
// top-ups (valve clatter), a held chamber keeps its INFLATE valve OPEN with
// the pressure pump running continuously at the *equilibrium duty* - the PWM
// whose delivery exactly balances the leak at the hold pressure. The duty
// comes from a per-chamber calibration curve on the PC (duty vs kPa); with a
// working gauge the engine trims it slowly (integral steps) so drift and
// curve error are corrected on the node itself.
//
// SHARED-MANIFOLD RULE: all inflate valves tap one common line (no check
// valves), so co-held chambers EQUALISE. Chambers holding the SAME target
// (within a small tolerance) are therefore grouped and held together; holds
// at DIFFERENT targets take turns round-robin - one group's valves open per
// window, the others closed and coasting on their (slow) leak until their
// turn tops them back up.
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

// Targets within this of each other count as "the same" and hold together.
constexpr float GROUP_TOL_KPA = 0.5f;

// Round-robin window when groups at different targets coexist.
constexpr uint32_t ROBIN_WINDOW_MS = 3000;

// Keepalive dead-man: a hold not refreshed for this long is dropped (valve
// closed). The PC re-asserts holds every ~2 s.
constexpr uint32_t KEEPALIVE_MS = 6000;

// Integral trim cadence and authority (gauge-guided duty correction).
constexpr uint32_t TRIM_MS       = 250;
constexpr float    TRIM_BAND_KPA = 0.15f;  // |err| below this: leave duty alone
constexpr uint8_t  DUTY_MIN      = 15;     // below this the pump stalls anyway
constexpr uint8_t  DUTY_MAX      = 255;

template <int MAXN>
struct Engine {
    uint8_t  count = MAXN;         // runtime chamber count (board may use fewer)

    uint16_t activeMask = 0;       // chambers currently holding
    uint16_t openMask   = 0;       // chambers whose valve WE hold open right now
    uint16_t blindMask  = 0;       // holds with no usable gauge (pure duty)
    float    target[MAXN]  = {};   // hold target kPa (NAN = duty-only hold)
    uint8_t  duty[MAXN]    = {};   // current per-chamber equilibrium duty
    uint32_t aliveMs[MAXN] = {};   // last keepalive refresh (millis)

    uint32_t robinMs  = 0;         // current round-robin window start
    int      robinIdx = 0;         // which group index is on the line
    uint32_t trimMs   = 0;         // last trim step
    bool     suspended = false;    // manifold owned by someone else

    bool active() const { return activeMask != 0; }
    bool isHolding(int i) const { return (activeMask >> i) & 1; }

    // Start or refresh a hold. ``target_kpa`` NAN (or ``blind``) disables the
    // gauge trim - the duty runs as commanded (sensorless boards, and the
    // future vacuum-below-floor case). Also the keepalive.
    void request(int i, float target_kpa, uint8_t d, bool blind) {
        if (i < 0 || i >= count) return;
        target[i]  = target_kpa;
        if (d) duty[i] = d < DUTY_MIN ? DUTY_MIN : d;   // 0 = keep current
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
    }

    // Hard reset (emergency stop / vent / test_run took the hardware). The
    // caller has already slammed everything off; this only clears state.
    void abort() {
        activeMask = 0;
        openMask   = 0;
        suspended  = false;
    }

    // ---- internal: the group (by target) that chamber i belongs to ----
    bool sameGroup(int a, int b) const {
        bool na = (blindMask >> a) & 1, nb = (blindMask >> b) & 1;
        if (na != nb) return false;
        // Blind holds have no target to match - the shared line runs ONE pump
        // duty, so only equal-duty blind holds can honestly hold together.
        if (na) return duty[a] == duty[b];
        return fabsf(target[a] - target[b]) <= GROUP_TOL_KPA;
    }

    // Distinct groups, ordered by lowest member index. Returns group count and
    // fills ``rep[]`` with one representative chamber per group.
    int groups(int rep[MAXN]) const {
        int n = 0;
        for (int i = 0; i < count; i++) {
            if (!isHolding(i)) continue;
            bool found = false;
            for (int g = 0; g < n && !found; g++)
                if (sameGroup(rep[g], i)) found = true;
            if (!found) rep[n++] = i;
        }
        return n;
    }

    uint16_t groupMask(int repCh) const {
        uint16_t m = 0;
        for (int i = 0; i < count; i++)
            if (isHolding(i) && sameGroup(repCh, i)) m |= (uint16_t)(1u << i);
        return m;
    }

    // Drive the hold. Call every loop tick.
    //   busy     - manifold owned by the fill engines / manual / test / vent.
    //   openFn(i)  - open chamber i's inflate valve.
    //   closeFn(i) - close chamber i's inflate valve.
    //   readFn(i)  - gauge kPa for chamber i (only called for non-blind holds).
    // Returns the pump duty this engine wants for the pressure pump feeding
    // its open valves: 0 = none (idle or suspended). The BOARD applies it in
    // its recalcPumps, and only when no non-hold valve is open.
    template <class OpenFn, class CloseFn, class ReadFn>
    uint8_t tick(uint32_t now, bool busy,
                 OpenFn openFn, CloseFn closeFn, ReadFn readFn) {
        // Keepalive dead-man: silently expired holds are dropped.
        for (int i = 0; i < count; i++)
            if (isHolding(i) &&
                (int32_t)(now - aliveMs[i]) >= (int32_t)KEEPALIVE_MS)
                drop(i, closeFn);

        if (!activeMask) { suspended = false; return 0; }

        // Someone else owns the manifold: close only OUR valves and wait.
        if (busy) {
            if (!suspended) {
                for (int i = 0; i < count; i++)
                    if (openMask & (1u << i)) closeFn(i);
                openMask  = 0;
                suspended = true;
            }
            return 0;
        }
        suspended = false;

        // Pick the group on the line: single group = always; several = robin.
        int rep[MAXN];
        int n = groups(rep);
        if (n == 0) return 0;
        if (robinIdx >= n) robinIdx = 0;
        if (n > 1 && (int32_t)(now - robinMs) >= (int32_t)ROBIN_WINDOW_MS) {
            robinIdx = (robinIdx + 1) % n;
            robinMs  = now;
        }
        uint16_t want = groupMask(rep[robinIdx]);

        // Reconcile valves to the wanted set.
        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if ((want & bit) && !(openMask & bit)) { openFn(i);  openMask |= bit; }
            if (!(want & bit) && (openMask & bit)) { closeFn(i); openMask &= ~bit; }
        }

        // Group duty = max of members (trim converges them together).
        uint8_t d = DUTY_MIN;
        for (int i = 0; i < count; i++)
            if ((want & (1u << i)) && duty[i] > d) d = duty[i];

        // Gauge trim: the open group equalises with the line, so one member's
        // gauge speaks for the group. Slow integral steps only.
        int repCh = rep[robinIdx];
        if (!((blindMask >> repCh) & 1) &&
            (int32_t)(now - trimMs) >= (int32_t)TRIM_MS) {
            trimMs = now;
            float err = target[repCh] - readFn(repCh);
            int step = 0;
            if      (err >  TRIM_BAND_KPA) step = +1;
            else if (err < -TRIM_BAND_KPA) step = -1;
            if (step != 0) {
                int nd = (int)d + step;
                if (nd < DUTY_MIN) nd = DUTY_MIN;
                if (nd > DUTY_MAX) nd = DUTY_MAX;
                d = (uint8_t)nd;
                for (int i = 0; i < count; i++)
                    if (want & (1u << i)) duty[i] = d;
            }
        }
        return d;
    }
};

}  // namespace hold_duty

#pragma once
#include <Arduino.h>
#include <math.h>
#include "pump_duty.h"     // shared PWM floor/ceiling (both boards + PC)

// ---------------------------------------------------------------------------
// Leak-compensating hold ("hold_duty") - shared by node_direct and
// node_multiplexed. Drives BOTH sides: a pressure hold (dir 0, inflate valve +
// pressure pump) and a vacuum hold (dir 1, deflate valve + vacuum pump).
//
// THE PROBLEM: the silicone skins and their tubing leak, so a chamber "held"
// by closed valves decays off its pose within seconds. A held chamber is
// therefore REGULATED on the node, DUTY FIRST: its valve stays open and the
// pump PWM of that side is servoed in real time so the delivery balances
// the leak at the target - a continuous, pulse-free hold. The shared duty is
// base + KP * deficit: the proportional term reacts at once (both ways - a
// chamber above its target pulls the duty DOWN toward the floor), the base
// is the learned leak, primed by the PC's calibrated equilibrium seed.
//
// PULSES ONLY WHEN THE FLOOR OVER-DELIVERS: the diaphragm pumps barely move
// air below pump_duty::MIN, so the servo cannot go under that floor. When
// even the floor delivers more than the chamber leaks, the pressure keeps
// rising with the servo saturated - only then does the valve close, and it
// re-opens once the chamber has drifted OPEN_BAND below its target. That
// fallback is a train of top-up pulses, each one felt as a bump on the skin,
// so the engine keeps them as small and soft as it can:
//   * PREDICTIVE CLOSE - with the servo on the floor, the valve closes when
//     the reading extrapolated LEAD_MS ahead at its measured slope reaches
//     the target (not once it is past it), so the sensor / valve latency no
//     longer becomes overshoot. Confirmed on CLOSE_DEBOUNCE consecutive
//     ticks, after a MIN_OPEN_MS pulse so the pump-start kick on the gauge
//     cannot chatter the valve.
//   * PUMP RAMP - the duty slews up at SLEW_PWM_PER_MS instead of stepping
//     from 0 to the floor, which softens the pressure surge that started
//     every pulse.
//   * NO INTEGRAL WINDUP - the base steps up only while the chamber sits
//     below target AND is not already rising (or the pulse starves past
//     PULSE_LONG_MS), and steps down only while it sits above target AND is
//     not already falling. The previous integral stepped the base UP for
//     as long as a chamber sat below target - which is the whole of every
//     pulse - so the pulses hardened cycle after cycle.
//   * ONE LEVEL PER LINE - one pump feeds all chambers of a side through one
//     line with no check valves, so co-open chambers equalise and the higher
//     one is robbed by the lower. Only chambers whose targets lie within
//     GROUP_TOL_KPA of each other share the line: a chamber at another level
//     waits while the line's chambers still need air, and the line YIELDS
//     to it (its satisfied chambers close, to re-open when they drift) once
//     they sit at their targets. A starving line lets every needy chamber
//     in regardless.
//
// VACUUM (dir 1): the same regulator mirrored - the gauge axis is flipped so
// "deficit" always means "needs more pumping". A target the gauge can SEE
// (the -40..40 sensor, or a shallow vacuum above the tared floor) is held
// exactly like a pressure target. A target BELOW the gauge floor (the blind
// 0..100 sensor reads its floor for anything deeper) cannot be servoed, so
// it is held by TIMED RE-PULLS: a short REPULL_ON_MS pull at the seed duty
// every REPULL_PERIOD_MS compensates the leak, and whenever the chamber has
// leaked up into the visible band (reading above floor + OPEN_BAND) it is
// pulled straight back to the floor. The deflate that preceded the hold did
// the full-duty burst; the re-pulls only trickle. The valve-lock limit
// (FA0520E ~47 kPa, pressure.h VACUUM_HOLD_FLOOR_KPA) bounds set_min on the
// boards; the re-pull duty cycle is kept small so a low leak cannot walk the
// chamber far past its pose between keepalives.
//
// OWNERSHIP: the coupled-fill engines, the manual/bench overrides and the
// vent own the manifolds when active - this engine SUSPENDS (closes only its
// own valves, releases the pumps) whenever the board reports them busy, and
// resumes afterwards. Safety: every hold carries a keepalive dead-man (the PC
// re-asserts ~2 s; no refresh for KEEPALIVE_MS = hold dropped), and
// emergency stop / vent / test_run abort() the whole engine. The pumps are
// OFF whenever no held valve of their side is open - a pump running into
// closed valves dead-heads the manifold, which the valves cannot open
// against.
// ---------------------------------------------------------------------------

namespace hold_duty {

// Keepalive dead-man: a hold not refreshed for this long is dropped (valve
// closed). The PC re-asserts holds every ~2 s.
constexpr uint32_t KEEPALIVE_MS = 6000;

// Default control cadence (a board sets Engine::ctrlPeriodMs to its own).
constexpr uint32_t DEFAULT_CTRL_MS = 50;

// A closed chamber re-opens once it has drifted this far off its target
// toward the leak side. Wide enough to ride the gauge noise.
constexpr float OPEN_BAND_KPA = 0.25f;

// Inside this of the target the chamber counts as AT it: the base holds
// still and the line counts the chamber as satisfied.
constexpr float DEAD_KPA = 0.1f;

// Predictive close (servo saturated on the floor only): the valve closes
// when the reading extrapolated LEAD_MS ahead at its measured slope reaches
// the target (covers the read + valve latency), confirmed on CLOSE_DEBOUNCE
// consecutive control ticks.
constexpr uint32_t LEAD_MS        = 80;
constexpr uint8_t  CLOSE_DEBOUNCE = 2;
// EMA weight of the slope estimate (per control tick).
constexpr float    SLOPE_ALPHA    = 0.3f;

// Shortest pulse: rides out the pump-start kick on the gauge.
constexpr uint32_t MIN_OPEN_MS = 60;

// Chambers whose targets lie within this of the level on the line pulse
// together; others wait for the line.
constexpr float GROUP_TOL_KPA = 0.5f;

// A chamber still short of its target after this long open is starving:
// the pump is not keeping up. The base climbs even though the reading is
// creeping up, and every needy chamber is let onto the line.
constexpr uint32_t PULSE_LONG_MS = 1500;
// Base (integral) step cadence.
constexpr uint32_t INTEG_MS      = 200;

// Proportional term of the shared duty on the neediest open deficit (PWM
// per kPa), both ways: above target it cuts the duty toward the floor.
constexpr float KP_PWM_PER_KPA = 25.0f;

// Upward duty slew (PWM per ms): 0 -> floor in ~90 ms. Down is immediate.
constexpr float SLEW_PWM_PER_MS = 2.0f;

// Vacuum target this close to (or below) the gauge floor is not visible
// enough to servo: it is held by timed re-pulls instead.
constexpr float FLOOR_MARGIN_KPA = 2.0f * OPEN_BAND_KPA;

// Blind vacuum re-pull: valve open for REPULL_ON_MS every REPULL_PERIOD_MS at
// the chamber's seed duty (>= the floor).
constexpr uint32_t REPULL_PERIOD_MS = 2500;
constexpr uint32_t REPULL_ON_MS     = 120;

// Pump duty floor and ceiling. The floor is only crossed by the start-up ramp.
constexpr uint8_t DUTY_MIN = pump_duty::MIN;
constexpr uint8_t DUTY_MAX = pump_duty::FULL;

// Pump duties the engine wants, one per side (0 = that pump off).
struct Duties {
    uint8_t inflate;   // pressure pump(s), for the open dir-0 hold valves
    uint8_t deflate;   // vacuum pump(s), for the open dir-1 hold valves
};

template <int MAXN>
struct Engine {
    uint8_t  count        = MAXN;            // runtime chamber count
    uint32_t ctrlPeriodMs = DEFAULT_CTRL_MS; // board-tuned control cadence

    uint16_t activeMask = 0;       // chambers currently holding
    uint16_t openMask   = 0;       // chambers whose valve WE hold open right now
    uint16_t blindMask  = 0;       // sensorless holds: valve open at duty, no gauge
    uint16_t floorMask  = 0;       // vacuum holds below the gauge floor: timed re-pulls
    uint16_t dirMask    = 0;       // bit set = vacuum hold (dir 1), clear = pressure
    float    target[MAXN]   = {};  // hold target kPa (NAN = duty-only hold)
    float    floorKpa[MAXN] = {};  // gauge floor of the chamber at request time
    uint8_t  duty[MAXN]     = {};  // seed / last applied duty per chamber
    uint32_t aliveMs[MAXN]  = {};  // last keepalive refresh (millis)

    // Per-chamber regulator state.
    uint32_t openSince[MAXN]  = {};  // when the current pulse opened
    uint32_t lastPullMs[MAXN] = {};  // blind vacuum: when the last re-pull ended
    uint32_t lastReadMs[MAXN] = {};  // slope estimator
    float    lastKpa[MAXN]    = {};
    float    slope[MAXN]      = {};  // kPa per ms, EMA
    float    deficit[MAXN]    = {};  // signed toward the pump side (>0 = needs pumping)
    uint8_t  closeCnt[MAXN]   = {};  // consecutive "reached target" ticks

    // Per-side shared pump servo.
    int      base[2]     = {DUTY_MIN, DUTY_MIN};  // learned floor of the duty
    uint8_t  lastDuty[2] = {0, 0};                // duty returned last step
    uint32_t integMs[2]  = {0, 0};
    uint32_t ctrlMs      = 0;
    bool     suspended   = false;  // manifolds owned by someone else

    bool active() const { return activeMask != 0; }
    bool isHolding(int i) const { return (activeMask >> i) & 1; }
    uint8_t dirOf(int i) const { return (dirMask >> i) & 1; }
    uint16_t sideMask(uint8_t d) const {
        return d ? (activeMask & dirMask) : (activeMask & (uint16_t)~dirMask);
    }
    uint16_t openInflateMask() const { return openMask & (uint16_t)~dirMask; }
    uint16_t openDeflateMask() const { return openMask & dirMask; }

    // Start or refresh a hold. ``d`` is the side (0 pressure, 1 vacuum).
    // ``target_kpa`` NAN (or ``blind``) disables the gauge: the valve stays
    // open and the pump runs at the commanded duty (sensorless boards). A
    // vacuum target the gauge cannot see (below ``floor_kpa`` + margin) is
    // held by timed re-pulls. ``seed`` 0 keeps the current duty. Also the
    // keepalive. ``closeFn(i, side)`` closes a valve this engine holds open
    // on the OTHER side when a live hold flips direction.
    template <class CloseFn>
    void request(int i, uint8_t d, float target_kpa, uint8_t seed, bool blind,
                 float floor_kpa, CloseFn closeFn) {
        if (i < 0 || i >= count) return;
        uint16_t bit = (uint16_t)(1u << i);
        d = d ? 1 : 0;
        bool fresh = !isHolding(i);
        if (!fresh && dirOf(i) != d && (openMask & bit)) {
            closeFn(i, dirOf(i));
            openMask &= ~bit;
        }
        uint8_t s = seed < DUTY_MIN ? DUTY_MIN : seed;
        if (seed) duty[i] = s;
        else if (fresh) duty[i] = DUTY_MIN;
        // The first hold on an idle side primes its servo base with the seed
        // (a keepalive refresh must not: the base is what the servo learned).
        if (fresh && !(sideMask(d) & ~bit)) base[d] = duty[i];
        target[i]   = target_kpa;
        floorKpa[i] = floor_kpa;
        aliveMs[i]  = millis();
        if (d) dirMask |= bit; else dirMask &= ~bit;
        bool isBlind = blind || isnan(target_kpa);
        if (isBlind) blindMask |= bit; else blindMask &= ~bit;
        bool belowFloor = !isBlind && d == 1 && target_kpa < floor_kpa + FLOOR_MARGIN_KPA;
        if (belowFloor) floorMask |= bit; else floorMask &= ~bit;
        if (fresh) {
            openSince[i]  = 0;
            lastPullMs[i] = millis();   // the deflate/inflate just ran: first re-pull after a period
            lastReadMs[i] = 0;
            slope[i]      = 0.0f;
            deficit[i]    = 0.0f;
            closeCnt[i]   = 0;
        }
        activeMask |= bit;
    }

    // Drop one hold. Closes the valve via ``closeFn(i, side)`` if we hold it
    // open right now (pump recalc is the caller's tick's job).
    template <class CloseFn>
    void drop(int i, CloseFn closeFn) {
        if (i < 0 || i >= count) return;
        uint16_t bit = (uint16_t)(1u << i);
        if (!(activeMask & bit)) return;
        if (openMask & bit) { closeFn(i, dirOf(i)); openMask &= ~bit; }
        activeMask &= ~bit;
        blindMask  &= ~bit;
        floorMask  &= ~bit;
        for (int d = 0; d < 2; d++)
            if (!sideMask((uint8_t)d)) lastDuty[d] = 0;
    }

    // Hard reset (emergency stop / vent / test_run took the hardware). The
    // caller has already slammed everything off; this only clears state.
    void abort() {
        activeMask = 0;
        openMask   = 0;
        blindMask  = 0;
        floorMask  = 0;
        lastDuty[0] = lastDuty[1] = 0;
        suspended  = false;
    }

    // Drive the holds. Call every loop tick.
    //   busy          - manifolds owned by the fill engines / manual / test / vent.
    //   openFn(i, s)  - open chamber i's valve on side s (0 inflate, 1 deflate).
    //   closeFn(i, s) - close it.
    //   readFn(i)     - gauge kPa for chamber i (only called for gauged holds).
    // Returns the pump duty wanted per side for the hold valves it has open:
    // 0 = that pump off (idle, suspended, or every held chamber of that side
    // is closed at its target). The BOARD applies them in its recalcPumps,
    // and only while no non-hold valve of that side is open.
    template <class OpenFn, class CloseFn, class ReadFn>
    Duties tick(uint32_t now, bool busy,
                OpenFn openFn, CloseFn closeFn, ReadFn readFn) {
        // Keepalive dead-man: silently expired holds are dropped.
        for (int i = 0; i < count; i++)
            if (isHolding(i) &&
                (int32_t)(now - aliveMs[i]) >= (int32_t)KEEPALIVE_MS)
                drop(i, closeFn);

        if (!activeMask) {
            suspended = false;
            lastDuty[0] = lastDuty[1] = 0;
            return Duties{0, 0};
        }

        // Someone else owns the manifolds: close only OUR valves and wait.
        if (busy) {
            if (!suspended) {
                for (int i = 0; i < count; i++)
                    if (openMask & (1u << i)) closeFn(i, dirOf(i));
                openMask  = 0;
                suspended = true;
                lastDuty[0] = lastDuty[1] = 0;
            }
            return Duties{0, 0};
        }
        suspended = false;

        // Between control steps the outputs stand.
        if ((int32_t)(now - ctrlMs) < (int32_t)ctrlPeriodMs)
            return Duties{lastDuty[0], lastDuty[1]};
        uint32_t dt = ctrlMs ? now - ctrlMs : ctrlPeriodMs;
        if (dt > 4 * ctrlPeriodMs) dt = 4 * ctrlPeriodMs;   // a stall is not a slope
        ctrlMs = now;

        uint16_t wantMask = wishes(now, readFn);
        for (uint8_t d = 0; d < 2; d++) wantMask = arbitrate(now, d, wantMask);
        applyValves(now, wantMask, openFn, closeFn);

        Duties out{0, 0};
        out.inflate = sideDuty(now, dt, 0);
        out.deflate = sideDuty(now, dt, 1);
        return out;
    }

private:
    // Phase 1: what each held chamber wants on its own (open = bit set), from
    // its gauge - or its timer for blind/below-floor holds.
    template <class ReadFn>
    uint16_t wishes(uint32_t now, ReadFn readFn) {
        uint16_t want = 0;
        for (int i = 0; i < count; i++) {
            if (!isHolding(i)) continue;
            uint16_t bit = (uint16_t)(1u << i);
            bool open = (openMask & bit) != 0;
            if (blindMask & bit) { want |= bit; continue; }   // sensorless: always open

            float k = readFn(i);
            uint32_t gap = now - lastReadMs[i];
            if (lastReadMs[i] && gap && gap <= 4 * ctrlPeriodMs) {
                float s = (k - lastKpa[i]) / (float)gap;
                slope[i] += SLOPE_ALPHA * (s - slope[i]);
            } else {
                slope[i] = 0.0f;   // first read, or a gap (suspend) - not a slope
            }
            lastKpa[i]    = k;
            lastReadMs[i] = now;

            if (floorMask & bit) {
                // Blind vacuum: pull whenever the chamber has leaked up into
                // the visible band, else a timed trickle each period.
                bool visible = k > floorKpa[i] + OPEN_BAND_KPA;
                deficit[i] = visible ? k - floorKpa[i] : 0.0f;
                if (open) {
                    if (visible || (now - openSince[i]) < REPULL_ON_MS) want |= bit;
                } else if (visible || (now - lastPullMs[i]) >= REPULL_PERIOD_MS) {
                    want |= bit;
                }
                continue;
            }

            // Gauged (either side): signed axis toward the pump side.
            float sgn   = dirOf(i) ? -1.0f : 1.0f;
            float e     = sgn * (target[i] - k);               // > 0: needs pumping
            float ePred = e - sgn * slope[i] * (float)LEAD_MS; // deficit LEAD_MS ahead
            deficit[i]  = e;
            if (open) {
                // The valve stays open while the duty servo still has room
                // to cut: it closes only once the servo would need to go
                // UNDER the floor (base + KP * e <= floor) and the chamber
                // is at / heading past its target anyway.
                uint8_t d = dirOf(i);
                bool floored = base[d] + (int)(e * KP_PWM_PER_KPA) <= (int)DUTY_MIN;
                bool reached = floored
                            && (ePred <= 0.0f || e <= -OPEN_BAND_KPA)
                            && (now - openSince[i]) >= MIN_OPEN_MS;
                closeCnt[i] = reached ? (uint8_t)(closeCnt[i] + 1) : 0;
                if (closeCnt[i] < CLOSE_DEBOUNCE) want |= bit;
            } else if (e > OPEN_BAND_KPA) {
                want |= bit;
            }
        }
        return want;
    }

    // Phase 2: one target level per line. Among the GAUGED chambers of side d
    // that want to be open, newcomers join only if their target matches the
    // level already on the line (or the neediest newcomer's when the line is
    // idle). A mismatched newcomer waits while the line's chambers still
    // need air; once they all sit at their targets the line yields (they
    // close and re-open when they drift). A starving line admits everyone.
    uint16_t arbitrate(uint32_t now, uint8_t d, uint16_t want) {
        uint16_t gauged    = want & sideMask(d) & (uint16_t)~blindMask & (uint16_t)~floorMask;
        uint16_t online    = gauged & openMask;
        uint16_t newcomers = gauged & (uint16_t)~openMask;
        if (!newcomers) return want;

        float lineLevel = NAN;
        float best      = -INFINITY;
        bool  starving  = false;
        bool  satisfied = true;   // every line chamber at its target
        uint16_t pool   = online ? online : newcomers;
        for (int i = 0; i < count; i++) {
            if (!(pool & (1u << i))) continue;
            if (deficit[i] > best) { best = deficit[i]; lineLevel = target[i]; }
            if (online && (now - openSince[i]) >= PULSE_LONG_MS && deficit[i] > DEAD_KPA)
                starving = true;
            if (deficit[i] > DEAD_KPA) satisfied = false;
        }
        if (starving || isnan(lineLevel)) return want;

        uint16_t mismatched = 0;
        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if ((newcomers & bit) && fabsf(target[i] - lineLevel) > GROUP_TOL_KPA)
                mismatched |= bit;
        }
        if (!mismatched) return want;
        if (online && satisfied) {
            // Yield: the line's chambers are at their targets - hand the line
            // to the neediest waiting level (and whoever matches it).
            want &= ~online;
            float newLevel = NAN; best = -INFINITY;
            for (int i = 0; i < count; i++)
                if ((mismatched & (1u << i)) && deficit[i] > best) {
                    best = deficit[i]; newLevel = target[i];
                }
            for (int i = 0; i < count; i++) {
                uint16_t bit = (uint16_t)(1u << i);
                if ((newcomers & bit) && fabsf(target[i] - newLevel) > GROUP_TOL_KPA)
                    want &= ~bit;
            }
            return want;
        }
        return want & (uint16_t)~mismatched;   // wait for the line
    }

    // Phase 3: reconcile the valves with the wishes.
    template <class OpenFn, class CloseFn>
    void applyValves(uint32_t now, uint16_t want, OpenFn openFn, CloseFn closeFn) {
        for (int i = 0; i < count; i++) {
            if (!isHolding(i)) continue;
            uint16_t bit = (uint16_t)(1u << i);
            bool open = (openMask & bit) != 0;
            bool w    = (want & bit) != 0;
            if (w && !open) {
                openFn(i, dirOf(i));
                openMask    |= bit;
                openSince[i] = now;
                closeCnt[i]  = 0;
            } else if (!w && open) {
                closeFn(i, dirOf(i));
                openMask &= ~bit;
                if (floorMask & bit) lastPullMs[i] = now;
            }
        }
    }

    // Phase 4: the shared duty of side d for its open hold valves.
    uint8_t sideDuty(uint32_t now, uint32_t dt, uint8_t d) {
        uint16_t open = openMask & sideMask(d);
        if (!open) { lastDuty[d] = 0; return 0; }

        // The neediest gauged chamber on the line drives the servo (its
        // deficit may be negative: everyone above target -> cut the duty).
        float   need      = -INFINITY;
        float   needSlope = 0.0f;   // toward the pump side, kPa per ms
        bool    starving  = false;
        bool    anyGauged = false;
        uint8_t fixed     = 0;      // blind / re-pull holds run at their seed
        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if (!(open & bit)) continue;
            if ((blindMask | floorMask) & bit) {
                if (duty[i] > fixed) fixed = duty[i];
                continue;
            }
            anyGauged = true;
            if (deficit[i] > need) {
                need      = deficit[i];
                needSlope = dirOf(i) ? -slope[i] : slope[i];
            }
            if ((now - openSince[i]) >= PULSE_LONG_MS && deficit[i] > DEAD_KPA)
                starving = true;
        }
        if (!anyGauged) need = 0.0f;

        // Base = learned leak. Step up while below target and not already
        // rising (or starving), step down while above target and not already
        // falling - never during a transient the pump is already correcting.
        if (anyGauged && (int32_t)(now - integMs[d]) >= (int32_t)INTEG_MS) {
            integMs[d] = now;
            if (need > DEAD_KPA && (needSlope <= 0.0f || starving)) {
                if (base[d] < DUTY_MAX) base[d]++;
            } else if (need < -DEAD_KPA && needSlope >= 0.0f) {
                if (base[d] > DUTY_MIN) base[d]--;
            }
        }

        int want = base[d] + (int)(need * KP_PWM_PER_KPA + (need >= 0.0f ? 0.5f : -0.5f));
        if (fixed > want) want = fixed;
        if (want < DUTY_MIN) want = DUTY_MIN;
        if (want > DUTY_MAX) want = DUTY_MAX;

        // Soft start: slew up from wherever the pump is (0 when it was off).
        int maxStep = (int)(SLEW_PWM_PER_MS * (float)dt + 0.5f);
        if (maxStep < 1) maxStep = 1;
        int prev = lastDuty[d];
        if (want > prev + maxStep) want = prev + maxStep;

        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if ((open & bit) && !((blindMask | floorMask) & bit))
                duty[i] = (uint8_t)(want < DUTY_MIN ? DUTY_MIN : want);
        }
        lastDuty[d] = (uint8_t)want;
        return lastDuty[d];
    }
};

}  // namespace hold_duty

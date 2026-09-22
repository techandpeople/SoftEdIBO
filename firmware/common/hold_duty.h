#pragma once
#include <Arduino.h>
#include <math.h>
#include "pump_duty.h"     // shared PWM floors/ceiling (both boards + PC)

// ---------------------------------------------------------------------------
// Leak-compensating hold ("hold_duty") - shared by node_direct and
// node_multiplexed. Drives BOTH sides: a pressure hold (dir 0, inflate valve +
// pressure pump) and a vacuum hold (dir 1, deflate valve + vacuum pump).
//
// THE PROBLEM: a chamber "held" by closed valves may decay off its pose
// (leaky skin / tubing), and a chamber held with its valve OPEN loses air
// through the line (back through a stopped pump, the manifold) far faster
// than a closed one. A held chamber is therefore REGULATED on the node from
// its own gauge and from the LOSSES IT MEASURES IN REAL TIME. Nothing pulses
// unless the physics leave no other way.
//
// THE REGULATOR (v4):
//   * CLOSED FIRST. A hold starts with the valve closed, measuring the
//     chamber's closed-valve loss (kPa/s). A tight chamber - the log showed
//     11.6 kPa flat for 73 s - simply stays closed: no pump, no valve, no
//     bump. Only a chamber that has drifted OPEN_BAND below its target opens.
//   * CONTINUOUS DUTY. Once open the valve STAYS open and the pump PWM is
//     the regulator: duty = ff + KP * predicted deficit +- dither. The pump
//     runs down to pump_duty::RUN_MIN (the run floor, well under the start
//     floor: a spinning motor keeps turning far below the PWM it needs to
//     start), so a small line loss is balanced by a small continuous
//     trickle, not by on/off bursts at the start floor.
//   * MODEL-BASED FEEDFORWARD. ff is the predicted equilibrium duty. While
//     the line is open the engine samples (mean duty, reading slope) every
//     MODEL_WINDOW_MS and fits the line  slope = a * duty + b  by least
//     squares over the last MODEL_N windows. b is the loss of the open line
//     extrapolated to a stopped pump, a the pump's gain (kPa/s per PWM) at
//     this pressure; the root  ff = -b / a  is the duty where delivery
//     equals loss - the prediction the servo aims at. A +-DITHER_PWM square
//     wave keeps the fit identifiable at equilibrium (persistent excitation)
//     and is far too small to feel. Until the fit is trustworthy (enough
//     spread, positive gain) ff walks by a slow integral of the predicted
//     deficit (SEARCH_PWM_PER_S), seeded by the PC's calibrated duty.
//   * PREDICTION. The deficit the servo acts on is extrapolated LEAD_MS
//     ahead along the measured slope, so sensor / valve / pump latency
//     does not become overshoot.
//   * START KICK + LEARNED RUN FLOOR. A pump start is a KICK_MS burst at
//     pump_duty::MIN (the start floor) that then slews down to ff. If a
//     running pump stops delivering below the start floor (the reading
//     falls for STALL_MS although the chamber needs air), the side's run
//     floor is raised by STALL_STEP above that duty and the pump is
//     re-kicked: the floor is learned from the hardware, not assumed.
//   * THE ONLY PULSE LEFT. When ff sits ON the run floor - even the
//     slowest trickle over-delivers - and the chamber has reached its
//     target, the valve closes (predictively, CLOSE_DEBOUNCE ticks; a
//     chamber a whole OVER_KPA past its target closes as soon as the
//     command is on the floor, whatever ff says). A
//     tight chamber then stays closed for good. A leaky one re-opens once
//     it has lost OPEN_BAND, at the run floor duty, not the start floor:
//     the physical limit of a pump that cannot trickle any slower.
//   * ONE LEVEL PER LINE. One pump feeds all chambers of a side through one
//     line with no check valves, so co-open chambers equalise and the higher
//     one is robbed by the lower. Only chambers whose targets lie within
//     GROUP_TOL_KPA of each other share the line: a chamber at another level
//     waits while the line's chambers still need air, and the line YIELDS
//     to it (its satisfied chambers close, to re-open when they drift) once
//     they sit at their targets. A starving line lets every needy chamber in.
//
// VACUUM (dir 1): the same regulator mirrored - the gauge axis is flipped so
// "deficit" always means "needs more pumping". A target the gauge can SEE
// (the -40..40 sensor, or a shallow vacuum above the tared floor) is held
// exactly like a pressure target. A target BELOW the gauge floor (the blind
// 0..100 sensor reads its floor for anything deeper) cannot be servoed, so
// it is held by TIMED RE-PULLS: a short REPULL_ON_MS pull at the seed duty
// every REPULL_PERIOD_MS compensates the leak, and whenever the chamber has
// leaked up into the visible band (reading above floor + OPEN_BAND) it is
// pulled straight back to the floor.
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

// A closed chamber opens once it has drifted this far off its target toward
// the leak side; an open one closes (when the servo is floored) at target,
// or at once when it sits this far PAST it. Wide enough to ride gauge noise.
constexpr float OPEN_BAND_KPA = 0.25f;

// Inside this of the target the chamber counts as AT it.
constexpr float DEAD_KPA = 0.1f;

// Runaway guard: an open chamber this far PAST its target closes as soon as
// the pump command is down on the run floor (the normal close is the
// floored one). Not before: while the pump still pushes hard the gauge tee
// over-reads by the flow head, which vanishes as the servo cuts the duty.
constexpr float OVER_KPA = 1.0f;

// Prediction horizon: the deficit acted on is the reading extrapolated this
// far ahead along its measured slope (covers read + valve + pump latency).
constexpr uint32_t LEAD_MS     = 80;
// EMA weight of the per-tick slope estimate.
constexpr float    SLOPE_ALPHA = 0.3f;

// Predictive close confirmed on this many consecutive control ticks, never
// before MIN_OPEN_MS (rides out the start kick on the gauge).
constexpr uint8_t  CLOSE_DEBOUNCE = 2;
constexpr uint32_t MIN_OPEN_MS    = 160;

// Chambers whose targets lie within this of the level on the line share it.
constexpr float GROUP_TOL_KPA = 0.5f;

// A chamber still short of its target after this long open is starving: the
// line admits every needy chamber regardless of level.
constexpr uint32_t STARVE_MS = 1500;

// Pump duties: run floor (the servo's lower bound), start floor (the kick),
// ceiling. See pump_duty.h.
constexpr uint8_t DUTY_MIN  = pump_duty::RUN_MIN;
constexpr uint8_t DUTY_KICK = pump_duty::MIN;
constexpr uint8_t DUTY_MAX  = pump_duty::FULL;

// Pump start: KICK_MS at the start floor, then slew to the servo's duty.
constexpr uint32_t KICK_MS = 80;
// Duty slew (PWM per ms) outside the kick (which steps straight to the
// start floor: a motor that is not yet turning has nothing to soften).
constexpr float SLEW_UP_PWM_PER_MS   = 2.0f;
constexpr float SLEW_DOWN_PWM_PER_MS = 2.0f;

// Proportional term on the predicted deficit of the neediest open chamber.
// With a trusted model the gain is derived from the identified pump gain
// so the loop closes at KP_RATE_PER_S (1/s) whatever the chamber size:
// kp = KP_RATE_PER_S / a, clamped; without one, KP_DEFAULT.
constexpr float KP_DEFAULT_PWM_PER_KPA = 15.0f;
constexpr float KP_RATE_PER_S          = 2.0f;
constexpr float KP_MIN_PWM_PER_KPA     = 5.0f;
constexpr float KP_MAX_PWM_PER_KPA     = 30.0f;

// Loss model: one (mean duty, reading slope) sample per window, least-squares
// line over the last MODEL_N samples. The fit is trusted only with enough
// duty spread (variance, PWM^2) and a positive gain (kPa/s per PWM). A window
// whose duty swung more than MODEL_MAX_STEP is a transient and is skipped.
// ff moves MODEL_BLEND of the way to the fitted root each window.
constexpr uint32_t MODEL_WINDOW_MS = 200;
constexpr int      MODEL_N         = 16;
constexpr float    MODEL_MIN_VAR   = 4.0f;
constexpr float    MODEL_MIN_GAIN  = 0.001f;
constexpr int      MODEL_MAX_STEP  = 12;
constexpr float    MODEL_BLEND     = 0.5f;

// Persistent excitation: +-DITHER_PWM square wave, DITHER_HALF_MS per half.
constexpr int      DITHER_PWM     = 4;
constexpr uint32_t DITHER_HALF_MS = 500;

// Feedforward search while the model is not (yet) trusted: ff integrates the
// predicted deficit at this rate, scaled up to (1 + SEARCH_GAIN * |kPa|),
// at most SEARCH_MAX_STEP per window, and never on a transient window (the
// duty swung more than MODEL_MAX_STEP: the reading is still answering it).
constexpr float SEARCH_PWM_PER_S  = 40.0f;
constexpr float SEARCH_GAIN       = 2.0f;
constexpr float SEARCH_MAX_STEP   = 12.0f;
constexpr int   SEARCH_MAX_SWING  = 30;     // a window swinging more is a transient for the search too

// First feedforward of an unseeded side (the PC had no calibrated duty):
// predicted from the closed-valve loss the chamber measured before opening,
// run floor + LOSS_TO_PWM per kPa/s (a conservative, low prior pump gain);
// a tight chamber thus starts right on the floor. Without a loss reading
// yet, run floor + UNSEEDED_PWM.
constexpr float   LOSS_TO_PWM  = 20.0f;
constexpr uint8_t UNSEEDED_PWM = 30;

// Closed-valve loss: sampled per model window after the valve has settled.
// A fresh hold that already needs air waits for its first loss window (at
// most LOSS_WAIT_MS) before opening, so its first feedforward is predicted
// from a measured loss rather than guessed.
constexpr uint32_t CLOSED_SETTLE_MS = 200;
constexpr float    LOSS_ALPHA       = 0.3f;
constexpr uint32_t LOSS_WAIT_MS     = 600;

// The command has sat on the run floor this long (and the reading is not
// falling) before "even the slowest trickle over-delivers" is believed:
// a transient dip of the servo to the floor is not a reason to close.
constexpr uint32_t FLOOR_HOLD_MS = 400;

// Stall / no-delivery detector: the reading falls for STALL_MS while the
// chamber needs air and the pump runs under the start floor -> re-kick and
// raise the feedforward by STALL_STEP; when the pump was already ON the run
// floor, raise the floor itself by that much (it was not delivering there).
constexpr uint32_t STALL_MS   = 600;
constexpr uint8_t  STALL_STEP = 12;

// Vacuum target this close to (or below) the gauge floor is not visible
// enough to servo: it is held by timed re-pulls instead.
constexpr float FLOOR_MARGIN_KPA = 2.0f * OPEN_BAND_KPA;

// Blind vacuum re-pull: valve open for REPULL_ON_MS every REPULL_PERIOD_MS at
// the chamber's seed duty (>= the start floor).
constexpr uint32_t REPULL_PERIOD_MS = 2500;
constexpr uint32_t REPULL_ON_MS     = 120;

// Pump duties the engine wants, one per side (0 = that pump off).
struct Duties {
    uint8_t inflate;   // pressure pump(s), for the open dir-0 hold valves
    uint8_t deflate;   // vacuum pump(s), for the open dir-1 hold valves
};

// Per-side pump servo: feedforward, loss model, kick / stall bookkeeping.
struct SideServo {
    float    ff        = DUTY_KICK;   // predicted equilibrium duty
    bool     seeded    = false;       // ff primed (PC seed or measured loss)
    uint8_t  runFloor  = DUTY_MIN;    // learned lower bound of a delivering pump
    uint8_t  last      = 0;           // duty output last step (0 = pump off)
    uint32_t kickUntil = 0;
    uint32_t stallSince = 0;
    uint32_t floorSince = 0;          // command continuously on the run floor since
    uint32_t dither0   = 0;
    // Loss model ring: (mean duty, slope toward the pump side, kPa/s).
    float    mu[MODEL_N] = {};
    float    ms[MODEL_N] = {};
    uint8_t  mn = 0, mh = 0;
    bool     valid = false;
    float    a = 0.0f, b = 0.0f, root = NAN;
    // Current sample window.
    uint32_t winStart = 0;
    float    winK0    = 0.0f;   // line reading at window start
    float    winU     = 0.0f;   // duty accumulator
    uint16_t winTicks = 0;
    int      winUmin = 0, winUmax = 0;
    uint16_t winOpen  = 0;      // open set the window / model belong to

    void resetModel() { mn = mh = 0; valid = false; root = NAN; winStart = 0; }

    void push(float u, float s) {
        mu[mh] = u; ms[mh] = s;
        mh = (uint8_t)((mh + 1) % MODEL_N);
        if (mn < MODEL_N) mn++;
    }

    // Least-squares  s = a * u + b  over the ring; root = -b / a.
    void fit() {
        valid = false;
        if (mn < 4) return;
        float su = 0, ss = 0;
        for (int k = 0; k < mn; k++) { su += mu[k]; ss += ms[k]; }
        float um = su / mn, sm = ss / mn;
        float suu = 0, sus = 0;
        for (int k = 0; k < mn; k++) {
            float du = mu[k] - um;
            suu += du * du;
            sus += du * (ms[k] - sm);
        }
        float var = suu / mn;
        if (var < MODEL_MIN_VAR) return;
        a = sus / suu;
        if (a < MODEL_MIN_GAIN) return;
        b = sm - a * um;
        root = -b / a;
        valid = true;
    }
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
    uint32_t openSince[MAXN]   = {};  // when the valve opened
    uint32_t closedSince[MAXN] = {};  // when the valve closed (0 = never / open)
    uint32_t lastPullMs[MAXN]  = {};  // blind vacuum: when the last re-pull ended
    uint32_t lastReadMs[MAXN]  = {};  // slope estimator
    float    lastKpa[MAXN]     = {};
    float    slope[MAXN]       = {};  // kPa per ms, EMA
    float    deficit[MAXN]     = {};  // signed toward the pump side (>0 = needs pumping)
    float    predDeficit[MAXN] = {};  // deficit LEAD_MS ahead
    uint8_t  closeCnt[MAXN]    = {};  // consecutive "reached target" ticks
    float    lossClosed[MAXN]  = {};  // measured closed-valve loss, kPa/s (NAN = unknown)
    uint32_t lossWinMs[MAXN]   = {};
    float    lossWinKpa[MAXN]  = {};

    SideServo side[2];
    uint32_t  ctrlMs    = 0;
    bool      suspended = false;  // manifolds owned by someone else

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
        else if (fresh) duty[i] = DUTY_KICK;
        // The first hold on an idle side primes its servo with the seed - or
        // leaves it to predict its own from the loss it measures (seed 0). A
        // keepalive refresh must not: ff is what the servo has learned.
        if (fresh && !(sideMask(d) & ~bit)) {
            side[d].ff       = seed ? (float)s : (float)DUTY_KICK;
            side[d].seeded   = seed != 0;
            side[d].runFloor = DUTY_MIN;
            side[d].resetModel();
        }
        target[i]   = target_kpa;
        floorKpa[i] = floor_kpa;
        aliveMs[i]  = millis();
        if (d) dirMask |= bit; else dirMask &= ~bit;
        bool isBlind = blind || isnan(target_kpa);
        if (isBlind) blindMask |= bit; else blindMask &= ~bit;
        bool belowFloor = !isBlind && d == 1 && target_kpa < floor_kpa + FLOOR_MARGIN_KPA;
        if (belowFloor) floorMask |= bit; else floorMask &= ~bit;
        if (fresh) {
            openSince[i]   = 0;
            uint32_t t0 = millis();     // closed now: start measuring the loss
            closedSince[i] = t0 ? t0 : 1;
            lastPullMs[i]  = millis();  // the deflate/inflate just ran: first re-pull after a period
            lastReadMs[i]  = 0;
            slope[i]       = 0.0f;
            deficit[i]     = 0.0f;
            predDeficit[i] = 0.0f;
            closeCnt[i]    = 0;
            lossClosed[i]  = NAN;
            lossWinMs[i]   = 0;
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
            if (!sideMask((uint8_t)d)) side[d].last = 0;
    }

    // Hard reset (emergency stop / vent / test_run took the hardware). The
    // caller has already slammed everything off; this only clears state.
    void abort() {
        activeMask = 0;
        openMask   = 0;
        blindMask  = 0;
        floorMask  = 0;
        side[0].last = side[1].last = 0;
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
            side[0].last = side[1].last = 0;
            return Duties{0, 0};
        }

        // Someone else owns the manifolds: close only OUR valves and wait.
        if (busy) {
            if (!suspended) {
                for (int i = 0; i < count; i++)
                    if (openMask & (1u << i)) { closeFn(i, dirOf(i)); closedSince[i] = now; lossWinMs[i] = 0; }
                openMask  = 0;
                suspended = true;
                side[0].last = side[1].last = 0;
            }
            return Duties{0, 0};
        }
        suspended = false;

        // Between control steps the outputs stand.
        if ((int32_t)(now - ctrlMs) < (int32_t)ctrlPeriodMs)
            return Duties{side[0].last, side[1].last};
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

    // Measured closed-valve loss of chamber i (kPa/s toward the leak side),
    // NAN until a window has been measured. Diagnostics.
    float closedLoss(int i) const { return (i >= 0 && i < count) ? lossClosed[i] : NAN; }

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
                predDeficit[i] = deficit[i];
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
            deficit[i]     = e;
            predDeficit[i] = ePred;
            if (open) {
                // The valve stays open while the pump can still trickle
                // slower: it closes only when the pump is ON the run floor
                // (the predicted equilibrium sits there, or the command
                // does and the reading still is not falling - even the
                // slowest trickle over-delivers) and the chamber is at /
                // heading past its target - or at once when it sits a whole
                // OVER_KPA past it.
                const SideServo& S = side[dirOf(i)];
                bool floored = S.ff <= (float)S.runFloor + 1.0f
                            || (S.floorSince && (now - S.floorSince) >= FLOOR_HOLD_MS
                                && sgn * slope[i] >= 0.0f);
                bool atFloor = S.last <= S.runFloor + DITHER_PWM;
                bool reached = (floored && ePred <= 0.0f) || (atFloor && e <= -OVER_KPA);
                bool matured = (now - openSince[i]) >= MIN_OPEN_MS;
                closeCnt[i] = (reached && matured) ? (uint8_t)(closeCnt[i] + 1) : 0;
                if (closeCnt[i] < CLOSE_DEBOUNCE) want |= bit;
            } else {
                // Closed: measure the loss once the valve has settled.
                if (closedSince[i] && (now - closedSince[i]) >= CLOSED_SETTLE_MS) {
                    if (!lossWinMs[i]) {
                        lossWinMs[i] = now; lossWinKpa[i] = k;
                    } else if ((now - lossWinMs[i]) >= MODEL_WINDOW_MS) {
                        float loss = sgn * (lossWinKpa[i] - k) * 1000.0f
                                   / (float)(now - lossWinMs[i]);
                        lossClosed[i] = isnan(lossClosed[i]) ? loss
                                      : lossClosed[i] + LOSS_ALPHA * (loss - lossClosed[i]);
                        lossWinMs[i] = now; lossWinKpa[i] = k;
                    }
                }
                bool lossKnown = !isnan(lossClosed[i])
                              || !closedSince[i]
                              || (now - closedSince[i]) >= LOSS_WAIT_MS;
                if (e > OPEN_BAND_KPA && lossKnown) want |= bit;
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
            if (online && (now - openSince[i]) >= STARVE_MS && deficit[i] > DEAD_KPA)
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
                openMask      |= bit;
                openSince[i]   = now;
                closedSince[i] = 0;
                closeCnt[i]    = 0;
            } else if (!w && open) {
                closeFn(i, dirOf(i));
                openMask      &= ~bit;
                closedSince[i] = now;
                lossWinMs[i]   = 0;
                if (floorMask & bit) lastPullMs[i] = now;
            }
        }
    }

    // Phase 4: the shared duty of side d for its open hold valves.
    uint8_t sideDuty(uint32_t now, uint32_t dt, uint8_t d) {
        SideServo& S = side[d];
        uint16_t open = openMask & sideMask(d);
        if (!open) {
            S.last = 0; S.winStart = 0; S.stallSince = 0; S.floorSince = 0;
            return 0;
        }
        float sgn = d ? -1.0f : 1.0f;

        // The line: the neediest gauged chamber drives the servo (its deficit
        // may be negative - everyone above target - which cuts the duty); the
        // model sees the mean reading of the open gauged chambers.
        float   need      = -INFINITY;
        float   needPred  = -INFINITY;
        float   needSlope = 0.0f;   // toward the pump side, kPa per ms
        float   lineK     = 0.0f;
        int     nGauged   = 0;
        uint8_t fixed     = 0;      // blind / re-pull holds run at their seed
        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if (!(open & bit)) continue;
            if ((blindMask | floorMask) & bit) {
                uint8_t f = duty[i] < DUTY_KICK ? DUTY_KICK : duty[i];
                if (f > fixed) fixed = f;
                continue;
            }
            nGauged++;
            lineK += lastKpa[i];
            if (deficit[i] > need) {
                need      = deficit[i];
                needPred  = predDeficit[i];
                needSlope = sgn * slope[i];
            }
        }
        bool anyGauged = nGauged > 0;
        if (anyGauged) lineK /= (float)nGauged;
        else { need = needPred = 0.0f; }

        // Pump start: kick at the start floor, then descend to ff. An
        // unseeded side predicts its first ff from the closed-valve loss
        // the opening chambers measured while they waited.
        if (S.last == 0) {
            S.kickUntil  = now + KICK_MS;
            S.dither0    = now;
            S.stallSince = 0;
            S.winStart   = 0;
            if (!S.seeded && anyGauged) {
                float loss = NAN;
                for (int i = 0; i < count; i++) {
                    uint16_t bit = (uint16_t)(1u << i);
                    if ((open & bit) && !((blindMask | floorMask) & bit)
                        && !isnan(lossClosed[i]) && (isnan(loss) || lossClosed[i] > loss))
                        loss = lossClosed[i];
                }
                S.ff = isnan(loss) ? (float)S.runFloor + (float)UNSEEDED_PWM
                     : (float)S.runFloor + LOSS_TO_PWM * (loss > 0.0f ? loss : 0.0f);
                if (S.ff > (float)DUTY_MAX) S.ff = (float)DUTY_MAX;
                S.seeded = true;
            }
        }
        bool kicking = (int32_t)(now - S.kickUntil) < 0;

        // Loss model: one sample per window while the line runs steadily.
        if (anyGauged && !kicking) {
            if (open != S.winOpen) { S.resetModel(); S.winOpen = open; }
            if (!S.winStart) {
                S.winStart = now; S.winK0 = lineK;
                S.winU = 0.0f; S.winTicks = 0;
                S.winUmin = S.winUmax = S.last;
            } else {
                S.winU += (float)S.last; S.winTicks++;
                if (S.last < S.winUmin) S.winUmin = S.last;
                if (S.last > S.winUmax) S.winUmax = S.last;
                uint32_t w = now - S.winStart;
                if (w >= MODEL_WINDOW_MS) {
                    float um = S.winU / (float)S.winTicks;
                    float sl = sgn * (lineK - S.winK0) * 1000.0f / (float)w;
                    int swing = S.winUmax - S.winUmin;
                    if (swing <= MODEL_MAX_STEP) S.push(um, sl);
                    S.fit();
                    if (S.valid) {
                        float r = S.root;
                        if (r < (float)S.runFloor) r = (float)S.runFloor;
                        if (r > (float)DUTY_MAX)   r = (float)DUTY_MAX;
                        S.ff += MODEL_BLEND * (r - S.ff);
                    } else if (swing <= SEARCH_MAX_SWING
                               && (needPred > DEAD_KPA || needPred < -DEAD_KPA)) {
                        float mag  = fabsf(needPred);
                        float step = SEARCH_PWM_PER_S * (1.0f + SEARCH_GAIN * (mag > 1.0f ? 1.0f : mag))
                                   * (float)w / 1000.0f;
                        if (step > SEARCH_MAX_STEP) step = SEARCH_MAX_STEP;
                        S.ff += needPred > 0.0f ? step : -step;
                    }
                    if (S.ff < (float)S.runFloor) S.ff = (float)S.runFloor;
                    if (S.ff > (float)DUTY_MAX)   S.ff = (float)DUTY_MAX;
                    S.winStart = now; S.winK0 = lineK;
                    S.winU = 0.0f; S.winTicks = 0;
                    S.winUmin = S.winUmax = S.last;
                }
            }
        } else {
            S.winStart = 0;
        }

        // Duty command: feedforward + proportional on the predicted deficit
        // + dither; a fixed-duty hold on the line never runs under its seed.
        float want;
        if (anyGauged) {
            int dith = (((now - S.dither0) / DITHER_HALF_MS) & 1) ? DITHER_PWM : -DITHER_PWM;
            float kp = KP_DEFAULT_PWM_PER_KPA;
            if (S.valid) {
                kp = KP_RATE_PER_S / S.a;
                if (kp < KP_MIN_PWM_PER_KPA) kp = KP_MIN_PWM_PER_KPA;
                if (kp > KP_MAX_PWM_PER_KPA) kp = KP_MAX_PWM_PER_KPA;
            }
            want = S.ff + kp * needPred + (float)dith;
            if ((float)fixed > want) want = (float)fixed;
        } else {
            want = (float)fixed;
        }
        if (kicking && want < (float)DUTY_KICK) want = (float)DUTY_KICK;
        if (want < (float)S.runFloor) want = (float)S.runFloor;
        if (want > (float)DUTY_MAX)   want = (float)DUTY_MAX;

        // Stall / no delivery: falling for STALL_MS under the start floor
        // although the chamber needs air -> re-kick and push ff up; a pump
        // that was sitting ON the run floor learns the floor up instead.
        if (anyGauged && !kicking && S.last < DUTY_KICK && need > DEAD_KPA && needSlope < 0.0f) {
            if (!S.stallSince) S.stallSince = now;
            else if ((now - S.stallSince) >= STALL_MS) {
                if (S.last <= S.runFloor + DITHER_PWM) {
                    int nf = (int)S.runFloor + STALL_STEP;
                    if (nf > DUTY_KICK) nf = DUTY_KICK;
                    S.runFloor = (uint8_t)nf;
                }
                S.ff += (float)STALL_STEP;
                if (S.ff < (float)S.runFloor) S.ff = (float)S.runFloor;
                if (S.ff > (float)DUTY_MAX)   S.ff = (float)DUTY_MAX;
                S.kickUntil  = now + KICK_MS;
                S.stallSince = 0;
                S.resetModel();
                want = (float)DUTY_KICK;
            }
        } else {
            S.stallSince = 0;
        }

        // Slew: up fast, down gently (never a step the skin could feel);
        // the kick itself steps straight to the start floor.
        int w = (int)(want + 0.5f);
        int prev = S.last;
        int maxUp   = (int)(SLEW_UP_PWM_PER_MS * (float)dt + 0.5f);
        int maxDown = (int)(SLEW_DOWN_PWM_PER_MS * (float)dt + 0.5f);
        if (maxUp < 1) maxUp = 1;
        if (maxDown < 1) maxDown = 1;
        if (kicking) {
            if (w < DUTY_KICK) w = DUTY_KICK;
        } else if (w > prev + maxUp) {
            w = prev + maxUp;
        } else if (prev > 0 && w < prev - maxDown) {
            w = prev - maxDown;
        }

        if (!kicking && w <= S.runFloor + DITHER_PWM) {
            if (!S.floorSince) S.floorSince = now;
        } else {
            S.floorSince = 0;
        }

        for (int i = 0; i < count; i++) {
            uint16_t bit = (uint16_t)(1u << i);
            if ((open & bit) && !((blindMask | floorMask) & bit))
                duty[i] = (uint8_t)(w < DUTY_MIN ? DUTY_MIN : w);
        }
        S.last = (uint8_t)w;
        return S.last;
    }
};

}  // namespace hold_duty

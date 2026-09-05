#pragma once
#include <Arduino.h>
#include <math.h>

// ---------------------------------------------------------------------------
// Coupled-fill supervisor - shared by node_direct and node_multiplexed.
//
// THE HARDWARE PROBLEM THIS SOLVES
// Both actuator boards put every chamber of one direction on a SINGLE shared
// pneumatic line: the inflate valves all tap a common pressure manifold (1 pump
// on direct, 3 on multiplexed), the deflate valves a common vacuum manifold.
// There are NO check valves. So while two or more valves of the same direction
// are open, all those chambers EQUALISE - and each chamber's in-line gauge reads
// the shared line, not its own chamber. Per-chamber closed-loop cutoff therefore
// fires on the wrong pressure (one chamber's gauge briefly seeing the line trips
// the others' cutoff). That is why "inflate the whole skin" never settled right.
//
// THE ALGORITHM ("actuate coupled, close progressively, verify isolated")
//   GROUPING - a chamber requested in this direction starts a short coalescing
//              window so sibling per-chamber frames (the runtime sends one frame
//              PER chamber, not a broadcast) are batched and OPEN TOGETHER.
//   FILLING  - open every chamber in the group at once + run the pumps. While
//              coupled every open chamber EQUALISES with the line, so when the
//              line reaches the LOWEST open target that chamber holds exactly
//              its target - close JUST IT (progressive close) and keep pumping
//              the rest without stopping. The line keeps moving to the next
//              target; chambers close one by one in target order (inflate:
//              lowest max first; deflate: highest min first, since the line
//              falls). A pump-start spike is ignored for MIN_ROUND_MS and each
//              cutoff must persist CUTOFF_DEBOUNCE reads per chamber. A target
//              the gauge cannot see (deflate below the sensor floor) closes on
//              its per-chamber time budget (capMs, from the PC's calibrated
//              deflate curve) instead - and once its reading has BOTTOMED OUT
//              at the gauge floor the chamber is flagged AT-FLOOR (floorMask):
//              the pull goes on, but the board drops the vacuum pump to the
//              reduced pump_duty::DEFLATE_BELOW_FLOOR while every open deflate
//              valve is at the floor (the unsupervised part of the pull is
//              gentler). A BLIND chamber (board with no pressure
//              sensor populated - the PC sends "timed":1) is time-only both
//              ways: its floating ADC reading is noise, so the gauge never
//              opens, closes, or verifies it - only capMs does.
//   SETTLING - entered once, after the LAST open valve closes. Every gauge now
//              reads its OWN chamber, so after SETTLE_MS each requested chamber
//              is verified isolated (+/-tol); whatever is short re-opens for a
//              correction round. One settle per sequence instead of one per
//              round - the pumps only ever stop when everything has closed.
//
// One Engine instance drives ONE direction. A board owns two (inflate, deflate);
// they are independent because the two manifolds are independent. The board
// supplies the actuation (open/close a valve, recalc pumps) and a median-filtered
// gauge read through a small Ops bundle, plus a per-board Tuning (the multiplexed
// board is slower - I2C PCA9685 writes + a 16-channel mux scan - so its timings
// are looser). Pump COUNT scaling (multiplexed: ceil(open/3) pumps) is the board's
// recalc job; the engine only opens/closes valves and asks the board to recalc.
// ---------------------------------------------------------------------------

namespace coupled_fill {

// Per-board timing/threshold tuning. Direct is fast (dedicated ADC pins, direct
// GPIO valves); multiplexed is slow (I2C valve writes + mux settle per read), so
// it needs longer round/settle windows to not act on a half-actuated line.
struct Tuning {
    uint32_t group_window_ms;     // coalesce sibling per-chamber requests this long
    uint32_t min_round_single_ms; // 1-chamber round: ignore the cutoff only this long (tight)
    uint32_t min_round_ms;        // 2+-chamber round: ignore the cutoff this long (manifold kick)
    uint32_t settle_ms;           // settle after closing before measuring isolated
    uint32_t round_max_ms;        // per-round cap if nobody reaches target (leak/slow)
    uint32_t seq_max_ms;          // whole-sequence safety cap
    uint32_t chamber_max_ms;      // per-chamber cumulative open cap (sensor-fail / vacuum)
    uint8_t  cutoff_debounce;     // consecutive over-target reads before ending a round
    float    tol_frac;            // |target - kpa| within this*range counts as reached
    float    hard_max_kpa;        // single-sample inflate safety cut (no debounce)
};

enum Phase : uint8_t { IDLE = 0, GROUPING = 1, FILLING = 2, SETTLING = 3 };

// Debug event codes handed to Ops::dbg so the board can stream a wireless trace.
enum Event : uint8_t {
    EV_ROUND_OPEN   = 0,   // a round just opened a set of valves (mask = opened)
    EV_ROUND_END    = 1,   // a round closed all its valves (mask = was open)
    EV_MEASURE      = 2,   // isolated measure done (mask = still pending)
    EV_DONE         = 3,   // whole sequence finished (mask = 0)
    EV_ABORT        = 4,   // safety cap hit, sequence aborted (mask = was pending)
    EV_CHAMBER_DONE = 5,   // one chamber closed at its target (mask = still open)
    EV_AT_FLOOR     = 6,   // a deflating chamber's gauge bottomed out (mask = at-floor set)
};

// A reading within this band above the chamber's gauge floor counts as
// "bottomed out" - the ambient-zeroed reading sits at (P_MIN - zero) with a
// little ADC noise once the chamber is at/below what the sensor can see.
constexpr float FLOOR_BAND_KPA = 0.5f;

// Bundle of board callbacks. Built at the call site with ops(...) so the lambda
// types are deduced (no std::function, no heap - everything inlines).
//   readKpa(i)  -> median-filtered gauge for chamber i (also refreshes telemetry)
//   open(i,dir) -> open chamber i's valve for `dir` (0 inflate / 1 deflate)
//   close(i)    -> close chamber i (both valves), set it IDLE
//   recalc()    -> recompute the shared pumps from the live valve outputs
//   dbg(ev,mask)-> emit a debug event (no-op in release)
template <class RK, class OP, class CL, class RC, class DB>
struct Ops {
    RK readKpa;
    OP open;
    CL close;
    RC recalc;
    DB dbg;
};

template <class RK, class OP, class CL, class RC, class DB>
inline Ops<RK, OP, CL, RC, DB> ops(RK rk, OP op, CL cl, RC rc, DB db) {
    return Ops<RK, OP, CL, RC, DB>{rk, op, cl, rc, db};
}

// MAXN = compile-time chamber capacity (3 direct, 12 multiplexed). Masks are
// 16-bit so up to 16 chambers fit.
template <int MAXN>
struct Engine {
    uint8_t  dir   = 0;            // 0 = inflate, 1 = deflate (set once in begin)
    uint8_t  count = MAXN;         // runtime chamber count (board may use fewer)
    Tuning   tune{};

    Phase    phase       = IDLE;
    uint16_t pendingMask = 0;      // chambers wanting their target, not yet confirmed
    uint16_t openMask    = 0;      // chambers open in the current round
    uint8_t  roundCount  = 0;      // how many valves the last round opened
    uint32_t phaseMs     = 0;      // current phase start (millis)
    uint32_t seqStartMs  = 0;      // whole-sequence start (millis)

    float    target[MAXN]   = {};  // each chamber's absolute target kPa
    float    range[MAXN]    = {};  // each chamber's (max-min), for the reached tolerance
    uint32_t openedMs[MAXN] = {};  // when chamber i last opened (for accumOpenMs)
    uint32_t accumMs[MAXN]  = {};  // cumulative open time this sequence (safety cap)
    uint32_t capMs[MAXN]    = {};  // per-chamber open-time budget (0 = tune.chamber_max_ms)
    uint8_t  overCnt[MAXN]  = {};  // per-chamber consecutive at-target reads (cutoff debounce)
    uint16_t blindMask      = 0;   // chambers running open-loop: gauge ignored, capMs closes
    float    floorKpa[MAXN] = {};  // per-chamber gauge floor (lowest visible reading)
    uint16_t floorMask      = 0;   // open deflate chambers whose gauge bottomed out (latched until close)

    void begin(uint8_t direction, const Tuning& t) { dir = direction; tune = t; }

    bool active() const { return phase != IDLE; }
    bool isPending(int i) const { return (pendingMask >> i) & 1; }

    // Direction-aware "reached target" test. tol widens it (used for the isolated
    // measure so a small settle dip doesn't re-open a basically-full chamber); the
    // in-round cutoff passes tol = 0 to stop right at the target.
    bool reached(float k, int i, float tol) const {
        return dir == 0 ? (k >= target[i] - tol) : (k <= target[i] + tol);
    }

    // Effective open-time budget for chamber i: its own cap when the request set
    // one (e.g. a calibrated deflate time for a target the gauge can't see),
    // else the board tuning's global backstop.
    uint32_t capOf(int i) const {
        return capMs[i] ? capMs[i] : tune.chamber_max_ms;
    }

    // Add/refresh a target for chamber i. The board only calls this when chamber i
    // actually needs to move (it has already checked current-vs-target), so a
    // request always means "open this chamber for at least one round". A request
    // while IDLE starts the coalescing window; mid-cycle it joins the next round.
    // ``cap_ms`` (optional) bounds this chamber's total open time - the closing
    // authority for a target below the gauge floor; 0 keeps the tuning backstop.
    // ``blind`` runs the chamber fully open-loop (a board with no pressure
    // sensor populated - its ADC pin floats, so any reading is noise): the
    // gauge is ignored entirely, both for the in-round cutoff and the measure
    // verdict, and ``cap_ms`` (or the tuning backstop) is the only closer.
    // ``floor_kpa`` is the lowest reading this chamber's gauge can produce
    // (P_MIN minus its ambient zero); a deflate whose target lies below it is
    // flagged at-floor once the reading gets there (see floorMask). Leave the
    // default for inflate: nothing bottoms out on the way up.
    void request(int i, float target_kpa, float range_kpa, uint32_t cap_ms = 0,
                 bool blind = false, float floor_kpa = -INFINITY) {
        if (i < 0 || i >= count) return;
        target[i]   = target_kpa;
        range[i]    = range_kpa;
        capMs[i]    = cap_ms;
        floorKpa[i] = floor_kpa;
        if (blind) blindMask |=  (uint16_t)(1u << i);
        else       blindMask &= ~(uint16_t)(1u << i);
        floorMask &= ~(uint16_t)(1u << i);
        accumMs[i] = 0;
        bool wasEmpty = (pendingMask == 0);
        pendingMask |= (uint16_t)(1u << i);
        if (phase == IDLE) {
            phase      = GROUPING;
            phaseMs    = millis();
            seqStartMs = phaseMs;
        } else if (wasEmpty) {
            // pendingMask was emptied by the last measure but the phase had not
            // settled to IDLE yet - restart the sequence clock.
            seqStartMs = millis();
        }
    }

    // Drop chamber i from the sequence (opposite-direction command, hold or stop).
    // If it is open right now the board closes it via `closeFn`.
    template <class CloseFn>
    void drop(int i, CloseFn closeFn) {
        if (i < 0 || i >= count) return;
        uint16_t bit = (uint16_t)(1u << i);
        pendingMask &= ~bit;
        floorMask   &= ~bit;
        if (openMask & bit) { closeFn(i); openMask &= ~bit; }
        if (pendingMask == 0 && openMask == 0) phase = IDLE;
    }

    // True while at least one deflate valve is open AND every chamber the
    // board lists as open (``valve_mask``, from its live valve mirror - manual
    // and bench valves included) has bottomed out at its gauge floor: the
    // whole vacuum line is past what the sensor can see, so the board may run
    // the pump at the reduced below-floor duty. Any open valve NOT at the
    // floor (still visibly falling, or opened by hand) keeps full duty.
    bool lineAtFloor(uint16_t valve_mask) const {
        return valve_mask != 0 && (valve_mask & ~floorMask) == 0;
    }

    // Hard reset (emergency stop / manual override takeover). Caller has already
    // slammed the hardware off; this just clears engine state.
    void abort() {
        phase = IDLE;
        pendingMask = 0;
        openMask = 0;
        floorMask = 0;
    }

    // ---- internal: open every still-pending chamber together for a round ----
    template <class O>
    void openRound(uint32_t now, O& o) {
        openMask   = pendingMask;
        roundCount = __builtin_popcount(openMask);
        for (int i = 0; i < count; i++) {
            if (openMask & (1u << i)) {
                o.open(i, dir);
                openedMs[i] = now;
                overCnt[i]  = 0;
            }
        }
        o.recalc();
        phase     = FILLING;
        phaseMs   = now;
        o.dbg(EV_ROUND_OPEN, openMask);
    }

    // ---- internal: progressive close - one chamber reached its target (or its
    // time budget) while the rest keep filling. Closing while coupled is exact:
    // the chamber equalised with the line, so it traps the line's pressure. The
    // pumps re-scale to the remaining open valves; when the last one closes the
    // sequence moves to its single final SETTLING verify. ----
    template <class O>
    void closeOne(int i, uint32_t now, O& o) {
        accumMs[i] += now - openedMs[i];
        o.close(i);
        openMask  &= ~(uint16_t)(1u << i);
        floorMask &= ~(uint16_t)(1u << i);
        overCnt[i] = 0;
        o.recalc();
        o.dbg(EV_CHAMBER_DONE, openMask);
        if (openMask == 0) {
            phase   = SETTLING;
            phaseMs = now;
        }
    }

    // ---- internal: close all open valves, accrue their open time, go settle ----
    template <class O>
    void endRound(uint32_t now, O& o) {
        for (int i = 0; i < count; i++) {
            if (openMask & (1u << i)) {
                accumMs[i] += now - openedMs[i];
                o.close(i);
            }
        }
        uint16_t wasOpen = openMask;
        openMask  = 0;
        floorMask = 0;
        o.recalc();
        o.dbg(EV_ROUND_END, wasOpen);
        phase    = SETTLING;
        phaseMs  = now;
    }

    // Drive the round -> settle -> measure cycle. Call every control tick with a
    // fresh Ops bundle. Cheap when idle.
    template <class O>
    void tick(uint32_t now, O& o) {
        if (phase == IDLE) return;

        // Whole-sequence safety cap: slam any open valves shut and give up.
        if ((int32_t)(now - seqStartMs) >= (int32_t)tune.seq_max_ms) {
            for (int i = 0; i < count; i++)
                if (openMask & (1u << i)) o.close(i);
            openMask  = 0;
            floorMask = 0;
            o.recalc();
            o.dbg(EV_ABORT, pendingMask);
            abort();
            return;
        }

        switch (phase) {
        case GROUPING:
            if ((int32_t)(now - phaseMs) >= (int32_t)tune.group_window_ms)
                openRound(now, o);
            break;

        case FILLING: {
            // Safety paths that end the WHOLE round (close everything -> settle):
            // the inflate hard ceiling (immediate, no debounce) and the round cap
            // (nobody progressing - leak or a pump problem).
            bool endAll = ((int32_t)(now - phaseMs) >= (int32_t)tune.round_max_ms);
            // Ignore target cutoffs until the pump-start transient on the shared
            // line has passed; a single-chamber round was never coupled so it only
            // needs the short gate. HARD_MAX and the time caps bypass the gate.
            uint32_t gate = (roundCount > 1) ? tune.min_round_ms : tune.min_round_single_ms;
            bool gateOpen = (int32_t)(now - phaseMs) >= (int32_t)gate;
            for (int i = 0; i < count && !endAll; i++) {
                if (!(openMask & (1u << i))) continue;
                bool blind = (blindMask >> i) & 1;
                // Per-chamber time budget: a stuck gauge, a target the gauge
                // can't see (deflate below the sensor floor) or a blind chamber
                // closing on its calibrated time. Closes just this chamber.
                if (accumMs[i] + (now - openedMs[i]) >= capOf(i)) {
                    closeOne(i, now, o);
                    continue;
                }
                // A blind chamber's ADC pin floats - its reading is noise, so
                // neither the hard ceiling nor the target cutoff may act on it.
                if (blind) continue;
                float k = o.readKpa(i);
                if (dir == 0 && k >= tune.hard_max_kpa) { endAll = true; break; }
                // Deflate past the gauge floor: the reading has bottomed out
                // and the target lies below it, so from here the pull is
                // time-only (capMs). Latch AT-FLOOR and let the board drop the
                // vacuum pump to the reduced duty once every open valve is
                // there (recalc consults lineAtFloor).
                uint16_t bit = (uint16_t)(1u << i);
                if (dir == 1 && !(floorMask & bit) && target[i] < floorKpa[i]
                    && k <= floorKpa[i] + FLOOR_BAND_KPA) {
                    floorMask |= bit;
                    o.recalc();
                    o.dbg(EV_AT_FLOOR, floorMask);
                }
                // Progressive close: every open chamber equalises with the line,
                // so this chamber reading its own target means the line got there
                // - it is done. Debounced per chamber so a spike can't close it.
                if (reached(k, i, 0.0f)) {
                    if (gateOpen && ++overCnt[i] >= tune.cutoff_debounce)
                        closeOne(i, now, o);
                } else {
                    overCnt[i] = 0;
                }
            }
            if (endAll && phase == FILLING) endRound(now, o);
            break;
        }

        case SETTLING: {
            if ((int32_t)(now - phaseMs) < (int32_t)tune.settle_ms) {
                // A round that opened a single chamber was never coupled, so its
                // fill reading was already trustworthy - no need to wait the full
                // settle before measuring it.
                if (roundCount > 1) break;
            }
            for (int i = 0; i < count; i++) {
                if (!(pendingMask & (1u << i))) continue;
                // A chamber that spent its time budget counts as done even when
                // its gauge disagrees - for a target below the sensor floor the
                // budget IS the closing authority, and re-opening it would loop.
                // A blind chamber (no sensor populated) is time-ONLY: its noise
                // reading may neither confirm nor deny the target.
                bool done = accumMs[i] >= capOf(i);
                if (!done && !((blindMask >> i) & 1)) {
                    float tol = tune.tol_frac * range[i];
                    done = reached(o.readKpa(i), i, tol);
                }
                if (done) pendingMask &= ~(uint16_t)(1u << i);
            }
            o.dbg(EV_MEASURE, pendingMask);
            if (pendingMask == 0) {
                phase = IDLE;
                o.dbg(EV_DONE, 0);
            } else {
                openRound(now, o);   // re-open whatever is still short (higher round)
            }
            break;
        }

        default:
            break;
        }
    }
};

}  // namespace coupled_fill

#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Coupled-fill supervisor — shared by node_direct and node_multiplexed.
//
// THE HARDWARE PROBLEM THIS SOLVES
// Both actuator boards put every chamber of one direction on a SINGLE shared
// pneumatic line: the inflate valves all tap a common pressure manifold (1 pump
// on direct, 3 on multiplexed), the deflate valves a common vacuum manifold.
// There are NO check valves. So while two or more valves of the same direction
// are open, all those chambers EQUALISE — and each chamber's in-line gauge reads
// the shared line, not its own chamber. Per-chamber closed-loop cutoff therefore
// fires on the wrong pressure (one chamber's gauge briefly seeing the line trips
// the others' cutoff). That is why "inflate the whole skin" never settled right.
//
// THE ALGORITHM (the user's "actuate coupled, measure isolated")
//   GROUPING — a chamber requested in this direction starts a short coalescing
//              window so sibling per-chamber frames (the runtime sends one frame
//              PER chamber, not a broadcast) are batched and OPEN TOGETHER.
//   FILLING  — open every chamber in the group at once + run the pumps. While
//              coupled the line is the only trustworthy reading, so the round
//              ends when the line first reaches the LOWEST target among the open
//              chambers (== that chamber is done). A pump-start spike is ignored
//              for MIN_ROUND_MS and a real cutoff must persist CUTOFF_DEBOUNCE
//              reads, so a sensor spike never ends the round (or cuts a pump).
//   SETTLING — every valve is now shut, so after SETTLE_MS each gauge reads its
//              OWN chamber again. Drop the chambers that really reached target
//              (±tol); whatever is still short re-opens for the next, higher
//              round. The set shrinks each round; the last chamber opens ALONE,
//              so its gauge reads itself → precise. A reading taken while coupled
//              is never trusted.
//
// One Engine instance drives ONE direction. A board owns two (inflate, deflate);
// they are independent because the two manifolds are independent. The board
// supplies the actuation (open/close a valve, recalc pumps) and a median-filtered
// gauge read through a small Ops bundle, plus a per-board Tuning (the multiplexed
// board is slower — I2C PCA9685 writes + a 16-channel mux scan — so its timings
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
    EV_ROUND_OPEN = 0,   // a round just opened a set of valves (mask = opened)
    EV_ROUND_END  = 1,   // a round closed all its valves (mask = was open)
    EV_MEASURE    = 2,   // isolated measure done (mask = still pending)
    EV_DONE       = 3,   // whole sequence finished (mask = 0)
    EV_ABORT      = 4,   // safety cap hit, sequence aborted (mask = was pending)
};

// Bundle of board callbacks. Built at the call site with ops(...) so the lambda
// types are deduced (no std::function, no heap — everything inlines).
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
    uint8_t  overCount   = 0;      // consecutive over-target reads (round-cutoff debounce)
    uint32_t phaseMs     = 0;      // current phase start (millis)
    uint32_t seqStartMs  = 0;      // whole-sequence start (millis)

    float    target[MAXN]   = {};  // each chamber's absolute target kPa
    float    range[MAXN]    = {};  // each chamber's (max-min), for the reached tolerance
    uint32_t openedMs[MAXN] = {};  // when chamber i last opened (for accumOpenMs)
    uint32_t accumMs[MAXN]  = {};  // cumulative open time this sequence (safety cap)

    void begin(uint8_t direction, const Tuning& t) { dir = direction; tune = t; }

    bool active() const { return phase != IDLE; }
    bool isPending(int i) const { return (pendingMask >> i) & 1; }

    // Direction-aware "reached target" test. tol widens it (used for the isolated
    // measure so a small settle dip doesn't re-open a basically-full chamber); the
    // in-round cutoff passes tol = 0 to stop right at the target.
    bool reached(float k, int i, float tol) const {
        return dir == 0 ? (k >= target[i] - tol) : (k <= target[i] + tol);
    }

    // Add/refresh a target for chamber i. The board only calls this when chamber i
    // actually needs to move (it has already checked current-vs-target), so a
    // request always means "open this chamber for at least one round". A request
    // while IDLE starts the coalescing window; mid-cycle it joins the next round.
    void request(int i, float target_kpa, float range_kpa) {
        if (i < 0 || i >= count) return;
        target[i] = target_kpa;
        range[i]  = range_kpa;
        accumMs[i] = 0;
        bool wasEmpty = (pendingMask == 0);
        pendingMask |= (uint16_t)(1u << i);
        if (phase == IDLE) {
            phase      = GROUPING;
            phaseMs    = millis();
            seqStartMs = phaseMs;
        } else if (wasEmpty) {
            // pendingMask was emptied by the last measure but the phase had not
            // settled to IDLE yet — restart the sequence clock.
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
        if (openMask & bit) { closeFn(i); openMask &= ~bit; }
        if (pendingMask == 0 && openMask == 0) phase = IDLE;
    }

    // Hard reset (emergency stop / manual override takeover). Caller has already
    // slammed the hardware off; this just clears engine state.
    void abort() {
        phase = IDLE;
        pendingMask = 0;
        openMask = 0;
        overCount = 0;
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
            }
        }
        o.recalc();
        overCount = 0;
        phase     = FILLING;
        phaseMs   = now;
        o.dbg(EV_ROUND_OPEN, openMask);
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
        o.recalc();
        o.dbg(EV_ROUND_END, openMask);
        openMask = 0;
        phase    = SETTLING;
        phaseMs  = now;
    }

    // Drive the round → settle → measure cycle. Call every control tick with a
    // fresh Ops bundle. Cheap when idle.
    template <class O>
    void tick(uint32_t now, O& o) {
        if (phase == IDLE) return;

        // Whole-sequence safety cap: slam any open valves shut and give up.
        if ((int32_t)(now - seqStartMs) >= (int32_t)tune.seq_max_ms) {
            for (int i = 0; i < count; i++)
                if (openMask & (1u << i)) o.close(i);
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
            bool end     = false;
            bool hardCut = false;
            bool anyReached = false;
            for (int i = 0; i < count; i++) {
                if (!(openMask & (1u << i))) continue;
                float k = o.readKpa(i);
                // The line is shared, so any open chamber hitting its own target
                // means the line reached the LOWEST open target → end the round.
                if (reached(k, i, 0.0f)) anyReached = true;
                // Inflate hard ceiling cuts immediately, no debounce (safety).
                if (dir == 0 && k >= tune.hard_max_kpa) hardCut = true;
                // Per-chamber cumulative-open safety (stuck gauge, or a deflate
                // into vacuum the gauge can't see): force the round to end.
                if (accumMs[i] + (now - openedMs[i]) >= tune.chamber_max_ms) end = true;
            }
            // Ignore the cutoff until the pump-start transient on the shared line
            // has passed; a single-chamber round was never coupled so it only needs
            // the short gate, a multi-chamber round rides out the bigger manifold
            // kick. HARD_MAX and the safety caps bypass the gate.
            uint32_t gate = (roundCount > 1) ? tune.min_round_ms : tune.min_round_single_ms;
            if (anyReached && (int32_t)(now - phaseMs) >= (int32_t)gate) {
                if (++overCount >= tune.cutoff_debounce) end = true;
            } else if (!anyReached) {
                overCount = 0;
            }
            if ((int32_t)(now - phaseMs) >= (int32_t)tune.round_max_ms) end = true;  // slow/leak
            if (hardCut) end = true;
            if (end) endRound(now, o);
            break;
        }

        case SETTLING: {
            if ((int32_t)(now - phaseMs) < (int32_t)tune.settle_ms) {
                // A round that opened a single chamber was never coupled, so its
                // fill reading was already trustworthy — no need to wait the full
                // settle before measuring it.
                if (roundCount > 1) break;
            }
            for (int i = 0; i < count; i++) {
                if (!(pendingMask & (1u << i))) continue;
                float tol = tune.tol_frac * range[i];
                float k   = o.readKpa(i);
                bool done = reached(k, i, tol)
                         || accumMs[i] >= tune.chamber_max_ms;
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

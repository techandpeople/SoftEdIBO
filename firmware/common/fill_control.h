#pragma once
#include <Arduino.h>

// Shared chamber fill-control POLICY for both actuator variants (direct &
// multiplexed). The *actuation* — which valves/pumps to drive — is genuinely
// board-specific and stays in each node's chambers.h. This header owns the
// board-agnostic policy so the safety constants and behaviour can't drift
// between the two boards:
//   - time-based fill window (a calibrated fill_time) with a hard ceiling, and
//   - idle leak maintenance: top a held chamber back up when it droops, with a
//     touch-safe, debounced trigger.
//
// Each Chamber struct must expose these fields:
//     uint32_t fill_until_ms;   // INFLATING: stop at this millis() (0 = none)
//     float    hold_kpa;        // IDLE: level to maintain against leaks (0 = none)
//     uint8_t  droop_count;     // consecutive idle checks below hold (debounce)
// State is read through node-supplied predicates, so each board keeps its own
// State enum.

namespace fill_control {

// Hard ceiling for a single time-based fill (mirrors the PC calibrator), so a
// bad fill_time can't run a pump indefinitely. The per-chamber HARD_MAX / max
// pressure cutoff is the independent backstop.
constexpr uint32_t MAX_FILL_MS = 5000;

// Idle leak maintenance tuning.
constexpr float    LEAK_MARGIN_KPA   = 0.5f;   // top up only after this much droop
constexpr float    MAINTAIN_MIN_KPA  = 0.5f;   // don't maintain near-empty chambers
constexpr uint32_t MAINTAIN_CHECK_MS = 2000;   // how often to re-check idle chambers
constexpr uint8_t  DROOP_DEBOUNCE    = 2;      // consecutive droop checks before a top-up

// millis() deadline for a time-based fill (0 = pressure-based / disabled).
inline uint32_t fillUntil(uint32_t fill_ms) {
    return fill_ms ? millis() + min(fill_ms, MAX_FILL_MS) : 0;
}

// Close any chamber whose time-based fill window elapsed, recording the achieved
// pressure as the level to maintain. `isInflating(ch)` is the node's state
// predicate; `stopHold(i, achievedKpa)` is its stop-and-record-hold action.
template<typename Chamber, typename IsInflating, typename StopHold>
void fillTimeTick(Chamber* st, const float* kpa, int n, uint32_t now,
                  IsInflating isInflating, StopHold stopHold) {
    for (int i = 0; i < n; i++) {
        Chamber& ch = st[i];
        if (isInflating(ch) && ch.fill_until_ms != 0
            && (int32_t)(now - ch.fill_until_ms) >= 0) {
            stopHold(i, kpa[i]);
        }
    }
}

// Idle leak maintenance. A held chamber is topped back up when its pressure
// droops past LEAK_MARGIN_KPA for DROOP_DEBOUNCE consecutive checks. The droop
// test is one-directional, so a touch — which *raises* pressure — never triggers
// a top-up and actually suppresses maintenance; the debounce ignores the brief
// dip a release can cause. `lastCheck` is the node-owned throttle timestamp.
// `isIdle(ch)` is the node's state predicate; `topUp(i, holdKpa)` opens a
// pressure-based top-up to holdKpa.
template<typename Chamber, typename IsIdle, typename TopUp>
void maintainTick(Chamber* st, const float* kpa, int n, uint32_t now,
                  uint32_t& lastCheck, IsIdle isIdle, TopUp topUp) {
    if (now - lastCheck < MAINTAIN_CHECK_MS) return;
    lastCheck = now;
    for (int i = 0; i < n; i++) {
        Chamber& ch = st[i];
        if (!isIdle(ch) || ch.hold_kpa <= MAINTAIN_MIN_KPA) {
            ch.droop_count = 0;
            continue;
        }
        if (kpa[i] < ch.hold_kpa - LEAK_MARGIN_KPA) {
            if (++ch.droop_count >= DROOP_DEBOUNCE) {
                ch.droop_count = 0;
                topUp(i, ch.hold_kpa);
            }
        } else {
            ch.droop_count = 0;   // recovered (or being touched → pressure up)
        }
    }
}

}  // namespace fill_control

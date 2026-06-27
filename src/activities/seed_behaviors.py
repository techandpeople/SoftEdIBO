"""Seed behaviour specs for the hospital study — the 3 conditions.

Each condition is a 3-phase timeline that heals the robot autonomously over
time, and can be hurried along by the child's touch. All three converge on the
same "cured" look (yellow ring, all chambers beating together):

- Condition A (Heartbeat): the *rhythm* changes — slow random beat → mixed
  amplitudes → strong synchronised beat.
- Condition B (Movement): chambers move *one at a time* (low energy) → mixed
  amplitudes → synchronised beat.
- Condition C (Texture): the *skin* changes — emptied / rough (chambers at
  0 %) → half empty / half smooth → smooth and breathing.

These are the comportamentos 1-3 / 4-6 / 7-9 from the study brief. They are
authored here as plain data so the same specs can later be loaded into the
Blockly block editor and tuned by the researcher. Phase transitions fire on
time **or** on enough touches, whichever comes first.
"""

from __future__ import annotations

from typing import Any

PURPLE = "#8e44ad"
YELLOW = "#f1c40f"
HALF = [PURPLE, YELLOW]          # half ring purple, half yellow

# Phase durations (ms) measured from the START of the session.
PHASE2_AFTER_MS = 120_000        # 2 min
PHASE3_AFTER_MS = 300_000        # 5 min
TOUCHES_TO_ADVANCE = 15          # touch shortcut for either transition


def _advance(to: str, since_state_ms: int) -> dict[str, Any]:
    """A transition that fires after a time in the current state OR after
    enough touches — encodes the study's 'time + interaction' rule."""
    return {
        "to": to,
        "when": {"any": [
            {"elapsed_ms": since_state_ms},
            {"touch_count": {"min": TOUCHES_TO_ADVANCE}},
        ]},
    }


# Phase 2 and phase 3 are identical across conditions (the conditions differ
# only in how phase 1 expresses the sickness), so build them once.
_PHASE2 = {
    "do": [
        {"set_led_halves": {"colors": HALF}},
        {"repeat": {"forever": True, "do": [
            {"beat": {"mode": "aligned", "aligned": 2,
                      "pct": 55, "pct2": 20, "period_ms": 4000}},
        ]}},
    ],
    "transitions": [_advance("phase3", PHASE3_AFTER_MS - PHASE2_AFTER_MS)],
}

_PHASE3 = {
    "do": [
        {"set_led": {"color": YELLOW, "pattern": "solid"}},
        {"repeat": {"forever": True, "do": [
            {"beat": {"mode": "sync", "pct": 70, "period_ms": 2000}},
        ]}},
    ],
    "transitions": [],
}


CONDITION_A = {
    "initial": "phase1",
    "states": {
        "phase1": {
            "do": [
                {"set_led": {"color": PURPLE, "pattern": "pulse",
                             "period_ms": 3000}},
                {"repeat": {"forever": True, "do": [
                    {"beat": {"mode": "random", "pct": 35, "period_ms": 6000}},
                ]}},
            ],
            "transitions": [_advance("phase2", PHASE2_AFTER_MS)],
        },
        "phase2": _PHASE2,
        "phase3": _PHASE3,
    },
}

CONDITION_B = {
    "initial": "phase1",
    "states": {
        "phase1": {
            "do": [
                {"set_led": {"color": PURPLE, "pattern": "pulse",
                             "period_ms": 3000}},
                {"repeat": {"forever": True, "do": [
                    {"beat": {"mode": "sequential", "pct": 45,
                              "period_ms": 5000}},
                ]}},
            ],
            "transitions": [_advance("phase2", PHASE2_AFTER_MS)],
        },
        "phase2": _PHASE2,
        "phase3": _PHASE3,
    },
}

CONDITION_C = {
    "initial": "phase1",
    "states": {
        # Emptied all over (rough, "no air"), dim purple.
        "phase1": {
            "do": [
                {"set_led": {"color": PURPLE, "pattern": "solid"}},
                {"for_each_chamber": {"do": [{"set_pressure": {"pct": 0}}]}},
            ],
            "transitions": [_advance("phase2", PHASE2_AFTER_MS)],
        },
        # Half air: chambers 0,1 emptied, chamber 2 smooth; half-colour ring.
        "phase2": {
            "do": [
                {"set_led_halves": {"colors": HALF}},
                {"set_pressure": {"chamber": 0, "pct": 0}},
                {"set_pressure": {"chamber": 1, "pct": 0}},
                {"set_pressure": {"chamber": 2, "pct": 55}},
            ],
            "transitions": [_advance("phase3", PHASE3_AFTER_MS - PHASE2_AFTER_MS)],
        },
        # Smooth and breathing together — cured.
        "phase3": _PHASE3,
    },
}


SEED_CONDITIONS: list[tuple[str, str, dict]] = [
    ("Behaviour A — Heartbeat",
     "Hospital study condition: heals by changing the heartbeat rhythm "
     "(slow random → mixed → strong synchronised).", CONDITION_A),
    ("Behaviour B — Movement",
     "Hospital study condition: chambers move one at a time, then converge on "
     "a synchronised beat.", CONDITION_B),
    ("Behaviour C — Texture",
     "Hospital study condition: skin goes from emptied / rough (chambers at "
     "0 %) to smooth and breathing.", CONDITION_C),
]

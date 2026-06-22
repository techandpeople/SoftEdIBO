"""Touch-gesture taxonomy — the label set + operational definitions.

Single source of truth shared by live labeling (observer panel), the rule
baseline, and the training script. Definitions are expressed in terms of
**sensor index / activation sequence**, not metric coordinates, because the
touch hardware is sparse (4 magnetic sensors per skin) and per-skin-type models
make the indices stable. Thresholds are tunable constants — refine them once
real labelled segments exist (the classes are exploratory).
"""

from __future__ import annotations

# Class labels.
TAP = "tap"
DOUBLE_TAP = "double_tap"
TRIPLE_TAP = "triple_tap"
PRESS = "press"
STROKE = "stroke"
SQUEEZE = "squeeze"
UNKNOWN = "unknown"

GESTURE_CLASSES: tuple[str, ...] = (
    TAP, DOUBLE_TAP, TRIPLE_TAP, PRESS, STROKE, SQUEEZE)

# Operational definitions (also used as button tooltips / docs).
DEFINITIONS: dict[str, str] = {
    TAP:        "Short contact, one dominant sensor, abrupt onset.",
    DOUBLE_TAP: "Two quick taps in a row (group the two touches as one).",
    TRIPLE_TAP: "Three quick taps in a row (group the three touches as one).",
    PRESS:      "Sustained contact, same sensor(s), stable.",
    STROKE:     "Distinct sensors activate in temporal sequence (movement).",
    SQUEEZE:    "High fraction of sensors active simultaneously.",
    UNKNOWN:    "Not classifiable / noise.",
}

# Tunable thresholds for the rule baseline (and as priors for ML). Durations in
# milliseconds. Refine against real data.
TAP_MAX_MS = 250          # at/under this and single-sensor → tap
PRESS_MIN_MS = 600        # at/over this and stable → press
SQUEEZE_MIN_ACTIVE_FRAC = 0.6   # fraction of sensors active at peak → squeeze
STROKE_MIN_DISTINCT = 2   # distinct sensors visited in sequence → stroke

"""Rule-based touch-gesture classifier — a baseline, not the product.

Implements the operational definitions in ``gesture_taxonomy`` directly on the
features. Used by the training script as a comparison point ("does the learned
model actually beat simple rules?"). With only ~4 sparse sensors per skin, pure
rules are not expected to be enough — that's the whole reason for the ML path —
so this is deliberately simple.
"""

from __future__ import annotations

from src.ml import gesture_taxonomy as tax
from src.ml.touch_features import extract_features
from src.ml.touch_segmenter import TouchSegment


def classify(seg: TouchSegment) -> str:
    """Best-effort gesture label from the segment's features (rules only)."""
    f = extract_features(seg)
    if f["n_samples"] == 0:
        return tax.UNKNOWN

    # Squeeze: many sensors active at once dominates.
    if f["active_frac_max"] >= tax.SQUEEZE_MIN_ACTIVE_FRAC:
        return tax.SQUEEZE
    # Stroke: distinct sensors activated in sequence (movement).
    if (f["is_sequential"] >= 1.0
            and f["n_distinct_sensors"] >= tax.STROKE_MIN_DISTINCT):
        return tax.STROKE
    # Tap vs press by duration.
    if f["duration_ms"] <= tax.TAP_MAX_MS:
        return tax.TAP
    if f["duration_ms"] >= tax.PRESS_MIN_MS:
        return tax.PRESS
    return tax.UNKNOWN

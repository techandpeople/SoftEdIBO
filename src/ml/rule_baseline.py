"""Rule-based touch reading - the baseline every trained model is compared to.

Reads a segment with the taxonomy's rules alone (see :mod:`src.ml.touch_axes`).
Used by the training script as a comparison point ("does the learned movement
model actually beat simple rules?") and live when no model was trained.
"""

from __future__ import annotations

from src.ml.touch_axes import DEFAULT_THRESHOLD_UT, read_touch
from src.ml.touch_features import extract_features
from src.ml.touch_axes import movement_by_rules
from src.ml.touch_segmenter import TouchSegment


def classify(seg: TouchSegment,
             threshold_ut: float = DEFAULT_THRESHOLD_UT) -> str:
    """Best-effort gesture label from the segment's features (rules only)."""
    return read_touch(seg, threshold_ut).gesture


def movement(seg: TouchSegment) -> str:
    """The segment's movement (static / slide / rub) by rules only."""
    return movement_by_rules(extract_features(seg))

"""Coordinate-free features for a touch segment.

Features depend only on sensor **index** and activation **sequence**, never on
metric positions — so they need no (often unreliable) geometry config and stay
valid within a skin type, where index *i* is always the same physical spot.
Pure Python (no numpy) so the runtime stays dependency-free.

``extract_features(segment) -> dict[str, float]`` returns a flat, ordered
feature dict. ``feature_vector`` flattens it to a list in a stable order for a
classifier.
"""

from __future__ import annotations

from src.hardware.skin_geometry import SKIN_VARIANTS
from src.ml.touch_segmenter import TouchSegment


def _safe_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def extract_features(seg: TouchSegment) -> dict[str, float]:
    """Compute the per-segment feature dict (coordinate-free, index-based)."""
    n_sensors = seg.sensor_count
    n_samples = len(seg.mags)
    feats: dict[str, float] = {
        "duration_ms": seg.duration_ms,
        "n_samples": float(n_samples),
        "n_sensors": float(n_sensors),
        "n_pulses": float(getattr(seg, "n_pulses", 1)),
    }
    if n_samples == 0 or n_sensors == 0:
        # Empty/degenerate segment — fill the schema with zeros so the feature
        # vector stays a fixed length.
        feats.update({
            "peak_mag": 0.0, "mean_mag": 0.0, "rise_ms": 0.0,
            "active_frac_max": 0.0, "active_frac_mean": 0.0,
            "n_distinct_sensors": 0.0, "n_transitions": 0.0,
            "is_sequential": 0.0,
        })
        return feats

    # --- Magnitude features (over all sensors/samples) ---
    all_vals = [v for vec in seg.mags for v in vec]
    feats["peak_mag"] = max(all_vals) if all_vals else 0.0
    feats["mean_mag"] = _safe_mean(all_vals)

    # Rise time: ms from start to the sample with the highest single reading.
    peak_idx = 0
    peak_val = -1.0
    for i, vec in enumerate(seg.mags):
        m = max(vec) if vec else 0.0
        if m > peak_val:
            peak_val, peak_idx = m, i
    feats["rise_ms"] = seg.times_ms[peak_idx] - seg.times_ms[0]

    # --- Activation features (fraction of sensors active) ---
    active_fracs = [len(a) / n_sensors for a in seg.acts]
    feats["active_frac_max"] = max(active_fracs) if active_fracs else 0.0
    feats["active_frac_mean"] = _safe_mean(active_fracs)

    # --- Sequence features (movement proxy, no coordinates) ---
    distinct: set[int] = set()
    for a in seg.acts:
        distinct |= a
    feats["n_distinct_sensors"] = float(len(distinct))

    # Transitions between consecutive active-sets (how much the touch moved).
    transitions = 0
    for prev, cur in zip(seg.acts, seg.acts[1:]):
        if prev != cur:
            transitions += 1
    feats["n_transitions"] = float(transitions)

    # Sequential = distinct sensors became active at different times (a stroke),
    # rather than all together (a press/squeeze). Compare first-activation order.
    first_seen: dict[int, int] = {}
    for i, a in enumerate(seg.acts):
        for s in a:
            first_seen.setdefault(s, i)
    distinct_onset_times = len(set(first_seen.values()))
    feats["is_sequential"] = 1.0 if (len(distinct) >= 2
                                     and distinct_onset_times >= 2) else 0.0
    return feats


# Stable feature order for vectorisation.
FEATURE_NAMES: tuple[str, ...] = (
    "duration_ms", "n_samples", "n_sensors", "n_pulses",
    "peak_mag", "mean_mag", "rise_ms",
    "active_frac_max", "active_frac_mean",
    "n_distinct_sensors", "n_transitions", "is_sequential",
)


def feature_vector(seg: TouchSegment) -> list[float]:
    """Flatten :func:`extract_features` to a fixed-order list for a model."""
    feats = extract_features(seg)
    return [float(feats.get(name, 0.0)) for name in FEATURE_NAMES]


# --- Silicone-variant feature (one-hot) -----------------------------------
# The skin's silicone format is orthogonal to its shape; the per-shape model is
# trained across variants, so the variant is fed in as a one-hot block. The
# variant list is single-sourced from the geometry registry (imported at top).
VARIANT_FEATURE_NAMES: tuple[str, ...] = tuple(f"variant_{v}" for v in SKIN_VARIANTS)


def variant_features(skin_variant: str) -> list[float]:
    """One-hot encode the silicone variant (all zeros when unset/unknown)."""
    return [1.0 if skin_variant == v else 0.0 for v in SKIN_VARIANTS]


def full_feature_vector(seg: TouchSegment, skin_variant: str = "") -> list[float]:
    """Model input: the segment features plus the one-hot silicone variant.

    Used by both training and inference so the vector length/order always match.
    """
    return feature_vector(seg) + variant_features(skin_variant)


FULL_FEATURE_NAMES: tuple[str, ...] = FEATURE_NAMES + VARIANT_FEATURE_NAMES

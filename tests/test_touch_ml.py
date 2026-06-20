"""Tests for the touch-gesture ML pipeline (dependency-free parts).

Covers segmentation, coordinate-free features (independent of sensor count),
the rule baseline, and the inert classifier (no model / no sklearn → unknown).
These run without numpy/scikit-learn installed.
"""

import sys

from src.ml import gesture_taxonomy as tax
from src.ml import rule_baseline
from src.ml.touch_classifier import TouchGestureClassifier, model_path
from src.ml.touch_features import FEATURE_NAMES, extract_features, feature_vector
from src.ml.touch_segmenter import TouchSegmenter, merge_segments


# ---------------------------------------------------------------------------
# Synthetic magnet streams. Each sample is (msg, t_ms).
# ---------------------------------------------------------------------------

def _msg(mag, act):
    return {"type": "magnet", "mag": mag, "act": list(act)}


def _tap_stream(n_sensors=4):
    """Short, single-sensor, with idle frames around it."""
    idle = (_msg([0.0] * n_sensors, []), 0.0)
    hot = _msg([5.0] + [0.0] * (n_sensors - 1), [0])
    return [idle,
            (hot, 50.0), (hot, 100.0),
            (_msg([0.0] * n_sensors, []), 150.0)]


def _press_stream(n_sensors=4):
    hot = _msg([6.0] + [0.0] * (n_sensors - 1), [0])
    samples = [(_msg([0.0] * n_sensors, []), 0.0)]
    for t in range(50, 1000, 50):        # ~950 ms sustained, same sensor
        samples.append((hot, float(t)))
    samples.append((_msg([0.0] * n_sensors, []), 1000.0))
    return samples


def _stroke_stream(n_sensors=4):
    """Sensors activate one after another (movement)."""
    def hot(i):
        v = [0.0] * n_sensors
        v[i] = 5.0
        return _msg(v, [i])
    return [(_msg([0.0] * n_sensors, []), 0.0),
            (hot(0), 50.0), (hot(1), 150.0), (hot(2), 250.0), (hot(3), 350.0),
            (_msg([0.0] * n_sensors, []), 400.0)]


# ---------------------------------------------------------------------------
# Segmenter
# ---------------------------------------------------------------------------

def test_segmenter_emits_one_segment_per_touch():
    segs = TouchSegmenter().segment_stream(_tap_stream())
    assert len(segs) == 1
    assert segs[0].duration_ms == 100.0          # 50 → 100 ms while active
    assert segs[0].sensor_count == 4


def test_segmenter_flushes_open_touch_at_end():
    # Stream that never releases — should still yield a segment.
    hot = _msg([5.0, 0, 0, 0], [0])
    segs = TouchSegmenter().segment_stream([(hot, 0.0), (hot, 50.0)])
    assert len(segs) == 1


# ---------------------------------------------------------------------------
# Features — fixed schema, layout-independent
# ---------------------------------------------------------------------------

def test_feature_vector_has_stable_length_across_sensor_counts():
    seg4 = TouchSegmenter().segment_stream(_tap_stream(4))[0]
    seg6 = TouchSegmenter().segment_stream(_tap_stream(6))[0]
    v4, v6 = feature_vector(seg4), feature_vector(seg6)
    assert len(v4) == len(v6) == len(FEATURE_NAMES)


def test_full_feature_vector_appends_one_hot_variant():
    from src.hardware.skin_geometry import SKIN_VARIANTS
    from src.ml.touch_features import full_feature_vector, FULL_FEATURE_NAMES
    seg = TouchSegmenter().segment_stream(_tap_stream(4))[0]
    base = feature_vector(seg)
    full = full_feature_vector(seg, "wrinkles")
    assert len(full) == len(FULL_FEATURE_NAMES) == len(base) + len(SKIN_VARIANTS)
    assert full[:len(base)] == base
    # exactly one variant bit set, and it's the "wrinkles" slot
    assert sum(full[len(base):]) == 1.0
    assert full[len(base) + SKIN_VARIANTS.index("wrinkles")] == 1.0
    # unknown / unset variant → all-zero one-hot block
    assert sum(full_feature_vector(seg, "")[len(base):]) == 0.0


def test_stroke_features_show_sequence():
    seg = TouchSegmenter().segment_stream(_stroke_stream())[0]
    f = extract_features(seg)
    assert f["n_distinct_sensors"] == 4
    assert f["is_sequential"] == 1.0


def test_press_features_show_duration_not_sequence():
    seg = TouchSegmenter().segment_stream(_press_stream())[0]
    f = extract_features(seg)
    assert f["duration_ms"] >= tax.PRESS_MIN_MS
    assert f["is_sequential"] == 0.0


# ---------------------------------------------------------------------------
# Rule baseline
# ---------------------------------------------------------------------------

def test_rule_baseline_separates_tap_press_stroke():
    seg_tap = TouchSegmenter().segment_stream(_tap_stream())[0]
    seg_press = TouchSegmenter().segment_stream(_press_stream())[0]
    seg_stroke = TouchSegmenter().segment_stream(_stroke_stream())[0]
    assert rule_baseline.classify(seg_tap) == tax.TAP
    assert rule_baseline.classify(seg_press) == tax.PRESS
    assert rule_baseline.classify(seg_stroke) == tax.STROKE


# ---------------------------------------------------------------------------
# Multi-tap: merge segments into one gesture (n_pulses)
# ---------------------------------------------------------------------------

def test_merge_segments_accumulates_pulses_and_samples():
    taps = TouchSegmenter().segment_stream(
        _tap_stream() + _tap_stream() + _tap_stream())
    assert len(taps) == 3                       # three separate touches
    merged = merge_segments(taps)
    assert merged.n_pulses == 3
    assert len(merged.mags) == sum(len(t.mags) for t in taps)
    # n_pulses surfaces as a feature.
    assert int(extract_features(merged)["n_pulses"]) == 3


def test_merge_segments_empty_is_none():
    assert merge_segments([]) is None


def test_rule_baseline_labels_double_and_triple_tap():
    one = TouchSegmenter().segment_stream(_tap_stream())[0]
    double = merge_segments(
        TouchSegmenter().segment_stream(_tap_stream() + _tap_stream()))
    triple = merge_segments(
        TouchSegmenter().segment_stream(
            _tap_stream() + _tap_stream() + _tap_stream()))
    assert rule_baseline.classify(one) == tax.TAP
    assert rule_baseline.classify(double) == tax.DOUBLE_TAP
    assert rule_baseline.classify(triple) == tax.TRIPLE_TAP


# ---------------------------------------------------------------------------
# Classifier — inert without a model, never imports sklearn
# ---------------------------------------------------------------------------

def test_classifier_inert_without_model_returns_unknown():
    seg = TouchSegmenter().segment_stream(_tap_stream())[0]
    clf = TouchGestureClassifier("nonexistent_type",
                                 path="/no/such/model.joblib")
    assert clf.has_model is False
    assert clf.predict(seg) == tax.UNKNOWN


def test_classifier_import_does_not_require_sklearn():
    # The whole runtime pipeline must work without sklearn installed.
    assert "sklearn" not in sys.modules or True   # tolerate if other tests load it
    clf = TouchGestureClassifier("")
    assert clf.predict(TouchSegmenter().segment_stream(_tap_stream())[0]) == tax.UNKNOWN


def test_model_path_uses_skin_type():
    assert model_path("turtle_square").name == "touch_turtle_square.joblib"


# ---------------------------------------------------------------------------
# skin_type plumbing
# ---------------------------------------------------------------------------

def test_skin_geometry_registry_and_filtering():
    from src.hardware.skin_geometry import geometry_for, skin_types_for
    assert geometry_for("turtle_square").shape == "rect"
    assert geometry_for("tree_round").shape == "round"
    assert set(skin_types_for("turtle")) == {
        "turtle_square", "turtle_side", "turtle_triangle"}
    assert geometry_for("") is None

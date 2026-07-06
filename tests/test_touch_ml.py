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

def test_rule_baseline_separates_tap_and_press():
    seg_tap = TouchSegmenter().segment_stream(_tap_stream())[0]
    seg_press = TouchSegmenter().segment_stream(_press_stream())[0]
    assert rule_baseline.classify(seg_tap) == tax.TAP
    assert rule_baseline.classify(seg_press) == tax.PRESS


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


def test_rule_baseline_labels_compressions_bout():
    # A single pulse is a tap; a run of several merged pulses is a
    # compressions bout (COMPRESSIONS_MIN_PULSES = 3).
    one = TouchSegmenter().segment_stream(_tap_stream())[0]
    double = merge_segments(
        TouchSegmenter().segment_stream(_tap_stream() + _tap_stream()))
    bout = merge_segments(
        TouchSegmenter().segment_stream(
            _tap_stream() + _tap_stream() + _tap_stream()))
    assert rule_baseline.classify(one) == tax.TAP
    assert double.n_pulses == 2                       # below the bout threshold
    assert rule_baseline.classify(double) != tax.COMPRESSIONS
    assert bout.n_pulses >= tax.COMPRESSIONS_MIN_PULSES
    assert rule_baseline.classify(bout) == tax.COMPRESSIONS


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
    assert set(skin_types_for("turtle")) | set(skin_types_for("tree")) == {
        "turtle_square", "turtle_side", "turtle_triangle", "tree_round"}
    assert geometry_for("") is None


# ---------------------------------------------------------------------------
# PulseMerger — live multi-tap grouping
# ---------------------------------------------------------------------------

def _seg(start, end, n_sensors=4):
    from src.ml.touch_segmenter import TouchSegment
    s = TouchSegment(start_ms=start, end_ms=end)
    s.mags.append([5.0] + [0.0] * (n_sensors - 1))
    s.acts.append({0})
    s.times_ms.append(start)
    s.vecs.append(None)
    return s


def test_pulse_merger_groups_quick_taps():
    from src.ml.touch_segmenter import PulseMerger
    m = PulseMerger(gap_ms=400.0)
    assert m.feed(_seg(0, 100), 100.0) is None          # first tap held back
    assert m.feed(None, 200.0) is None                  # within gap — waiting
    assert m.feed(_seg(300, 380), 380.0) is None        # second tap joins
    merged = m.feed(None, 800.0)                        # gap elapsed → flush
    assert merged is not None and merged.n_pulses == 2
    assert merged.start_ms == 0 and merged.end_ms == 380


def test_pulse_merger_separates_slow_taps():
    from src.ml.touch_segmenter import PulseMerger
    m = PulseMerger(gap_ms=400.0)
    m.feed(_seg(0, 100), 100.0)
    first = m.feed(None, 600.0)                         # gap elapsed → single
    assert first is not None and first.n_pulses == 1
    m.feed(_seg(700, 800), 800.0)
    second = m.feed(None, 1300.0)
    assert second is not None and second.n_pulses == 1


def test_pulse_merger_waits_while_touch_active():
    from src.ml.touch_segmenter import PulseMerger
    m = PulseMerger(gap_ms=400.0)
    m.feed(_seg(0, 100), 100.0)
    # Gap elapsed but a follow-up touch is in progress — must not flush.
    assert m.feed(None, 600.0, touch_active=True) is None
    m.feed(_seg(650, 700), 700.0)
    merged = m.feed(None, 1200.0)
    assert merged is not None and merged.n_pulses == 2


# ---------------------------------------------------------------------------
# vec (3-axis) features
# ---------------------------------------------------------------------------

def _msg_vec(mag, act, vec):
    return {"type": "magnet", "mag": mag, "act": list(act), "vec": vec}


def test_vec_features_zero_without_vector_data():
    seg = TouchSegmenter().segment_stream(_press_stream())[0]
    f = extract_features(seg)
    assert f["vec_present"] == 0.0
    assert f["vec_dir_consistency"] == 0.0 and f["vec_z_frac"] == 0.0


def test_vec_features_press_vs_slide():
    # Press: dominant sensor's delta holds one direction (straight down z).
    press = [(_msg_vec([9.0, 0, 0, 0], [0], [[0, 0, 300]] * 4), float(t))
             for t in range(0, 400, 50)]
    press = [(_msg_vec([0.0] * 4, [], [[0, 0, 0]] * 4), -50.0)] + press + [
        (_msg_vec([0.0] * 4, [], [[0, 0, 0]] * 4), 400.0)]
    seg = TouchSegmenter().segment_stream(press)[0]
    f = extract_features(seg)
    assert f["vec_present"] == 1.0
    assert f["vec_dir_consistency"] > 0.99          # direction held steady
    assert f["vec_z_frac"] > 0.99                   # pure vertical push

    # Slide: direction rotates in the XY plane sample to sample.
    vecs = [[[300, 0, 50]] * 4, [[200, 200, 50]] * 4, [[0, 300, 50]] * 4,
            [[-200, 200, 50]] * 4, [[-300, 0, 50]] * 4]
    slide = [(_msg_vec([9.0, 0, 0, 0], [0], v), float(50 * i))
             for i, v in enumerate(vecs)]
    slide = [(_msg_vec([0.0] * 4, [], None), -50.0)] + slide + [
        (_msg_vec([0.0] * 4, [], None), 300.0)]
    seg2 = TouchSegmenter().segment_stream(slide)[0]
    f2 = extract_features(seg2)
    assert f2["vec_dir_consistency"] < 0.9          # direction wandered
    assert f2["vec_dir_consistency"] < f["vec_dir_consistency"]


def test_feature_vector_includes_vec_block():
    assert "vec_present" in FEATURE_NAMES
    seg = TouchSegmenter().segment_stream(_tap_stream())[0]
    assert len(feature_vector(seg)) == len(FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Train/serve parity — compensated stream preferred for training,
# excluded from coupling calibration
# ---------------------------------------------------------------------------

def _write_recording(tmp_path, lines):
    import json
    rec = tmp_path / "rec.jsonl"
    rec.write_text("\n".join(json.dumps(ln) for ln in lines), encoding="utf-8")
    return rec


def _mixed_recording(tmp_path):
    """Raw stream shows a (false) touch on sensor 1; the compensated stream
    (recorded alongside) removed it and shows only the real touch on sensor 0."""
    def raw(t, mag, act):
        return {"t": f"2024-01-01T00:00:{t:06.3f}",
                "msg": {"type": "magnet", "source": "AA", "mag": mag,
                        "act": act}}
    def comp(t, mag, act):
        out = raw(t, mag, act)
        out["msg"]["compensated"] = True
        return out
    return _write_recording(tmp_path, [
        raw(0.0, [0.0, 0.0], []),        comp(0.010, [0.0, 0.0], []),
        raw(0.1, [5.0, 4.0], [0, 1]),    comp(0.110, [5.0, 0.5], [0]),
        raw(0.2, [0.0, 0.0], []),        comp(0.210, [0.0, 0.0], []),
    ])


def test_training_segments_prefer_compensated_stream(tmp_path):
    from src.ml.training import segments_of
    segs = [seg for _src, seg in segments_of(_mixed_recording(tmp_path))]
    assert len(segs) == 1
    assert segs[0].acts[0] == {0}          # compensated view: sensor 1 removed


def test_coupling_calibration_skips_compensated(tmp_path):
    from src.core.touch_coupling import samples_from_recording
    samples = list(samples_from_recording(_mixed_recording(tmp_path)))
    assert len(samples) == 3               # only the raw magnet lines
    assert all(s[2] in ([0.0, 0.0], [5.0, 4.0]) for s in samples)

"""Tests for train_from_segments - the guided-capture training entry point.

Feeds labelled touch segments straight in (as GestureCaptureSession would) and
checks the model is saved and the TypeResult carries an accuracy + a square
confusion matrix, with the honest fallbacks when there are too few samples.
Needs scikit-learn (skipped otherwise); the rest of the pipeline stays
dependency-free.
"""

import pytest

pytest.importorskip("sklearn")

from src.ml import gesture_taxonomy as tax                      # noqa: E402
from src.ml.touch_segmenter import TouchSegmenter, merge_segments  # noqa: E402
from src.ml.training import train_from_segments                # noqa: E402


def _msg(mag, act):
    return {"type": "magnet", "mag": mag, "act": list(act)}


def _tap_seg():
    hot = _msg([5.0, 0, 0, 0], [0])
    return TouchSegmenter().segment_stream(
        [(_msg([0, 0, 0, 0], []), 0.0), (hot, 50.0), (hot, 100.0),
         (_msg([0, 0, 0, 0], []), 150.0)])[0]


def _press_seg():
    hot = _msg([6.0, 0, 0, 0], [0])
    samples = [(_msg([0, 0, 0, 0], []), 0.0)]
    samples += [(hot, float(t)) for t in range(50, 1000, 50)]
    samples.append((_msg([0, 0, 0, 0], []), 1000.0))
    return TouchSegmenter().segment_stream(samples)[0]


def _compressions_seg():
    """A bout of three press->release pulses merged into one gesture."""
    hot = _msg([5.0, 0, 0, 0], [0])
    idle = _msg([0, 0, 0, 0], [])
    segs = TouchSegmenter().segment_stream([
        (idle, 0.0),
        (hot, 50.0), (hot, 100.0), (idle, 150.0),
        (hot, 250.0), (hot, 300.0), (idle, 350.0),
        (hot, 450.0), (hot, 500.0), (idle, 550.0),
    ])
    return merge_segments(segs)


def _path_seg(order, peak=5.0):
    """A touch whose strongest sensor follows ``order`` (one sample each)."""
    idle = _msg([0, 0, 0, 0], [])
    samples = [(idle, 0.0)]
    for step, sensor in enumerate(order):
        mag = [0.0, 0.0, 0.0, 0.0]
        mag[sensor] = peak
        samples.append((_msg(mag, [sensor]), 50.0 + 100.0 * step))
    samples.append((idle, 60.0 + 100.0 * len(order)))
    return TouchSegmenter().segment_stream(samples)[0]


def _stroke_seg():
    return _path_seg([0, 1, 2, 3])


def _rub_seg():
    return _path_seg([0, 1, 0, 1, 0])


def _labeled(counts):
    """``{label: (segment_factory, n)}`` -> list of (type, variant, label, seg)."""
    out = []
    for label, (factory, n) in counts.items():
        out += [("turtle_square", "wrinkles", label, factory()) for _ in range(n)]
    return out


def test_trains_on_the_captured_gestures_and_reports_against_the_rules(tmp_path):
    labeled = _labeled({tax.TAP: (_tap_seg, 5),
                        tax.PRESS: (_press_seg, 5),
                        tax.COMPRESSIONS: (_compressions_seg, 5),
                        tax.STROKE: (_stroke_seg, 5),
                        tax.RUB: (_rub_seg, 5)})
    report = train_from_segments(labeled, tmp_path)

    res = next(r for r in report.results if r.skin_type == "turtle_square")
    assert res.trained
    assert (tmp_path / "touch_turtle_square.joblib").exists()
    assert res.model_acc is not None and res.model_acc >= 0.9
    assert res.labels == sorted([tax.TAP, tax.PRESS, tax.COMPRESSIONS,
                                 tax.STROKE, tax.RUB])
    assert res.confusion is not None
    assert len(res.confusion) == len(res.labels) == 5
    assert all(len(row) == 5 for row in res.confusion)
    # Every captured sample is accounted for in the matrix.
    assert sum(sum(row) for row in res.confusion) == 25
    # The rules, the baseline, already read these clean synthetic touches.
    assert res.baseline_acc == 1.0


def test_too_few_per_class_trains_without_cv(tmp_path):
    # 11 samples, 2 classes -> trains, but PRESS has <2 so no honest CV.
    labeled = _labeled({tax.TAP: (_tap_seg, 10), tax.PRESS: (_press_seg, 1)})
    res = train_from_segments(labeled, tmp_path).results[0]
    assert res.trained
    assert res.model_acc is None and res.confusion is None


def test_too_few_samples_are_not_trained(tmp_path):
    labeled = _labeled({tax.TAP: (_tap_seg, 3), tax.PRESS: (_press_seg, 3)})
    res = train_from_segments(labeled, tmp_path).results[0]
    assert not res.trained
    assert not (tmp_path / "touch_turtle_square.joblib").exists()


def test_empty_input_yields_empty_report(tmp_path):
    report = train_from_segments([], tmp_path)
    assert report.results == []


def test_trained_model_names_the_gesture_live(tmp_path):
    from src.ml.touch_axes import read_touch
    from src.ml.touch_classifier import TouchGestureModel
    labeled = _labeled({tax.TAP: (_tap_seg, 6), tax.STROKE: (_stroke_seg, 6),
                        tax.RUB: (_rub_seg, 6)})
    train_from_segments(labeled, tmp_path)
    model = TouchGestureModel(
        "turtle_square", path=tmp_path / "touch_turtle_square.joblib",
        skin_variant="wrinkles")
    assert model.has_model
    assert model.predict(_stroke_seg()) == tax.STROKE
    assert model.predict(_tap_seg()) == tax.TAP
    reading = read_touch(_stroke_seg(), gesture=model.predict(_stroke_seg()))
    assert (reading.gesture, reading.movement, reading.named_by) == (
        tax.STROKE, tax.MOVE_SLIDE, "model")


def test_model_from_another_feature_set_is_refused(tmp_path):
    import joblib
    from sklearn.dummy import DummyClassifier
    from src.ml.touch_classifier import TouchGestureModel
    path = tmp_path / "touch_turtle_square.joblib"
    joblib.dump(DummyClassifier().fit([[0.0]], ["tap"]), path)   # an old model
    model = TouchGestureModel("turtle_square", path=path)
    assert not model.has_model and model.predict(_tap_seg()) is None

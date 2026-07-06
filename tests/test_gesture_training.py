"""Tests for train_from_segments — the guided-capture training entry point.

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
    """A bout of three press→release pulses merged into one gesture."""
    hot = _msg([5.0, 0, 0, 0], [0])
    idle = _msg([0, 0, 0, 0], [])
    segs = TouchSegmenter().segment_stream([
        (idle, 0.0),
        (hot, 50.0), (hot, 100.0), (idle, 150.0),
        (hot, 250.0), (hot, 300.0), (idle, 350.0),
        (hot, 450.0), (hot, 500.0), (idle, 550.0),
    ])
    return merge_segments(segs)


def _labeled(counts):
    """``{label: (segment_factory, n)}`` → list of (type, variant, label, seg)."""
    out = []
    for label, (factory, n) in counts.items():
        out += [("turtle_square", "wrinkles", label, factory()) for _ in range(n)]
    return out


def test_trains_and_reports_accuracy_and_confusion(tmp_path):
    labeled = _labeled({tax.TAP: (_tap_seg, 6),
                        tax.PRESS: (_press_seg, 6),
                        tax.COMPRESSIONS: (_compressions_seg, 6)})
    report = train_from_segments(labeled, tmp_path)

    res = next(r for r in report.results if r.skin_type == "turtle_square")
    assert res.trained
    assert (tmp_path / "touch_turtle_square.joblib").exists()
    assert res.model_acc is not None and 0.0 <= res.model_acc <= 1.0
    # Square confusion matrix over the sorted label set.
    assert res.labels == sorted({tax.TAP, tax.PRESS, tax.COMPRESSIONS})
    assert len(res.confusion) == len(res.labels) == 3
    assert all(len(row) == 3 for row in res.confusion)
    # Every captured sample is accounted for in the matrix.
    assert sum(sum(row) for row in res.confusion) == 18


def test_too_few_per_class_trains_without_cv(tmp_path):
    # 11 samples, 2 classes → trains, but PRESS has <2 so no honest CV.
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

"""Tests for the touch-position feasibility bench (Phase 0).

Synthetic magnet streams drive the energy-based press detector and the
capture session; the analysis test builds per-cell separable signatures and
checks the report reflects that. Analysis tests skip without sklearn.
"""

import math
import random

import pytest

from src.ml.position_bench import (
    BenchPress,
    CaptureSession,
    PaceTracker,
    PressDetector,
    analyze,
    load_dataset,
    save_jsonl,
)


def _msg(mags, vecs=None):
    msg = {"type": "magnet", "mag": list(mags), "act": []}
    if vecs is not None:
        msg["vec"] = [list(v) for v in vecs]
    return msg


IDLE = _msg([1.0, 0.5, 0.8, 0.3])


def _feed_press(detector, mags, vecs=None, t0=0.0, hot_samples=8):
    """Feed idle -> press -> idle; return whatever the detector emitted."""
    out = []
    t = t0
    for _ in range(3):
        out.append(detector.feed(IDLE, t))
        t += 35.0
    for _ in range(hot_samples):
        out.append(detector.feed(_msg(mags, vecs), t))
        t += 35.0
    for _ in range(5):
        out.append(detector.feed(IDLE, t))
        t += 35.0
    return [p for p in out if p is not None], t


class TestPressDetector:
    def test_detects_press_above_threshold(self):
        det = PressDetector(enter_ut=60, exit_ut=35)
        presses, _ = _feed_press(det, [80.0, 10.0, 5.0, 5.0])
        assert len(presses) == 1
        assert presses[0].duration_ms > 0

    def test_weak_distributed_press_still_detected(self):
        # Sum crosses the threshold even though NO single sensor would pass
        # the firmware's default 300 uT act threshold - the whole point.
        det = PressDetector(enter_ut=60, exit_ut=35)
        presses, _ = _feed_press(det, [20.0, 20.0, 20.0, 20.0])
        assert len(presses) == 1

    def test_noise_blip_rejected(self):
        det = PressDetector(enter_ut=60, exit_ut=35, min_duration_ms=120)
        presses, _ = _feed_press(det, [500.0, 0.0, 0.0, 0.0], hot_samples=1)
        assert presses == []

    def test_two_presses_two_segments(self):
        det = PressDetector(enter_ut=60, exit_ut=35)
        first, t = _feed_press(det, [100.0, 0.0, 0.0, 0.0])
        second, _ = _feed_press(det, [0.0, 100.0, 0.0, 0.0], t0=t)
        assert len(first) == 1 and len(second) == 1


class TestSignature:
    def test_vec_signature_uses_peak_window(self):
        press = BenchPress(label="0,0", cell=(0, 0), rep=0)
        # Ramp: weak sample outside the 70% peak window must be excluded.
        for mag, vec in [(10.0, [1.0, 0.0, 0.0]),
                         (100.0, [10.0, 0.0, 0.0]),
                         (100.0, [10.0, 0.0, 0.0])]:
            press.times_ms.append(len(press.times_ms) * 35.0)
            press.mags.append([mag, 0.0, 0.0, 0.0])
            press.vecs.append([vec, [0, 0, 0], [0, 0, 0], [0, 0, 0]])
        sig = press.signature(use_vec=True)
        assert len(sig) == 12
        assert sig[0] == pytest.approx(10.0)

    def test_mag_fallback_when_no_vec(self):
        press = BenchPress(label="p", cell=None, rep=0)
        press.times_ms = [0.0, 35.0]
        press.mags = [[50.0, 10.0], [50.0, 10.0]]
        press.vecs = [None, None]
        assert not press.has_vec
        assert press.signature(use_vec=True) == pytest.approx([50.0, 10.0])


class TestCaptureSession:
    def test_labels_and_redo(self):
        session = CaptureSession(PressDetector(enter_ut=60, exit_ut=35))
        session.set_target("1,2", (1, 2))
        t = 0.0
        for _ in range(2):
            for _ in range(3):
                session.feed_message(IDLE, t); t += 35.0
            for _ in range(8):
                session.feed_message(_msg([90.0, 0, 0, 0]), t); t += 35.0
            for _ in range(5):
                session.feed_message(IDLE, t); t += 35.0
        assert session.target_count("1,2") == 2
        assert all(p.cell == (1, 2) for p in session.presses)
        assert session.drop_target("1,2") == 2
        assert session.presses == []


class TestPaceTracker:
    def test_median_pace_eta(self):
        pace = PaceTracker()
        for t in [0.0, 4.0, 8.0, 12.0]:
            pace.mark(t)
        assert pace.seconds_per_rep() == pytest.approx(4.0)
        assert pace.eta_s(10) == pytest.approx(40.0)
        assert PaceTracker.fmt(pace.eta_s(10)) == "0:40"

    def test_no_eta_before_two_marks(self):
        pace = PaceTracker()
        assert pace.eta_s(5) is None
        assert PaceTracker.fmt(None) == "--:--"


class TestRoundTrip:
    def test_save_load(self, tmp_path):
        press = BenchPress(label="0,1", cell=(0, 1), rep=0,
                           times_ms=[0.0, 35.0],
                           mags=[[10.0, 5.0], [12.0, 6.0]],
                           vecs=[[[1, 2, 3], [0, 0, 0]], None])
        path = tmp_path / "bench.jsonl"
        save_jsonl(path, {"rows": 2, "cols": 2}, [press])
        meta, presses = load_dataset([path])
        assert meta["rows"] == 2
        assert presses[0].cell == (0, 1)
        assert presses[0].vecs[1] is None
        assert presses[0].mags[1] == [12.0, 6.0]


def _synthetic_grid_presses(rows=3, cols=3, reps=8, seed=7):
    """Per-cell distinct 12-D directions + noise -> a separable dataset."""
    rng = random.Random(seed)
    cell_dirs = {}
    for r in range(rows):
        for c in range(cols):
            v = [rng.gauss(0, 1) for _ in range(12)]
            n = math.sqrt(sum(x * x for x in v))
            cell_dirs[(r, c)] = [x / n for x in v]
    presses = []
    for (r, c), direction in cell_dirs.items():
        for rep in range(reps):
            scale = rng.uniform(80, 300)      # press force varies per rep
            noisy = [scale * d + rng.gauss(0, 3.0) for d in direction]
            vec_row = [noisy[3 * s:3 * s + 3] for s in range(4)]
            mags = [math.sqrt(sum(x * x for x in vec_row[s])) for s in range(4)]
            press = BenchPress(label=f"{r},{c}", cell=(r, c), rep=rep)
            for _ in range(4):
                press.times_ms.append(len(press.times_ms) * 35.0)
                press.mags.append(mags)
                press.vecs.append(vec_row)
            presses.append(press)
    return presses


class TestAnalysis:
    def test_separable_grid_scores_high(self):
        pytest.importorskip("sklearn")
        presses = _synthetic_grid_presses()
        report = analyze({"rows": 3, "cols": 3, "cell_size_mm": 25}, presses)
        assert "Grid cells (3x3" in report
        assert "vec 3-axis" in report
        assert "Per-cell recall" in report
        # Force-invariant normalized features must make this easy: the RF
        # vec accuracy line should read >= 90 %.
        rf_lines = [ln for ln in report.splitlines()
                    if "RandomForest" in ln and "vec 3-axis" in ln]
        assert rf_lines, report
        acc = float(rf_lines[0].split(":")[1].replace("%", "").strip())
        assert acc >= 90.0, report

    def test_pattern_section(self):
        pytest.importorskip("sklearn")
        presses = _synthetic_grid_presses(rows=1, cols=2, reps=8)
        for p in presses:            # re-label the two cells as patterns
            p.label = "squeeze" if p.cell == (0, 0) else "both_hands"
            p.cell = None
        report = analyze({"rows": 0, "cols": 0}, presses)
        assert "Patterns (2 classes)" in report
        assert "squeeze" in report and "both_hands" in report


class TestTemporalFeatures:
    def test_six_features_sane(self):
        press = BenchPress(label="0,0", cell=(0, 0), rep=0)
        for i, (mag, vx) in enumerate([(20.0, 2.0), (100.0, 10.0),
                                       (100.0, 10.0), (40.0, 4.0)]):
            press.times_ms.append(i * 35.0)
            press.mags.append([mag, 0.0, 0.0, 0.0])
            press.vecs.append([[vx, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]])
        feats = press.temporal_features()
        assert len(feats) == 6
        rise_s, fall_s, dur_s, stability, path, slope = feats
        assert rise_s == pytest.approx(0.035)
        assert dur_s == pytest.approx(0.105)
        # Direction never changes in this synthetic press.
        assert stability == pytest.approx(1.0)
        assert path == pytest.approx(0.0)
        assert slope > 0

    def test_use_vec_false_ignores_vector_rows(self):
        """The mag-only feature set is the control for the vec one, so its
        transients must not be derived from the 3-axis rows."""
        press = BenchPress(label="0,0", cell=(0, 0), rep=0)
        # Magnitudes hold still while the vec direction swings hard: only a
        # vec-derived stability can drop below 1.
        for i, vec_row in enumerate([
            [[10.0, 0.0, 0.0], [0.0] * 3, [0.0] * 3, [0.0] * 3],
            [[0.0, 10.0, 0.0], [0.0] * 3, [0.0] * 3, [0.0] * 3],
            [[0.0, 0.0, 10.0], [0.0] * 3, [0.0] * 3, [0.0] * 3],
        ]):
            press.times_ms.append(i * 35.0)
            press.mags.append([10.0, 0.0, 0.0, 0.0])
            press.vecs.append(vec_row)
        _, _, _, vec_stability, vec_path, _ = press.temporal_features(use_vec=True)
        _, _, _, mag_stability, mag_path, _ = press.temporal_features(use_vec=False)
        assert vec_stability < 0.5 and vec_path > 0.5      # direction swung
        assert mag_stability == pytest.approx(1.0)         # magnitudes did not
        assert mag_path == pytest.approx(0.0)


class TestMixedVecDatasets:
    def test_presses_without_vec_do_not_make_a_ragged_matrix(self):
        """A pre-reflash capture analyzed together with a post-reflash one used
        to mix 12-D and 4-D rows, and numpy refused the whole report."""
        pytest.importorskip("sklearn")
        presses = _synthetic_grid_presses(rows=2, cols=2, reps=8)
        for p in presses[::4]:               # a quarter predate stream_vec
            p.vecs = [None] * len(p.vecs)
        report = analyze({"rows": 2, "cols": 2}, presses)
        assert "Grid cells (2x2" in report
        assert "presses skipped" in report   # the loss is stated, not silent

    def test_empty_press_is_zeroes(self):
        assert BenchPress(label="x", cell=None, rep=0).temporal_features() \
            == [0.0] * 6

    def test_report_includes_transient_sets(self):
        pytest.importorskip("sklearn")
        presses = _synthetic_grid_presses()
        report = analyze({"rows": 3, "cols": 3}, presses)
        assert "vec+transient" in report
        presses_p = _synthetic_grid_presses(rows=1, cols=2, reps=8)
        for p in presses_p:
            p.label = "squeeze" if p.cell == (0, 0) else "press_flat"
            p.cell = None
        report_p = analyze({"rows": 0, "cols": 0}, presses_p)
        assert "+transient" in report_p

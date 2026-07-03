"""Unit tests for src.core.touch_coupling (pure coupling-matrix analysis)."""

from __future__ import annotations

import json

from src.core import touch_coupling as tc


# --- classify_active ------------------------------------------------------

def test_classify_rest():
    assert tc.classify_active({0: 2.0, 1: 1.0}) == "rest"


def test_classify_single_chamber():
    assert tc.classify_active({0: 80.0, 1: 3.0}) == 0


def test_classify_ambiguous_two_high():
    assert tc.classify_active({0: 80.0, 1: 70.0}) is None


def test_classify_transition_one_mid():
    # chamber 0 up but chamber 1 mid-range (not at rest) → ambiguous/skip
    assert tc.classify_active({0: 80.0, 1: 20.0}) is None


# --- build_coupling -------------------------------------------------------

def _sweep_samples():
    """Rest, then chamber 0 inflated (moves sensor 0), with a lagged transition.

    During the settle window after inflation starts, the mag is already spiking
    (pressure-sensor lag) — those samples must be excluded from the means.
    """
    samples = []
    # rest 0..2000 ms: baseline mag = 1.0 everywhere
    for t in range(0, 2000, 100):
        samples.append((float(t), {0: 0.0, 1: 0.0}, [1.0, 1.0, 1.0, 1.0]))
    # chamber 0 active 2000..5000 ms: sensor 0 reads 5.0 (delta 4.0)
    for t in range(2000, 5000, 100):
        # within the 800 ms settle window the reading is wild (would skew means)
        mag0 = 99.0 if t < 2800 else 5.0
        samples.append((float(t), {0: 80.0, 1: 0.0}, [mag0, 1.0, 1.0, 1.0]))
    return samples


def test_build_coupling_steady_state_delta():
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=800.0)
    assert m.chambers == [0]
    row = m.deltas[0]
    assert abs(row[0] - 4.0) < 1e-6      # sensor 0 moved by ~4 (the 99.0 spikes dropped)
    for s in (1, 2, 3):
        assert abs(row[s]) < 1e-6
    assert abs(m.baseline[0] - 1.0) < 1e-6


def test_settle_window_excludes_transition_spikes():
    # With no settle guard the 99.0 spikes leak in and inflate the delta.
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=0.0)
    assert m.deltas[0][0] > 4.0


def test_mapping_and_primary_chamber():
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=800.0)
    assert m.mapping(threshold=2.0) == {0: [0]}
    primary = m.sensor_primary_chamber(threshold=2.0)
    assert primary[0] == 0
    assert primary[1] is None and primary[2] is None and primary[3] is None


def test_empty_samples():
    m = tc.build_coupling([], sensor_count=4)
    assert m.chambers == [] and m.baseline == [0.0, 0.0, 0.0, 0.0]


# --- recording parsing ----------------------------------------------------

def test_samples_from_recording(tmp_path):
    rec = tmp_path / "sweep.jsonl"
    lines = [
        {"t": "2024-01-01T00:00:00.000", "msg": {"type": "status", "chamber": 0, "pressure": 0.0}},
        {"t": "2024-01-01T00:00:00.100", "msg": {"type": "magnet", "mag": [1.0, 1.0, 1.0, 1.0]}},
        {"t": "2024-01-01T00:00:01.000", "msg": {"type": "status", "chamber": 0, "pressure": 80.0}},
        {"t": "2024-01-01T00:00:01.100", "msg": {"type": "magnet", "mag": [5.0, 1.0, 1.0, 1.0]}},
        {"t": "2024-01-01T00:00:01.200", "msg": {"type": "boot", "status": "ready"}},  # ignored
    ]
    rec.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")

    samples = list(tc.samples_from_recording(rec))
    assert len(samples) == 2                      # only the two magnet messages
    assert samples[0][1] == {0: 0.0}              # pressures before inflation
    assert samples[1][1] == {0: 80.0}             # last-known pressure carried forward
    assert samples[1][2] == [5.0, 1.0, 1.0, 1.0]

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
    # chamber 0 up AND chamber 1 also inflated (>= active_min) → ambiguous/skip
    assert tc.classify_active({0: 80.0, 1: 20.0}) is None


def test_classify_offrest_neighbor_does_not_block():
    # A neighbour off-rest but below active_min (a leaky/narrow-range residual)
    # must not drop the clearly-inflated chamber — it just gets attributed.
    assert tc.classify_active({0: 80.0, 1: 15.0}) == 0


def test_classify_leaky_narrow_range_neighbor():
    # The real bug: slot 1 vents to ~2 kPa which is 12 % of its 20 kPa range,
    # while slot 2 is inflated. Slot 2 must still classify (not be dropped).
    assert tc.classify_active({0: 3.0, 1: 12.0, 2: 22.0}) == 2


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


def _chamber_states(m, chamber):
    """Single-chamber measured states for ``chamber``, ascending level."""
    key = frozenset({chamber})
    return sorted((st for st in m.states if st.chambers == key),
                  key=lambda st: st.levels[chamber])


def test_build_coupling_steady_state_delta():
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=800.0)
    assert m.chambers == [0]
    row = m.deltas()[0]
    assert abs(row[0] - 4.0) < 1e-6      # sensor 0 moved by ~4 (the 99.0 spikes dropped)
    for s in (1, 2, 3):
        assert abs(row[s]) < 1e-6
    assert abs(m.baseline[0] - 1.0) < 1e-6


def test_settle_window_excludes_transition_spikes():
    # With no settle guard the 99.0 spikes leak in and inflate the delta.
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=0.0)
    assert m.deltas()[0][0] > 4.0


def test_mapping_and_primary_chamber():
    m = tc.build_coupling(_sweep_samples(), sensor_count=4, settle_ms=800.0)
    assert m.mapping(threshold=2.0) == {0: [0]}
    primary = m.sensor_primary_chamber(threshold=2.0)
    assert primary[0] == 0
    assert primary[1] is None and primary[2] is None and primary[3] is None


def test_empty_samples():
    m = tc.build_coupling([], sensor_count=4)
    assert m.chambers == [] and m.baseline == [0.0, 0.0, 0.0, 0.0]


def test_leaky_neighbor_does_not_drop_later_chamber():
    """A chamber inflated while a prior neighbour sits at a below-active residual
    (the narrow-range leak) must still land in the matrix."""
    samples = []
    t = 0.0
    for _ in range(20):                       # baseline: all rest
        samples.append((t, {0: 0.0, 1: 0.0}, [1.0, 1.0])); t += 100.0
    for _ in range(20):                       # chamber 0 inflated, vents clean
        samples.append((t, {0: 80.0, 1: 0.0}, [9.0, 1.0])); t += 100.0
    for _ in range(20):                       # chamber 1 inflated; 0 leaks at 12 %
        samples.append((t, {0: 12.0, 1: 80.0}, [1.0, 7.0])); t += 100.0
    m = tc.build_coupling(samples, sensor_count=2, settle_ms=800.0)
    assert m.chambers == [0, 1]               # chamber 1 survived the leak
    assert abs(m.deltas()[1][1] - 6.0) < 1e-6   # 7 - baseline 1


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


# --- multi-level states + 3-axis deltas -------------------------------------

def _staircase_samples(with_vec=False):
    """Rest, then chamber 0 held at 50 % and at 100 % (a staircase sweep)."""
    samples = []
    t = 0.0
    def emit(pressures, mag, vec):
        nonlocal t
        samples.append((t, pressures, mag, vec) if with_vec or vec is None
                       else (t, pressures, mag))
        t += 100.0
    for _ in range(20):
        emit({0: 0.0}, [1.0], [[1.0, 1.0, 1.0]] if with_vec else None)
    for _ in range(20):
        emit({0: 50.0}, [101.0], [[101.0, 1.0, 1.0]] if with_vec else None)
    for _ in range(20):
        emit({0: 100.0}, [201.0], [[201.0, 1.0, 1.0]] if with_vec else None)
    return samples


def test_staircase_builds_multi_level_states():
    m = tc.build_coupling(_staircase_samples(), sensor_count=1)
    points = _chamber_states(m, 0)
    assert [round(p.levels[0]) for p in points] == [50, 100]
    assert abs(points[0].mag[0] - 100.0) < 1e-6   # 101 - baseline 1
    assert abs(points[1].mag[0] - 200.0) < 1e-6
    assert m.deltas()[0] == points[-1].mag         # per-chamber view = top level
    assert not m.has_vec


def test_level_step_restarts_settle_guard():
    # Samples right after the 50→100 step must not leak into either bin: the
    # 100 % plateau only starts counting settle_ms after the level change.
    samples = _staircase_samples()
    # Corrupt the first 800 ms of the 100 % plateau; means must be unaffected.
    for i in range(40, 48):
        samples[i] = (samples[i][0], samples[i][1], [999.0])
    m = tc.build_coupling(samples, sensor_count=1, settle_ms=800.0)
    assert abs(_chamber_states(m, 0)[1].mag[0] - 200.0) < 1e-6


def test_vec_deltas_baseline_subtracted():
    m = tc.build_coupling(_staircase_samples(with_vec=True), sensor_count=1)
    assert m.has_vec
    assert m.baseline_vec == [[1.0, 1.0, 1.0]]
    top = _chamber_states(m, 0)[-1]
    assert top.vec == [[200.0, 0.0, 0.0]]
    cfg_states = m.states_for_config()
    assert cfg_states[-1]["vec"] == [[200.0, 0.0, 0.0]]


def test_build_captures_combination_state():
    """Two chambers co-inflated produce a combination state, not two singles."""
    samples = []
    t = 0.0
    for _ in range(20):                       # rest
        samples.append((t, {0: 0.0, 1: 0.0}, [1.0, 1.0])); t += 100.0
    for _ in range(20):                       # both chambers up together
        samples.append((t, {0: 80.0, 1: 80.0}, [9.0, 5.0])); t += 100.0
    m = tc.build_coupling(samples, sensor_count=2, settle_ms=800.0)
    combos = m.combos()
    assert len(combos) == 1
    assert combos[0].chambers == frozenset({0, 1})
    assert abs(combos[0].mag[0] - 8.0) < 1e-6   # 9 - baseline 1
    assert m.max_order == 2


def test_mixed_vec_availability_yields_no_vec():
    samples = _staircase_samples(with_vec=True)
    t, p, mag, _vec = samples[30]
    samples[30] = (t, p, mag, None)    # one sample without vec in the 50 % bin
    m = tc.build_coupling(samples, sensor_count=1)
    assert _chamber_states(m, 0)[0].vec is None
    assert not m.has_vec


def test_samples_from_recording_carries_vec(tmp_path):
    rec = tmp_path / "sweep.jsonl"
    lines = [
        {"t": "2024-01-01T00:00:00.000",
         "msg": {"type": "magnet", "mag": [1.0], "vec": [[1.0, 0.0, 0.0]]}},
        {"t": "2024-01-01T00:00:00.100", "msg": {"type": "magnet", "mag": [2.0]}},
    ]
    rec.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    samples = list(tc.samples_from_recording(rec))
    assert samples[0][3] == [[1.0, 0.0, 0.0]]
    assert samples[1][3] is None

"""Tests for the running-score group sync tracker (`score_fill`)."""

import pytest

from src.activities.touch_rhythm import MODE_AUTO, MODE_FIXED, SyncScoreTracker

RULES = dict(participants=3, sensor_ids=None, target_interval_ms=100,
             cadence_tolerance_ms=10, phase_tolerance_ms=40, min_gap_ms=30,
             gain=0.25, penalty=0.25)


def _tracker(mode=MODE_AUTO, **overrides):
    t = SyncScoreTracker()
    t.configure(**{**RULES, **overrides}, mode=mode)
    return t


def _round(t, at_ms, sensors=(0, 1, 2)):
    """One group beat: the sensors press 10 ms apart starting at ``at_ms``."""
    for i, s in enumerate(sensors):
        t.record(s, at_ms + 10 * i)


def test_unconfigured_tracker_ignores_presses():
    t = SyncScoreTracker()
    t.record(0, 0); t.record(1, 10); t.record(2, 20)
    t.tick(500)
    assert t.score == 0 and t.good_rounds == 0 and t.bad_rounds == 0


def test_good_rounds_add_gain_and_cap_at_one():
    t = _tracker()
    for k in range(6):
        _round(t, 1000 + 100 * k)
    t.tick(1000 + 100 * 6)
    assert t.good_rounds == 6
    assert t.score == pytest.approx(1.0) and t.complete


def test_first_round_after_a_pause_is_neutral_and_off_cadence_is_neutral():
    t = _tracker()
    _round(t, 1000)
    _round(t, 1100)
    _round(t, 1400)                # 300 ms after: together but off cadence
    _round(t, 1500)                # back on cadence relative to the last one
    t.tick(2000)
    assert t.good_rounds == 3 and t.bad_rounds == 0
    assert t.score == pytest.approx(0.75)


def test_missing_child_or_lone_press_costs_the_penalty_with_a_floor_at_zero():
    t = _tracker()
    _round(t, 1000)
    _round(t, 1100)
    assert t.score == pytest.approx(0.25)          # round 2 closed round 1... 
    t.tick(1200)                                   # ...and tick closes round 2
    assert t.score == pytest.approx(0.5)
    _round(t, 1200, sensors=(0, 1))                # child 2 missing
    t.tick(1300)
    assert t.bad_rounds == 1 and t.score == pytest.approx(0.25)
    t.record(0, 1300)                              # one child pressing alone
    t.tick(1400)
    t.record(1, 1400)
    t.tick(1500)
    assert t.bad_rounds == 3 and t.score == 0.0


def test_tick_only_closes_a_round_once_its_phase_window_expired():
    t = _tracker()
    _round(t, 1000)
    _round(t, 1100, sensors=(0, 1))
    t.tick(1130)                                   # still inside the window
    assert t.bad_rounds == 0
    t.record(2, 1135)                              # late but inside: complete
    t.tick(1200)
    assert t.bad_rounds == 0 and t.good_rounds == 2


def test_auto_mode_waits_for_enough_children_then_requires_late_joiners():
    t = _tracker(MODE_AUTO)
    _round(t, 1000, sensors=(0, 1))                # only two: nothing scores
    _round(t, 1100, sensors=(0, 1))
    t.tick(1200)
    assert t.score == 0 and t.bad_rounds == 0
    _round(t, 1200)                                # third child joins: counts
    t.tick(1300)
    assert t.sensors == [0, 1, 2] and t.good_rounds == 1
    _round(t, 1300, sensors=(0, 1, 2, 3))          # a fourth joins for good
    t.tick(1400)
    assert t.sensors == [0, 1, 2, 3]
    _round(t, 1400)                                # 3 press: the 4th is missing
    t.tick(1500)
    assert t.bad_rounds == 1


def test_fixed_mode_ignores_other_sensors_and_debounces():
    t = _tracker(MODE_FIXED)
    _round(t, 1000, sensors=(0, 1, 2, 3))          # sensor 3 is not in 0..2
    t.record(0, 1005)                              # chatter within 30 ms
    t.tick(1100)
    assert t.sensors == [0, 1, 2] and t.good_rounds == 1 and t.bad_rounds == 0


def test_reset_clears_score_and_group():
    t = _tracker()
    _round(t, 1000); t.tick(1100)
    t.reset()
    assert t.score == 0 and t.sensors == [] and t.status()["good_rounds"] == 0

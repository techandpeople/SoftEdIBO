"""Tests for shared-pump fill-time scaling (src.hardware.fill_scaling)."""

from __future__ import annotations

from src.hardware.fill_scaling import FillLoadTracker, effective_fill_ms


def test_single_chamber_reproduces_base():
    # One chamber, one pump → measured-alone time, scaled only by fill %.
    assert effective_fill_ms(1000, 100, active_chambers=1, pump_count=1) == 1000
    assert effective_fill_ms(1000, 50, active_chambers=1, pump_count=1) == 500


def test_concurrent_chambers_slow_each_other():
    # Two chambers sharing one pump → each fills ~twice as slow.
    assert effective_fill_ms(1000, 100, active_chambers=2, pump_count=1) == 2000
    assert effective_fill_ms(1000, 100, active_chambers=3, pump_count=1) == 3000


def test_more_pumps_share_the_load():
    # Two pumps absorb two concurrent chambers → still base time each.
    assert effective_fill_ms(1000, 100, active_chambers=2, pump_count=2) == 1000
    # Three chambers on two pumps → 1.5x.
    assert effective_fill_ms(1000, 100, active_chambers=3, pump_count=2) == 1500


def test_floor_never_under_inflates():
    # Fewer chambers than pumps must never drop below the base time.
    assert effective_fill_ms(1000, 100, active_chambers=1, pump_count=3) == 1000


def test_guards_clamp_bad_inputs():
    assert effective_fill_ms(1000, 100, active_chambers=0, pump_count=0) == 1000
    assert effective_fill_ms(1000, 999, active_chambers=1, pump_count=1) == 1000
    assert effective_fill_ms(1000, -5, active_chambers=1, pump_count=1) == 1


def test_tracker_counts_active_until_window_expires():
    now = [0.0]
    t = FillLoadTracker(pump_count=1, clock=lambda: now[0])
    assert t.active_count() == 0
    t.note_inflate(0, 1000)            # 1 s window
    t.note_inflate(1, 2000)            # 2 s window
    assert t.active_count() == 2
    now[0] = 1.5                       # first window elapsed
    assert t.active_count() == 1
    now[0] = 2.5                       # both elapsed
    assert t.active_count() == 0


def test_tracker_note_stop_releases_slot():
    now = [0.0]
    t = FillLoadTracker(clock=lambda: now[0])
    t.note_inflate(4, 5000)
    assert t.active_count() == 1
    t.note_stop(4)
    assert t.active_count() == 0


def test_tracker_drives_scaling_for_concurrent_fills():
    now = [0.0]
    t = FillLoadTracker(pump_count=1, clock=lambda: now[0])
    # First chamber starts alone.
    ms1 = effective_fill_ms(1000, 100, t.active_count() + 1, t.pump_count)
    t.note_inflate(0, ms1)
    assert ms1 == 1000
    # Second starts while the first is still filling → sees 2 active.
    ms2 = effective_fill_ms(1000, 100, t.active_count() + 1, t.pump_count)
    assert ms2 == 2000

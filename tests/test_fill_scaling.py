"""Tests for shared-pump fill-time scaling (src.hardware.fill_scaling)."""

from __future__ import annotations

from src.hardware.fill_scaling import (
    FULL_DUTY,
    DutyModel,
    FillLoadTracker,
    effective_fill_ms,
)


def test_single_chamber_reproduces_base():
    # One chamber, one pump -> measured-alone time, scaled only by fill %.
    assert effective_fill_ms(1000, 100, active_chambers=1, pump_count=1) == 1000
    assert effective_fill_ms(1000, 50, active_chambers=1, pump_count=1) == 500


def test_concurrent_chambers_slow_each_other():
    # Two chambers sharing one pump -> each fills ~twice as slow.
    assert effective_fill_ms(1000, 100, active_chambers=2, pump_count=1) == 2000
    assert effective_fill_ms(1000, 100, active_chambers=3, pump_count=1) == 3000


def test_duty_model_empty_below_two_points():
    assert DutyModel([]).is_empty
    assert DutyModel([[255, 1000]]).is_empty          # one point can't model a slope
    assert DutyModel.from_list(None) is None


def test_duty_model_picks_duty_for_requested_stretch():
    # At full duty a fill takes 1000 ms; at 128 it takes 2000 (2x slower); at 64,
    # 4000 (4x). So to stretch a natural 1000 ms fill to 2000 ms, use ~duty 128.
    m = DutyModel([[255, 1000], [128, 2000], [64, 4000]])
    assert not m.is_empty
    assert m.duty_for_period(1000, 2000) == 128        # exact measured point
    assert m.duty_for_period(1000, 1000) == FULL_DUTY   # no stretch -> full
    assert m.duty_for_period(1000, 500) == FULL_DUTY    # can't go faster than full
    # A stretch between measured factors interpolates the duty.
    d = m.duty_for_period(1000, 3000)                   # 3x slowdown, between 64 and 128
    assert 64 < d < 128


def test_duty_model_clamps_beyond_measured_range():
    m = DutyModel([[255, 1000], [128, 2000]])
    # Slower than the slowest measured duty -> clamp to that slowest duty.
    assert m.duty_for_period(1000, 9000) == 128


def test_duty_model_fixes_non_monotone_sweep_noise():
    # Sweep noise: duty 128 read faster (900) than full duty (1000). Forced monotone
    # so higher duty is never modelled as slower - both collapse to the fast time.
    m = DutyModel([[255, 1000], [128, 900], [64, 3000]])
    assert m.duty_for_period(1000, 1000) == FULL_DUTY


def test_more_pumps_share_the_load():
    # Two pumps absorb two concurrent chambers -> still base time each.
    assert effective_fill_ms(1000, 100, active_chambers=2, pump_count=2) == 1000
    # Three chambers on two pumps -> 1.5x.
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


def test_tracker_active_slots_returns_live_set_and_prunes():
    now = [0.0]
    t = FillLoadTracker(pump_count=1, clock=lambda: now[0])
    assert t.active_slots() == set()
    t.note_inflate(0, 1000)
    t.note_inflate(2, 2000)
    assert t.active_slots() == {0, 2}
    now[0] = 1.5                        # slot 0's window elapsed
    assert t.active_slots() == {2}
    now[0] = 2.5
    assert t.active_slots() == set()


def test_tracker_drives_scaling_for_concurrent_fills():
    now = [0.0]
    t = FillLoadTracker(pump_count=1, clock=lambda: now[0])
    # First chamber starts alone.
    ms1 = effective_fill_ms(1000, 100, t.active_count() + 1, t.pump_count)
    t.note_inflate(0, ms1)
    assert ms1 == 1000
    # Second starts while the first is still filling -> sees 2 active.
    ms2 = effective_fill_ms(1000, 100, t.active_count() + 1, t.pump_count)
    assert ms2 == 2000


def test_duty_for_period_full_speed_when_no_slowdown_wanted():
    from src.hardware.fill_scaling import duty_for_period, FULL_DUTY
    # period <= natural (can't go faster than full), or missing inputs -> full duty.
    assert duty_for_period(1000, 0) == FULL_DUTY
    assert duty_for_period(0, 2000) == FULL_DUTY
    assert duty_for_period(1000, 1000) == FULL_DUTY
    assert duty_for_period(1000, 800) == FULL_DUTY


def test_duty_for_period_scales_down_proportionally():
    from src.hardware.fill_scaling import duty_for_period, FULL_DUTY
    # 1.25x as long -> ~4/5 duty (still above the stall floor, so not clamped).
    assert duty_for_period(1000, 1250) == round(FULL_DUTY / 1.25)


def test_duty_for_period_floors_at_min_duty():
    from src.hardware.fill_scaling import duty_for_period, MIN_PUMP_DUTY
    # A very long period would compute a stalling duty; clamp to the floor.
    assert duty_for_period(100, 100000) == MIN_PUMP_DUTY


def test_duty_for_power_maps_endpoints_to_floor_and_full():
    from src.hardware.fill_scaling import duty_for_power, MIN_PUMP_DUTY, FULL_DUTY
    assert duty_for_power(1) == MIN_PUMP_DUTY          # level 1 = gentlest usable
    assert duty_for_power(5) == FULL_DUTY              # level 5 = full power
    # Midpoint sits halfway across [min, max].
    assert duty_for_power(3) == round((MIN_PUMP_DUTY + FULL_DUTY) / 2)


def test_duty_for_power_respects_configured_floor():
    from src.hardware.fill_scaling import duty_for_power, FULL_DUTY
    assert duty_for_power(1, min_duty=120) == 120
    assert duty_for_power(5, min_duty=120) == FULL_DUTY
    # Even spacing across the configured span.
    assert duty_for_power(2, min_duty=120) == round(120 + (FULL_DUTY - 120) / 4)


def test_duty_for_power_clamps_out_of_range_levels():
    from src.hardware.fill_scaling import duty_for_power, MIN_PUMP_DUTY, FULL_DUTY
    assert duty_for_power(0) == MIN_PUMP_DUTY          # below 1 clamps up to 1
    assert duty_for_power(9) == FULL_DUTY              # above 5 clamps down to 5
    # A floor above full is clamped so the pump never over-drives / inverts.
    assert duty_for_power(1, min_duty=999) == FULL_DUTY

"""Tests for the calibrated time→pressure fill curve (src.hardware.fill_profile)."""

from __future__ import annotations

from pytest import approx

from src.hardware.fill_profile import MAX_DEFLATE_MS, DeflateProfile, FillProfile


def test_anchors_at_ambient_and_keeps_order():
    p = FillProfile([(1000, 60), (500, 30), (2000, 95)])
    assert p.points[0] == (0.0, 0.0)            # ambient anchor prepended
    times = [t for t, _ in p.points]
    assert times == sorted(times)               # sorted by time
    assert p.full_time_ms == 2000
    assert p.top_pct == 95


def test_interpolates_partial_target():
    # 0%@0ms, 50%@1000ms, 100%@3000ms (non-linear: second half slower).
    p = FillProfile([(0, 0), (1000, 50), (3000, 100)])
    assert p.time_for_pct(0) == 0
    assert p.time_for_pct(25) == approx(500)    # halfway up the first segment
    assert p.time_for_pct(50) == approx(1000)
    assert p.time_for_pct(75) == approx(2000)   # halfway up the slower segment
    assert p.time_for_pct(100) == approx(3000)


def test_clamps_above_measured_top():
    p = FillProfile([(0, 0), (1000, 80)])       # sweep stalled at 80 %
    assert p.time_for_pct(80) == approx(1000)
    assert p.time_for_pct(100) == approx(1000)  # never extrapolates past measured
    assert p.time_for_pct(-5) == 0


def test_forces_monotone_pressure_over_noise():
    # A noisy dip (40 → 38) must not make the curve go backwards.
    p = FillProfile([(0, 0), (500, 40), (1000, 38), (1500, 70)])
    pts = dict(p.points)
    assert pts[1000.0] == 40                     # clamped up to the running max
    assert p.time_for_pct(40) == approx(500)


def test_linear_back_compat_matches_scalar():
    p = FillProfile.linear(2000)
    assert p is not None
    assert p.time_for_pct(50) == approx(1000)    # straight line to full
    assert p.time_for_pct(100) == approx(2000)


def test_linear_none_for_missing_scalar():
    assert FillProfile.linear(None) is None
    assert FillProfile.linear(0) is None


def test_round_trips_through_list():
    p = FillProfile([(0, 0), (480, 33.33), (1200, 95.0)])
    raw = p.to_list()
    assert raw[0] == [0, 0.0]
    back = FillProfile.from_list(raw)
    assert back is not None
    assert back.time_for_pct(95) == approx(1200)


def test_from_list_rejects_empty_or_degenerate():
    assert FillProfile.from_list(None) is None
    assert FillProfile.from_list([]) is None
    assert FillProfile.from_list([[0, 0]]) is None   # only an anchor → no rise
    assert FillProfile([]).is_empty
    assert FillProfile([(0, 0)]).is_empty


# ---------------------------------------------------------------------------
# DeflateProfile — the falling (vacuum-side) mirror
# ---------------------------------------------------------------------------

def test_deflate_anchors_at_start_and_falls_monotone():
    # Sweep from ~full down to the gauge floor (~6 %), with a noisy bounce.
    p = DeflateProfile([(100, 95), (500, 60), (900, 62), (1500, 6)])
    assert p.points[0] == (0.0, 95.0)             # anchored at the start level
    assert p.start_pct == 95
    assert p.floor_pct == 6                       # measured floor, not assumed 0
    pts = dict(p.points)
    assert pts[900.0] == 60                       # bounce clamped down (monotone)
    assert p.full_time_ms == 1500


def test_deflate_time_from_to_interpolates():
    # 100%@0 → 50%@1000 → 10%@3000 (tail slower).
    p = DeflateProfile([(0, 100), (1000, 50), (3000, 10)])
    assert p.time_from_to(100, 50) == approx(1000)
    assert p.time_from_to(75, 50) == approx(500)   # halfway down the fast segment
    assert p.time_from_to(50, 30) == approx(1000)  # halfway down the slow segment
    assert p.time_from_to(30, 60) == 0             # non-falling request → 0


def test_deflate_extrapolates_below_floor_with_cap():
    # Floor measured at 10 %; tail slope = 2000 ms / 40 pct = 50 ms/pct.
    p = DeflateProfile([(0, 100), (1000, 50), (3000, 10)])
    # From 50 % to floor is 2000 ms; 10 pct below floor adds 10 * 50 = 500 ms.
    assert p.extrapolate_ms(50, 0) == approx(2500)
    # Never past the hard vacuum cap, however deep the target.
    assert p.extrapolate_ms(100, -400) == MAX_DEFLATE_MS
    # A target at/above the floor is just the measured time (no extrapolation).
    assert p.extrapolate_ms(50, 10) == approx(2000)


def test_deflate_rejects_empty_or_flat():
    assert DeflateProfile.from_list(None) is None
    assert DeflateProfile.from_list([]) is None
    assert DeflateProfile([]).is_empty
    assert DeflateProfile([(0, 50), (1000, 50)]).is_empty   # never fell


def test_deflate_round_trips_through_list():
    p = DeflateProfile([(0, 95), (800, 40.4), (2000, 6.0)])
    back = DeflateProfile.from_list(p.to_list())
    assert back is not None
    assert back.floor_pct == approx(6.0)
    assert back.time_from_to(95, 40.4) == approx(800)

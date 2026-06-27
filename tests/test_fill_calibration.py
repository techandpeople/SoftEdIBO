"""Tests for chamber fill-curve calibration core + settings helpers."""

from pytest import approx

from src.hardware.fill_calibration import (
    FillProfileCalibrator,
    chambers_missing_calibration,
    iter_actuator_chambers,
    set_fill_profile,
)


def test_sweep_records_curve_until_target_reached():
    cal = FillProfileCalibrator(step_ms=500, target_pct=95)
    # Each step opens the valve for 500 ms; the driver feeds the settled pct.
    assert cal.record(20) is False        # 500 ms → 20 %
    assert cal.record(55) is False        # 1000 ms → 55 %
    assert cal.record(96) is True         # 1500 ms → 96 %, crosses target
    assert not cal.timed_out
    assert cal.elapsed_ms == 1500
    prof = cal.profile
    assert prof.time_for_pct(55) == approx(1000)
    assert prof.full_time_ms == 1500


def test_sweep_times_out_on_asymptotic_creep():
    cal = FillProfileCalibrator(step_ms=1000, target_pct=98, max_total_ms=3000)
    assert cal.record(40) is False        # 1000
    assert cal.record(60) is False        # 2000
    assert cal.record(70) is True         # 3000 → hits the ceiling, never 98 %
    assert cal.timed_out
    assert cal.profile.top_pct == 70


def test_finer_step_yields_more_points():
    coarse = FillProfileCalibrator(step_ms=500, target_pct=90)
    for pct in (40, 95):
        coarse.record(pct)
    fine = FillProfileCalibrator(step_ms=250, target_pct=90)
    for pct in (20, 45, 70, 95):
        fine.record(pct)
    assert fine.steps > coarse.steps


def _settings():
    return {"robots": {"turtles": [{
        "id": "turtle_1",
        "nodes": [
            {"mac": "AA:01", "node_type": "node_direct"},
            {"mac": "BB:02", "node_type": "node_magnet_sensor"},
        ],
        "skins": [{
            "skin_id": "belly",
            "chambers": [
                {"mac": "AA:01", "slot": 0, "max_pressure": 8.0},
                {"mac": "AA:01", "slot": 1, "max_pressure": 8.0,
                 "fill_time_ms": 1800},               # legacy scalar still counts
            ],
        }],
    }]}}


def test_iter_actuator_chambers_reports_calibration_state():
    chs = iter_actuator_chambers(_settings())
    assert len(chs) == 2                  # both on the node_direct
    by_slot = {c["slot"]: c for c in chs}
    assert by_slot[0]["calibrated"] is False
    assert by_slot[1]["calibrated"] is True       # has legacy fill_time_ms
    assert by_slot[1]["fill_time_ms"] == 1800
    assert all(c["node_type"] == "node_direct" for c in chs)


def test_set_fill_profile_writes_and_drops_legacy_scalar():
    data = _settings()
    curve = [[0, 0], [500, 30], [1200, 95]]
    # Slot 1 had a legacy fill_time_ms — writing a profile must drop it.
    assert set_fill_profile(data, "AA:01", 1, curve) == 1
    ch1 = data["robots"]["turtles"][0]["skins"][0]["chambers"][1]
    assert ch1["fill_profile"] == curve
    assert "fill_time_ms" not in ch1
    # Clearing removes the profile.
    assert set_fill_profile(data, "AA:01", 1, None) == 1
    assert "fill_profile" not in ch1


def test_chambers_missing_calibration():
    missing = chambers_missing_calibration(_settings())
    assert [c["slot"] for c in missing] == [0]    # slot 1 has the legacy scalar

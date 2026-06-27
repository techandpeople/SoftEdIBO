"""Tests for chamber fill-curve calibration core + settings helpers."""

from pytest import approx

from src.hardware.fill_calibration import (
    FillProfileCalibrator,
    MultiChamberFillCalibrator,
    chambers_missing_calibration,
    combo_key,
    iter_actuator_chambers,
    iter_actuator_nodes,
    parse_combo_key,
    set_fill_profile,
    set_fill_profiles,
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


def test_multi_chamber_sweeps_in_lockstep_with_dropout():
    cal = MultiChamberFillCalibrator([0, 1], step_ms=500, target_pct=90)
    assert cal.slots == [0, 1]
    assert cal.pending_slots() == [0, 1]
    # Step 1: both still filling.
    assert cal.record({0: 30, 1: 20}) is False
    assert cal.pending_slots() == [0, 1]
    # Step 2: slot 0 crosses target and drops out; slot 1 keeps going.
    assert cal.record({0: 95, 1: 50}) is False
    assert cal.pending_slots() == [1]
    assert cal.calibrator(0).done is True
    # Step 3: only slot 1 is fed; once it crosses target the run is done.
    assert cal.record({1: 92}) is True
    assert cal.done is True
    profiles = cal.profiles()
    # Slot 0 stopped at step 2 (1000 ms), slot 1 ran one step longer (1500 ms).
    assert profiles[0][-1] == [1000, 95.0]
    assert profiles[1][-1] == [1500, 92.0]


def test_multi_chamber_single_slot_is_a_solo_sweep():
    cal = MultiChamberFillCalibrator([2], step_ms=500, target_pct=90)
    assert cal.record({2: 40}) is False
    assert cal.record({2: 95}) is True
    assert cal.profiles()[2][-1] == [1000, 95.0]


def test_combo_key_round_trips_sorted_and_deduped():
    assert combo_key([2, 0, 1, 0]) == "0,1,2"
    assert parse_combo_key("0,1,2") == frozenset({0, 1, 2})
    assert parse_combo_key(combo_key([3, 1])) == frozenset({1, 3})


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


def test_pressure_mode_chamber_not_flagged_for_calibration():
    data = _settings()
    # Slot 0 is uncalibrated but set to pressure fill mode → needs no curve.
    data["robots"]["turtles"][0]["skins"][0]["chambers"][0]["fill_mode"] = "pressure"
    assert chambers_missing_calibration(data) == []


def test_iter_actuator_nodes_groups_chambers_by_mac():
    nodes = iter_actuator_nodes(_settings())
    assert len(nodes) == 1                      # only the node_direct actuates
    node = nodes[0]
    assert node["mac"] == "AA:01"
    assert node["node_type"] == "node_direct"
    assert node["slots"] == [0, 1]
    assert len(node["chambers"]) == 2


def test_set_fill_profiles_writes_combo_map_and_drops_legacy_scalar():
    data = _settings()
    combos = {"0,1": [[0, 0], [600, 40], [1500, 92]]}
    # Slot 1 had a legacy fill_time_ms — writing a measured combo must drop it.
    assert set_fill_profiles(data, "AA:01", 1, combos) == 1
    ch1 = data["robots"]["turtles"][0]["skins"][0]["chambers"][1]
    assert ch1["fill_profiles"] == combos
    assert "fill_time_ms" not in ch1
    # Combos surface through iter_actuator_chambers for pre-display.
    by_slot = {c["slot"]: c for c in iter_actuator_chambers(data)}
    assert by_slot[1]["fill_profiles"] == combos
    # Clearing removes the map.
    assert set_fill_profiles(data, "AA:01", 1, None) == 1
    assert "fill_profiles" not in ch1


def test_combos_do_not_gate_activity_start():
    # A chamber with only combo curves but no solo curve still counts as missing
    # calibration — combinations are a refinement, not the solo prerequisite.
    data = _settings()
    ch0 = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    ch0["fill_profiles"] = {"0,1": [[0, 0], [800, 95]]}
    assert [c["slot"] for c in chambers_missing_calibration(data)] == [0]

"""Tests for chamber fill-curve calibration core + settings helpers."""

from pytest import approx

from src.hardware.fill_calibration import (
    ContinuousDeflateCalibrator,
    ContinuousFillCalibrator,
    FillProfileCalibrator,
    MultiChamberFillCalibrator,
    PlateauDetector,
    chambers_missing_calibration,
    combo_key,
    get_type_deflate_profile,
    get_type_min_duty,
    get_type_profile,
    iter_actuator_chambers,
    iter_actuator_nodes,
    parse_combo_key,
    resolve_fill_profiles,
    set_deflate_profile,
    set_fill_profile,
    set_fill_profiles,
    set_type_deflate_profile,
    set_type_min_duty,
    set_type_profile,
    type_slug,
)


def test_sweep_records_curve_until_target_reached():
    cal = FillProfileCalibrator(step_ms=500, target_pct=95)
    # Each step opens the valve for 500 ms; the driver feeds the settled pct.
    assert cal.record(20) is False        # 500 ms -> 20 %
    assert cal.record(55) is False        # 1000 ms -> 55 %
    assert cal.record(96) is True         # 1500 ms -> 96 %, crosses target
    assert not cal.timed_out
    assert cal.elapsed_ms == 1500
    prof = cal.profile
    assert prof.time_for_pct(55) == approx(1000)
    assert prof.full_time_ms == 1500


def test_sweep_times_out_on_asymptotic_creep():
    cal = FillProfileCalibrator(step_ms=1000, target_pct=98, max_total_ms=3000)
    assert cal.record(40) is False        # 1000
    assert cal.record(60) is False        # 2000
    assert cal.record(70) is True         # 3000 -> hits the ceiling, never 98 %
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


def test_continuous_sweep_builds_dense_curve_until_target():
    cal = ContinuousFillCalibrator(target_pct=95)
    # Stream of (elapsed_ms, pct) while the valve is held open from ambient.
    assert cal.record(40, 8) is False
    assert cal.record(80, 22) is False
    assert cal.record(120, 41) is False
    assert cal.record(160, 63) is False
    assert cal.record(200, 96) is True        # crosses target
    assert not cal.timed_out
    assert cal.elapsed_ms == 200
    assert cal.samples == 5
    prof = cal.profile
    assert prof.full_time_ms == 200
    # Interpolates between the dense samples.
    assert prof.time_for_pct(41) == approx(120)


def test_continuous_sweep_times_out_on_creep():
    cal = ContinuousFillCalibrator(target_pct=98, max_total_ms=300)
    assert cal.record(100, 40) is False
    assert cal.record(200, 60) is False
    assert cal.record(300, 70) is True        # hits the ceiling, never 98 %
    assert cal.timed_out
    assert cal.profile.top_pct == 70


def test_continuous_sweep_tracks_top_pct_cheaply():
    cal = ContinuousFillCalibrator(target_pct=95)
    assert cal.top_pct == 0
    cal.record(100, 40)
    assert cal.top_pct == 40
    cal.record(200, 35)                       # sensor dip - top holds
    assert cal.top_pct == 40
    cal.record(300, 96)
    assert cal.top_pct == 96
    assert cal.top_pct == cal.profile.top_pct


def test_continuous_sweep_ignores_non_monotone_samples():
    cal = ContinuousFillCalibrator(target_pct=95)
    assert cal.record(100, 30) is False
    assert cal.record(100, 34) is False       # duplicate timestamp - ignored
    assert cal.record(80, 40) is False        # out-of-order - ignored
    assert cal.samples == 1
    assert cal.elapsed_ms == 100
    assert cal.record(150, 96) is True        # later sample still lands


def test_plateau_detector_declares_floor_when_drop_stops():
    det = PlateauDetector(min_drop=0.5, settle_ms=600, min_ms=300)
    assert det.update(100, 50.0) is False        # still early (min_ms)
    assert det.update(400, 30.0) is False        # falling - floor keeps moving
    assert det.update(800, 10.0) is False
    assert det.update(1200, 9.8) is False        # < min_drop: not "falling"
    assert det.update(1400, 9.9) is True         # 600 ms without a real drop
    # A meaningful new drop rearms the window.
    det2 = PlateauDetector(min_drop=0.5, settle_ms=600, min_ms=0)
    det2.update(0, 10.0)
    det2.update(500, 9.8)
    assert det2.update(550, 8.0) is False        # real drop at 550 -> window resets
    assert det2.update(1100, 7.9) is False       # only 550 ms since the drop
    assert det2.update(1200, 7.9) is True


def test_continuous_deflate_sweep_ends_on_measured_floor():
    cal = ContinuousDeflateCalibrator(plateau_drop_pct=1.0, plateau_ms=500)
    # Falling stream; the gauge floor sits at ~6 % (ambient != 0 on this sensor).
    assert cal.record(300, 95) is False
    assert cal.record(600, 60) is False
    assert cal.record(900, 20) is False
    assert cal.record(1200, 6.2) is False
    assert cal.record(1500, 6.1) is False        # < 1 % drop - plateau window runs
    assert cal.record(1800, 6.0) is True         # 600 ms without a real drop
    assert not cal.timed_out
    prof = cal.profile
    assert prof.floor_pct == approx(6.0)
    assert prof.time_from_to(95, 20) == approx(900 - 300)


def test_continuous_deflate_sweep_times_out():
    cal = ContinuousDeflateCalibrator(max_total_ms=1000,
                                      plateau_drop_pct=1.0, plateau_ms=5000)
    assert cal.record(400, 80) is False
    assert cal.record(1000, 40) is True          # ceiling
    assert cal.timed_out


def test_set_deflate_profile_writes_and_clears():
    data = _settings()
    curve = [[0.0, 95.0], [900.0, 40.0], [2000.0, 6.0]]
    assert set_deflate_profile(data, "AA:01", 0, curve) == 1
    ch0 = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    assert ch0["deflate_profile"] == curve
    by_slot = {c["slot"]: c for c in iter_actuator_chambers(data)}
    assert by_slot[0]["deflate_profile"] == curve
    assert set_deflate_profile(data, "AA:01", 0, None) == 1
    assert "deflate_profile" not in ch0


def test_resolver_inherits_deflate_template_and_override_wins():
    data = {}
    set_type_deflate_profile(data, "tree_round", "organ", 0, [[0, 95], [1500, 6]])
    assert get_type_deflate_profile(data, "tree_round", "organ", 0) is not None
    skins = [{
        "skin_id": "b1", "skin_type": "tree_round", "skin_variant": "organ",
        "chambers": [
            {"mac": "AA:01", "slot": 0},                              # inherits
            {"mac": "AA:01", "slot": 0,
             "deflate_profile": [[0, 90], [800, 10]]},                # override wins
        ],
    }]
    chs = resolve_fill_profiles(data, skins)[0]["chambers"]
    assert chs[0]["deflate_profile"] == [[0, 95], [1500, 6]]
    assert chs[1]["deflate_profile"] == [[0, 90], [800, 10]]
    # Clearing prunes the whole map.
    set_type_deflate_profile(data, "tree_round", "organ", 0, None)
    assert "deflate_profiles_by_type" not in data


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
    curve = [[0.0, 0.0], [500.0, 30.0], [1200.0, 95.0]]
    # Slot 1 had a legacy fill_time_ms - writing a profile must drop it.
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
    # Slot 0 is uncalibrated but set to pressure fill mode -> needs no curve.
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
    combos = {"0,1": [[0.0, 0.0], [600.0, 40.0], [1500.0, 92.0]]}
    # Slot 1 had a legacy fill_time_ms - writing a measured combo must drop it.
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


def test_type_slug_composes_type_and_variant():
    assert type_slug("tree_round", "organ") == "tree_round_organ"
    assert type_slug("thymio", "") == "thymio"          # no variant
    assert type_slug("", "organ") == ""                 # no type -> no template


def test_set_and_get_type_profile_round_trip_and_prune():
    data = {}
    curve = [[0, 0], [500, 40], [1200, 95]]
    assert set_type_profile(data, "tree_round", "organ", 0, curve) is True
    assert data["fill_profiles_by_type"]["tree_round_organ"]["0"] == curve
    assert get_type_profile(data, "tree_round", "organ", 0) == curve
    # A different slot of the same type lives alongside.
    set_type_profile(data, "tree_round", "organ", 1, [[0, 0], [800, 90]])
    assert set(data["fill_profiles_by_type"]["tree_round_organ"]) == {"0", "1"}
    # Clearing prunes the slot, then the type, then the whole map.
    set_type_profile(data, "tree_round", "organ", 0, None)
    set_type_profile(data, "tree_round", "organ", 1, None)
    assert "fill_profiles_by_type" not in data
    # An un-typed skin can't key a template.
    assert set_type_profile(data, "", "", 0, curve) is False
    assert get_type_profile(data, "", "", 0) is None


def test_set_and_get_type_min_duty_round_trip_and_prune():
    from src.hardware.fill_scaling import MIN_PUMP_DUTY, FULL_DUTY
    data: dict = {}
    # Absent -> the global default floor.
    assert get_type_min_duty(data, "tree_round", "organ") == MIN_PUMP_DUTY
    assert set_type_min_duty(data, "tree_round", "organ", 150) is True
    assert data["min_pump_duty_by_type"]["tree_round_organ"] == 150
    assert get_type_min_duty(data, "tree_round", "organ") == 150
    # A stray value is clamped into [1, FULL_DUTY].
    set_type_min_duty(data, "tree_round", "organ", 9999)
    assert get_type_min_duty(data, "tree_round", "organ") == FULL_DUTY
    # Clearing prunes the whole map; an un-typed skin can't key one.
    set_type_min_duty(data, "tree_round", "organ", None)
    assert "min_pump_duty_by_type" not in data
    assert set_type_min_duty(data, "", "", 150) is False


def test_resolve_fill_profiles_inherits_template_without_mutating():
    data = {"fill_profiles_by_type": {
        "tree_round_organ": {"0": [[0, 0], [900, 95]]}}}
    skins = [{
        "skin_id": "branch-1", "skin_type": "tree_round", "skin_variant": "organ",
        "chambers": [
            {"mac": "AA:01", "slot": 0},                          # inherits template
            {"mac": "AA:01", "slot": 1},                          # no template -> nothing
            {"mac": "AA:01", "slot": 0, "fill_profile": [[0, 0], [100, 50]]},  # override wins
        ],
    }]
    resolved = resolve_fill_profiles(data, skins)
    chs = resolved[0]["chambers"]
    assert chs[0]["fill_profile"] == [[0, 0], [900, 95]]     # inherited
    assert "fill_profile" not in chs[1]                       # slot 1 has no template
    assert chs[2]["fill_profile"] == [[0, 0], [100, 50]]      # own override untouched
    # The source skin dicts are never mutated (template stays the single source).
    assert "fill_profile" not in skins[0]["chambers"][0]


def test_combos_do_not_gate_activity_start():
    # A chamber with only combo curves but no solo curve still counts as missing
    # calibration - combinations are a refinement, not the solo prerequisite.
    data = _settings()
    ch0 = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    ch0["fill_profiles"] = {"0,1": [[0, 0], [800, 95]]}
    assert [c["slot"] for c in chambers_missing_calibration(data)] == [0]

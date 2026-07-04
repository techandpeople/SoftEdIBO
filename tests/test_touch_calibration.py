"""Tests for touch-coupling calibration settings helpers + sample→config core."""

from src.hardware.touch_calibration import (
    SweepProgram,
    sweep_diagnostics,
    coupling_config_from_samples,
    iter_touch_skins,
    set_compensation,
    set_touch_coupling,
)


def _settings():
    return {"robots": {"turtles": [{
        "id": "turtle_1",
        "nodes": [
            {"mac": "AA:01", "node_type": "node_direct"},      # actuator + magnet merged
            {"mac": "CC:03", "node_type": "node_multiplexed"},
        ],
        "skins": [
            {"skin_id": "belly",
             "chambers": [{"mac": "AA:01", "slot": 0}, {"mac": "AA:01", "slot": 1}],
             "touch": {"node_mac": "AA:01", "sensor_count": 4}},
            {"skin_id": "leg",   # multiplexed node has no magnet bus → excluded
             "chambers": [{"mac": "CC:03", "slot": 0}],
             "touch": {"node_mac": "CC:03", "sensor_count": 4}},
        ],
    }]}}


def test_iter_touch_skins_only_magnet_capable():
    skins = iter_touch_skins(_settings())
    assert [s["skin_id"] for s in skins] == ["belly"]
    belly = skins[0]
    assert belly["touch_mac"] == "AA:01"
    assert belly["chamber_mac"] == "AA:01"
    assert belly["slots"] == [0, 1]
    assert belly["enabled"] is False


def test_set_touch_coupling_and_compensation_round_trip():
    data = _settings()
    cfg = {"unit": "uT", "sensor_count": 4, "ref_pct": 100.0,
           "deltas": {"0": [200, 0, 0, 0]}}
    assert set_touch_coupling(data, "turtle_1", "belly", cfg) is True
    assert set_compensation(data, "turtle_1", "belly", enabled=True,
                            threshold_ut=120.0) is True
    skins = {s["skin_id"]: s for s in iter_touch_skins(data)}
    assert skins["belly"]["coupling"] == cfg
    assert skins["belly"]["enabled"] is True
    touch = data["robots"]["turtles"][0]["skins"][0]["touch"]
    assert touch["compensation"]["threshold_ut"] == 120.0


def test_set_touch_coupling_unknown_skin_returns_false():
    assert set_touch_coupling(_settings(), "turtle_1", "nope", {"x": 1}) is False


def test_clear_coupling_drops_key():
    data = _settings()
    set_touch_coupling(data, "turtle_1", "belly", {"deltas": {"0": [1]}})
    set_touch_coupling(data, "turtle_1", "belly", None)
    assert "coupling" not in data["robots"]["turtles"][0]["skins"][0]["touch"]


def test_suppress_pct_sentinel_leaves_unchanged_then_clears():
    data = _settings()
    set_compensation(data, "turtle_1", "belly", suppress_pct=90.0)
    comp = data["robots"]["turtles"][0]["skins"][0]["touch"]["compensation"]
    assert comp["suppress_pct"] == 90.0
    set_compensation(data, "turtle_1", "belly", enabled=True)   # sentinel default
    assert comp["suppress_pct"] == 90.0                         # unchanged
    set_compensation(data, "turtle_1", "belly", suppress_pct=None)   # explicit clear
    assert "suppress_pct" not in comp


def test_coupling_config_from_samples():
    # Two chambers (slots 0,1) over 2 sensors; chamber 0 moves sensor 0 strongly.
    # rest baseline ~ [10, 10]; chamber 0 inflated ~ [210, 12].
    samples = []
    t = 0.0
    # rest dwell
    for _ in range(20):
        samples.append((t, {0: 0.0, 1: 0.0}, [10.0, 10.0])); t += 100
    # chamber 0 inflated dwell (well past settle window)
    for _ in range(20):
        samples.append((t, {0: 100.0, 1: 0.0}, [210.0, 12.0])); t += 100
    cfg, matrix = coupling_config_from_samples(samples, sensor_count=2)
    assert cfg["unit"] == "uT"
    assert cfg["sensor_count"] == 2
    # chamber 0 delta on sensor 0 ≈ 200, sensor 1 ≈ 2
    assert abs(cfg["deltas"]["0"][0] - 200.0) < 1.0
    assert abs(cfg["deltas"]["0"][1] - 2.0) < 1.0


# --- SweepProgram + curve config --------------------------------------------


def test_levels_for_counts():
    assert SweepProgram.levels_for(1) == (100.0,)
    assert SweepProgram.levels_for(4) == (25.0, 50.0, 75.0, 100.0)
    assert SweepProgram.levels_for(5)[0] == 25.0     # floored, not 20
    assert SweepProgram.levels_for(0) == (100.0,)    # clamped


def test_sweep_program_step_sequence():
    prog = SweepProgram([0, 1], (50.0, 100.0))
    assert [(s.action, s.slot, s.level) for s in prog.steps] == [
        ("deflate_all", None, 0.0),
        ("set_pressure", 0, 50.0), ("set_pressure", 0, 100.0), ("deflate", 0, 0.0),
        ("set_pressure", 1, 50.0), ("set_pressure", 1, 100.0), ("deflate", 1, 0.0),
    ]
    progress = [s.progress for s in prog.steps]
    assert progress[0] == 0 and progress == sorted(progress)
    assert all(s.wait_ms > 0 for s in prog.steps)


def test_set_compensation_margin_and_guard():
    data = _settings()
    set_compensation(data, "turtle_1", "belly", margin_frac=0.25, guard_ms=800.0)
    comp = data["robots"]["turtles"][0]["skins"][0]["touch"]["compensation"]
    assert comp["margin_frac"] == 0.25 and comp["guard_ms"] == 800.0
    set_compensation(data, "turtle_1", "belly", enabled=True)   # None = unchanged
    assert comp["margin_frac"] == 0.25 and comp["guard_ms"] == 800.0


def test_coupling_config_from_samples_writes_curves():
    samples = []
    t = 0.0
    for _ in range(20):
        samples.append((t, {0: 0.0}, [10.0])); t += 100
    for _ in range(20):
        samples.append((t, {0: 50.0}, [110.0])); t += 100
    for _ in range(20):
        samples.append((t, {0: 100.0}, [210.0])); t += 100
    cfg, matrix = coupling_config_from_samples(samples, sensor_count=1)
    points = cfg["curves"]["0"]
    assert [round(p["pct"]) for p in points] == [50, 100]
    assert abs(points[0]["mag"][0] - 100.0) < 1.0
    assert abs(cfg["deltas"]["0"][0] - 200.0) < 1.0   # legacy view at ref 100


# --- kPa limits + empty-sweep diagnostics ------------------------------------

def test_iter_touch_skins_exposes_kpa_limits():
    data = _settings()
    chambers = data["robots"]["turtles"][0]["skins"][0]["chambers"]
    chambers[0]["min_pressure"] = 5.0
    chambers[0]["max_pressure"] = 30.0
    skins = iter_touch_skins(data)
    limits = skins[0]["limits"]
    assert limits[0] == (5.0, 30.0)
    assert limits[1] == (0.0, 8.0)       # defaults (DEFAULT_MIN/MAX_KPA)


def test_sweep_diagnostics_no_samples():
    text = sweep_diagnostics([], [0, 1])
    assert "0 magnet samples" in text


def test_sweep_diagnostics_levels_never_active():
    samples = [(0.0, {0: 12.0, 1: 3.0}, [1.0]), (100.0, {0: 8.0, 1: 5.0}, [1.0])]
    text = sweep_diagnostics(samples, [0, 1])
    assert "2 magnet samples" in text
    assert "slot 0: 12%" in text and "slot 1: 5%" in text
    assert "kPa limits" in text          # the below-ACTIVE_MIN hint fired

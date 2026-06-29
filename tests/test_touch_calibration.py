"""Tests for touch-coupling calibration settings helpers + sample→config core."""

from src.hardware.touch_calibration import (
    coupling_config_from_samples,
    iter_touch_skins,
    set_compensation,
    set_touch_coupling,
)


def _settings():
    return {"robots": {"turtle_trees": [{
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
    touch = data["robots"]["turtle_trees"][0]["skins"][0]["touch"]
    assert touch["compensation"]["threshold_ut"] == 120.0


def test_set_touch_coupling_unknown_skin_returns_false():
    assert set_touch_coupling(_settings(), "turtle_1", "nope", {"x": 1}) is False


def test_clear_coupling_drops_key():
    data = _settings()
    set_touch_coupling(data, "turtle_1", "belly", {"deltas": {"0": [1]}})
    set_touch_coupling(data, "turtle_1", "belly", None)
    assert "coupling" not in data["robots"]["turtle_trees"][0]["skins"][0]["touch"]


def test_suppress_pct_sentinel_leaves_unchanged_then_clears():
    data = _settings()
    set_compensation(data, "turtle_1", "belly", suppress_pct=90.0)
    comp = data["robots"]["turtle_trees"][0]["skins"][0]["touch"]["compensation"]
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

"""Unit tests for src.core.skin_config (pure, Qt-free domain logic)."""

from __future__ import annotations

from src.core import skin_config as skincfg


def _data() -> dict:
    """A settings tree with one turtle robot: 2 actuator nodes + 1 magnet node."""
    return {
        "robots": {
            "turtles": [{
                "nodes": [
                    {"mac": "AA", "node_type": "node_direct", "max_slots": 3},
                    {"mac": "BB", "node_type": "node_multiplexed", "max_slots": 12},
                    {"mac": "MM", "node_type": "node_magnet_sensor"},
                    {"node_type": "node_direct"},  # no mac -> ignored
                ],
                "skins": [
                    {"skin_id": "belly-1",
                     "chambers": [{"mac": "AA", "slot": 0}]},
                    {"skin_id": "belly-2",
                     "chambers": [{"mac": "BB", "slot": 5}]},
                ],
            }],
        },
    }


# --- navigation -----------------------------------------------------------

def test_actuator_macs_excludes_magnet_and_macless():
    assert skincfg.actuator_macs(_data(), "turtle", 0) == ["AA", "BB"]


def test_magnet_macs():
    # node_direct folds in the magnet sensing, so its MAC is also selectable as
    # a touch node, alongside the dedicated node_magnet_sensor board.
    assert skincfg.magnet_macs(_data(), "turtle", 0) == ["AA", "MM"]


def test_node_max_slots():
    assert skincfg.node_max_slots(_data(), "turtle", 0) == {"AA": 3, "BB": 12}


def test_robot_nodes_out_of_range():
    assert skincfg.robot_nodes(_data(), "turtle", 9) == []


def test_load_skin_cfg_new_skin_is_empty():
    assert skincfg.load_skin_cfg(_data(), "turtle", 0, -1) == {}


def test_load_skin_cfg_existing():
    assert skincfg.load_skin_cfg(_data(), "turtle", 0, 0)["skin_id"] == "belly-1"


def test_sibling_skins_excludes_self():
    sibs = skincfg.sibling_skins(_data(), "turtle", 0, 0)
    assert [s["skin_id"] for s in sibs] == ["belly-2"]


# --- next_skin_id ---------------------------------------------------------

def test_next_skin_id_increments():
    ids = ["belly-1", "belly-2", "head-1"]
    assert skincfg.next_skin_id("belly", ids) == "belly-3"


def test_next_skin_id_first():
    assert skincfg.next_skin_id("Belly Skin", []) == "belly_skin-1"


def test_next_skin_id_blank_source_falls_back():
    assert skincfg.next_skin_id("", []) == "skin-1"


def _two_kind_data():
    return {"robots": {
        "turtles": [{"id": "turtle-1", "skins": [{"skin_id": "turtle-1"}]}],
        "thymios": [
            {"thymio_id": "thymio-1", "skins": [{"skin_id": "thymio-1"}]},
            {"thymio_id": "thymio-2", "skins": [{"skin_id": "Thymio-2"},
                                                {"skin_id": ""}]},
        ],
    }}


def test_all_skin_ids_spans_every_robot_and_skips_blank():
    assert sorted(skincfg.all_skin_ids(_two_kind_data())) == [
        "Thymio-2", "thymio-1", "turtle-1"]


def test_all_skin_ids_excludes_the_edited_skin():
    ids = skincfg.all_skin_ids(_two_kind_data(), exclude=("thymio", 0, 0))
    assert "thymio-1" not in ids and "Thymio-2" in ids


def test_new_skin_id_is_unique_across_robots():
    ids = skincfg.all_skin_ids(_two_kind_data())
    assert skincfg.next_skin_id("thymio", ids) == "thymio-3"
    assert skincfg.next_skin_id("tree", ids) == "tree-1"


def test_skin_id_taken_ignores_case_and_spaces():
    ids = skincfg.all_skin_ids(_two_kind_data())
    assert skincfg.skin_id_taken(" THYMIO-2 ", ids)
    assert not skincfg.skin_id_taken("thymio-3", ids)


# --- validation -----------------------------------------------------------

def test_find_missing_mac():
    assert skincfg.find_missing_mac([{"mac": "AA", "slot": 0}, {"mac": "", "slot": 1}])
    assert not skincfg.find_missing_mac([{"mac": "AA", "slot": 0}])


def test_find_duplicate():
    chambers = [{"mac": "AA", "slot": 0}, {"mac": "AA", "slot": 0}]
    assert skincfg.find_duplicate(chambers) == {"mac": "AA", "slot": 0}
    assert skincfg.find_duplicate([{"mac": "AA", "slot": 0},
                                   {"mac": "AA", "slot": 1}]) is None


def test_sibling_conflicts():
    siblings = [{"chambers": [{"mac": "AA", "slot": 0}]}]
    chambers = [{"mac": "AA", "slot": 0}, {"mac": "BB", "slot": 1}]
    conflicts = skincfg.sibling_conflicts(chambers, siblings)
    assert conflicts == [{"mac": "AA", "slot": 0}]


def test_large_pressure_changes():
    prev = [{"mac": "AA", "slot": 0, "max_pressure": 8.0}]
    near_default = skincfg.DEFAULT_MAX_KPA + 0.5      # new chamber: vs the default
    chambers = [{"mac": "AA", "slot": 0, "max_pressure": 11.0},
                {"mac": "BB", "slot": 1, "max_pressure": near_default}]
    changes = skincfg.large_pressure_changes(chambers, prev)
    assert len(changes) == 1
    assert changes[0].mac == "AA" and changes[0].new_kpa == 11.0


# --- entry building + persistence ----------------------------------------

def test_build_skin_entry_strips_default_pressure():
    chambers = [{"mac": "AA", "slot": 0, "max_pressure": skincfg.DEFAULT_MAX_KPA},
                {"mac": "BB", "slot": 1, "max_pressure": 10.0}]
    entry = skincfg.build_skin_entry("belly-1", chambers,
                                     skin_type="round_4", skin_variant="v2")
    assert "max_pressure" not in entry["chambers"][0]
    assert entry["chambers"][1]["max_pressure"] == 10.0
    assert entry["skin_type"] == "round_4"
    assert entry["skin_variant"] == "v2"
    # input list must not be mutated
    assert chambers[0]["max_pressure"] == skincfg.DEFAULT_MAX_KPA


def test_default_limits():
    assert skincfg.DEFAULT_MAX_KPA == 30.0
    assert skincfg.DEFAULT_MIN_KPA == 0.0
    assert skincfg.DEFAULT_VACUUM_MS == 1000


def test_build_skin_entry_drops_min_pressure_and_default_vacuum():
    chambers = [{"mac": "AA", "slot": 0, "min_pressure": -8.0,
                 "vacuum_ms": skincfg.DEFAULT_VACUUM_MS},
                {"mac": "BB", "slot": 1, "vacuum_ms": 2000}]
    entry = skincfg.build_skin_entry("belly-1", chambers)
    assert "min_pressure" not in entry["chambers"][0]   # no longer configurable
    assert "vacuum_ms" not in entry["chambers"][0]      # default dropped
    assert entry["chambers"][1]["vacuum_ms"] == 2000    # non-default kept
    # input list must not be mutated
    assert chambers[0]["min_pressure"] == -8.0


def test_chamber_vacuum_ms_defaults_and_clamps():
    assert skincfg.chamber_vacuum_ms({}) == skincfg.DEFAULT_VACUUM_MS
    assert skincfg.chamber_vacuum_ms({"vacuum_ms": -5}) == 0
    assert skincfg.chamber_vacuum_ms({"vacuum_ms": 10**6}) == skincfg.MAX_VACUUM_MS
    assert skincfg.chamber_vacuum_ms({"vacuum_ms": "x"}) == skincfg.DEFAULT_VACUUM_MS


def test_build_skin_entry_omits_empty_type_variant():
    entry = skincfg.build_skin_entry("x", [{"mac": "AA", "slot": 0}])
    assert "skin_type" not in entry and "skin_variant" not in entry


def test_build_skin_entry_always_writes_the_fill_mode():
    chambers = [{"mac": "AA", "slot": 0, "fill_mode": "time"},
                {"mac": "BB", "slot": 1, "fill_mode": "pressure"},
                {"mac": "BB", "slot": 2}]                            # default
    entry = skincfg.build_skin_entry("belly-1", chambers)
    assert [c["fill_mode"] for c in entry["chambers"]] \
        == ["time", "pressure", "pressure"]
    assert "fill_mode" not in chambers[2]
    # input list must not be mutated
    assert chambers[0]["fill_mode"] == "time"


def test_normalize_fill_mode():
    assert skincfg.normalize_fill_mode("pressure") == "pressure"
    assert skincfg.normalize_fill_mode("time") == "time"
    assert skincfg.normalize_fill_mode(None) == skincfg.DEFAULT_FILL_MODE
    assert skincfg.normalize_fill_mode("bogus") == skincfg.DEFAULT_FILL_MODE


def test_save_skin_entry_append_and_replace():
    data = _data()
    skincfg.save_skin_entry(data, "turtle", 0, -1, {"skin_id": "new-1"})
    skins = data["robots"]["turtles"][0]["skins"]
    assert skins[-1]["skin_id"] == "new-1"
    skincfg.save_skin_entry(data, "turtle", 0, 0, {"skin_id": "belly-1b"})
    assert skins[0]["skin_id"] == "belly-1b"


def test_delete_skin():
    data = _data()
    skincfg.delete_skin(data, "turtle", 0, 0)
    skins = data["robots"]["turtles"][0]["skins"]
    assert [s["skin_id"] for s in skins] == ["belly-2"]


def test_saved_organs_survive_a_dialog_save():
    # The skin dialog no longer edits organs, so it must carry them through.
    organs = [{"id": "organ-1", "good_ohm": 1500, "bad_ohm": 4700}]
    entry: dict = {"skin_id": "x"}
    skincfg.carry_unmanaged_skin_keys(entry, {"skin_id": "x", "organs": organs})
    assert entry["organs"] == organs


def test_max_organs_for():
    from src.hardware.skin_geometry import max_organs_for
    assert max_organs_for("turtle_square") == 3
    assert max_organs_for("tree_round") == 1
    assert max_organs_for("unknown") == 3
    assert max_organs_for(None) == 3


def test_carry_calibration_keeps_every_calibration_key():
    saved = {"mac": "AA", "slot": 1, "max_pressure": 9.0,
             "fill_profile": [[0, 0]], "fill_profiles": {"1": [[0, 0]]},
             "deflate_profile": {"floor": -3}, "hold_duty_curve": [[1, 2]],
             "leak_curve": [[3, 4]], "duty_curve": [[5, 6]], "fill_time_ms": None}
    ch = {"mac": "AA", "slot": 1, "max_pressure": 7.0}
    skincfg.carry_calibration(ch, skincfg.chambers_by_key([saved]).get(("AA", 1)))
    for key in ("fill_profile", "fill_profiles", "deflate_profile",
                "hold_duty_curve", "leak_curve", "duty_curve"):
        assert ch[key] == saved[key]
    assert ch["max_pressure"] == 7.0          # row-edited fields win
    assert "fill_time_ms" not in ch           # None is not carried


def test_carry_calibration_no_saved_chamber():
    ch = {"mac": "AA", "slot": 0}
    skincfg.carry_calibration(ch, None)
    assert ch == {"mac": "AA", "slot": 0}


def test_carry_unmanaged_skin_keys_keeps_foreign_keys_only():
    saved = {"skin_id": "old", "name": "Belly", "organ": {"mux_ch": 3},
             "touch": {"node_mac": "X"}, "sensor_grid": [[0]]}
    entry = {"skin_id": "new", "chambers": []}
    skincfg.carry_unmanaged_skin_keys(entry, saved)
    assert entry == {"skin_id": "new", "chambers": [],
                     "name": "Belly", "organ": {"mux_ch": 3}}


# ---------------------------------------------------------------------------
# led_layout persistence
# ---------------------------------------------------------------------------

def test_set_skin_led_layout_patches_in_place_and_cleans_values():
    data = _data()
    assert skincfg.set_skin_led_layout(
        data, "turtle", 0, 0,
        {"count": "68", "gap": 1, "brightness": 180, "clockwise": 0,
         "ring": 0, "bogus": 1, "start": 0.3})
    saved = data["robots"]["turtles"][0]["skins"][0]["led_layout"]
    assert saved == {"count": 68, "gap": 1, "brightness": 180,
                     "clockwise": False, "ring": 0}
    # An empty layout removes the key (back to the type default).
    assert skincfg.set_skin_led_layout(data, "turtle", 0, 0, {})
    assert "led_layout" not in data["robots"]["turtles"][0]["skins"][0]
    assert not skincfg.set_skin_led_layout(data, "turtle", 0, 9, {"count": 1})


def test_normalise_led_layout_ignores_bad_values():
    assert skincfg.normalise_led_layout({"count": "x", "gap": None,
                                          "brightness": 12}) == {"brightness": 12}
    assert skincfg.normalise_led_layout(None) == {}


def test_led_layout_is_a_dialog_managed_key():
    assert "led_layout" in skincfg.DIALOG_SKIN_KEYS
    assert skincfg.NODE_LED_RINGS["node_direct"] == (68,)


def test_chamber_summary_with_and_without_mac():
    chambers = [{"mac": "F4:65:0B:B5:78:04", "slot": 0},
                {"mac": "F4:65:0B:B5:78:04", "slot": 2}]
    assert skincfg.chamber_summary(chambers) == "78:04#0, 78:04#2"
    assert skincfg.chamber_summary(chambers, with_mac=False) == "slots 0, 2"


def test_unconfigured_macs_lists_nodes_missing_from_the_robot():
    chambers = [{"mac": "AA:01", "slot": 0}, {"mac": "AA:02", "slot": 0},
                {"mac": "AA:02", "slot": 1}, {"mac": "", "slot": 2}]
    assert skincfg.unconfigured_macs(chambers, ["AA:01"]) == ["AA:02"]
    assert skincfg.unconfigured_macs(chambers, ["AA:01", "AA:02"]) == []


def test_next_free_slot_skips_used_slots_on_that_node_only():
    used = [("AA", 0), ("AA", 1), ("BB", 2), (None, None)]
    assert skincfg.next_free_slot("AA", used, 3) == 2
    assert skincfg.next_free_slot("BB", used, 3) == 0
    assert skincfg.next_free_slot("AA", [("AA", 1)], 3) == 0
    # A full node falls back to slot 0.
    assert skincfg.next_free_slot("AA", used + [("AA", 2)], 3) == 0


def test_sensor_address_is_the_binary_index():
    from src.core.touch_zones import sensor_address
    assert [sensor_address(i, 4) for i in range(4)] == ["00", "01", "10", "11"]
    assert sensor_address(5, 8) == "101"
    assert sensor_address(0, 1) == "0"

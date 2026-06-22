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
                    {"node_type": "node_direct"},  # no mac → ignored
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
    assert skincfg.magnet_macs(_data(), "turtle", 0) == ["MM"]


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
    chambers = [{"mac": "AA", "slot": 0, "max_pressure": 11.0},
                {"mac": "BB", "slot": 1, "max_pressure": 8.5}]  # vs default 8.0
    changes = skincfg.large_pressure_changes(chambers, prev)
    assert len(changes) == 1
    assert changes[0].mac == "AA" and changes[0].new_kpa == 11.0


# --- entry building + persistence ----------------------------------------

def test_build_skin_entry_strips_default_pressure():
    chambers = [{"mac": "AA", "slot": 0, "max_pressure": 8.0},
                {"mac": "BB", "slot": 1, "max_pressure": 10.0}]
    entry = skincfg.build_skin_entry("belly-1", chambers,
                                     skin_type="round_4", skin_variant="v2")
    assert "max_pressure" not in entry["chambers"][0]
    assert entry["chambers"][1]["max_pressure"] == 10.0
    assert entry["skin_type"] == "round_4"
    assert entry["skin_variant"] == "v2"
    # input list must not be mutated
    assert chambers[0]["max_pressure"] == 8.0


def test_build_skin_entry_omits_empty_type_variant():
    entry = skincfg.build_skin_entry("x", [{"mac": "AA", "slot": 0}])
    assert "skin_type" not in entry and "skin_variant" not in entry


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


def test_apply_organs_writes_and_drops():
    entry: dict = {"skin_id": "x"}
    organs = [{"id": "liver", "shape": "ellipse", "rect": [0.1, 0.1, 0.3, 0.3],
               "good_ohm": 1500, "bad_ohm": 4700}]
    skincfg.apply_organs(entry, organs)
    assert entry["organs"] == organs
    # Mutating the source list must not affect the stored copy.
    organs[0]["id"] = "changed"
    assert entry["organs"][0]["id"] == "liver"
    # Empty list removes the key.
    skincfg.apply_organs(entry, [])
    assert "organs" not in entry


def test_max_organs_for():
    from src.hardware.skin_geometry import max_organs_for
    assert max_organs_for("turtle_square") == 3
    assert max_organs_for("tree_round") == 1
    assert max_organs_for("unknown") == 3
    assert max_organs_for(None) == 3

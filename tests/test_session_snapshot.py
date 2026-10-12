"""Session snapshots: what a session ran with."""

from src.core import session_snapshot as snapshot


def _settings():
    return {
        "touch": {"act_threshold_ut": {"thymio": 120}},
        "robots": {
            "turtles": [{"id": "turtle-1", "skins": [{"skin_id": "turtle-1"}]}],
            "thymios": [
                {"thymio_id": "thymio-1", "skins": [{"skin_id": "thymio-1"}]},
                {"thymio_id": "thymio-2", "skins": [{"skin_id": "thymio-2"}]},
            ],
        },
    }


def test_snapshot_keeps_only_the_session_robots_and_is_a_copy():
    data = _settings()
    spec = {"initial": "a", "states": {"a": {"do": []}}}
    snaps = snapshot.build_session_snapshots(
        activity_name="Any", activity_spec=spec, settings_data=data,
        robot_ids=["thymio-2", "turtle-1"], firmware={"AA": "magvec-1"})

    robots = snaps[snapshot.KIND_ROBOTS]["robots"]
    assert [(r["robot_type"], r.get("id") or r.get("thymio_id")) for r in robots] == [
        ("turtle", "turtle-1"), ("thymio", "thymio-2")]
    assert snaps[snapshot.KIND_ACTIVITY] == {"name": "Any", "spec": spec}
    assert snaps[snapshot.KIND_TOUCH] == {"act_threshold_ut": {"thymio": 120}}
    assert snaps[snapshot.KIND_FIRMWARE] == {"AA": "magvec-1"}

    # Later edits to the live configuration must not change the snapshot.
    data["touch"]["act_threshold_ut"]["thymio"] = 999
    spec["initial"] = "changed"
    assert snaps[snapshot.KIND_TOUCH]["act_threshold_ut"]["thymio"] == 120
    assert snaps[snapshot.KIND_ACTIVITY]["spec"]["initial"] == "a"


def test_snapshot_without_spec_or_touch_section():
    snaps = snapshot.build_session_snapshots(
        activity_name="Coded", activity_spec=None, settings_data={},
        robot_ids=["x"])
    assert snaps[snapshot.KIND_ACTIVITY] == {"name": "Coded", "spec": None}
    assert snaps[snapshot.KIND_ROBOTS] == {"robots": []}
    assert snaps[snapshot.KIND_TOUCH] == {} and snaps[snapshot.KIND_FIRMWARE] == {}


class _Ctrl:
    def __init__(self, mac, fw):
        self.mac_address = mac
        self._fw = fw

    def get_last_status(self):
        return {"fw": self._fw} if self._fw else {}


class _Skin:
    def __init__(self, controller, touch_controller=None):
        self.controller = controller
        self.touch_controller = touch_controller


class _Robot:
    def __init__(self, skins):
        self.skins = dict(enumerate(skins))


def test_node_firmware_reads_reported_markers_and_skips_simulated():
    robots = [_Robot([_Skin(_Ctrl("AA", "hold-5"), _Ctrl("BB", "")),
                      _Skin(object())])]
    assert snapshot.node_firmware(robots) == {"AA": "hold-5", "BB": ""}

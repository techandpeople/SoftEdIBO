"""Sensor mapping session (node_multiplexed) against a simulated node."""

from src.hardware import sensor_mapping as sm
from src.hardware.sensor_mapping import Phase, SensorMapSession
from src.robots._robot_builder import multiplexed_configure


class _FakeNode:
    """A board whose chamber -> mux input plumbing is ``wiring``.

    Inflating a chamber raises its input (plus a weaker echo on the floating
    neighbours, as seen on the real board); venting drops it back."""

    REST = 400

    def __init__(self, wiring: dict[int, int], *, responds: bool = True,
                 streams: bool = True) -> None:
        self.wiring = wiring
        self.responds = responds
        self.streams = streams
        self.sent: list[tuple[str, dict]] = []
        self.level = {slot: 0.0 for slot in range(9)}   # ADC counts above rest
        self.inflating: int | None = None
        self.venting: int | None = None
        self.scan = False

    def send(self, cmd: str, **kw) -> bool:
        self.sent.append((cmd, kw))
        if cmd == "sensor_scan":
            self.scan = bool(kw["on"]) and self.streams
        if not self.responds:
            return True
        if cmd == "inflate":
            self.inflating = kw["chamber"]
        elif cmd == "hold":
            self.inflating = None
        elif cmd == "vent":
            self.venting = kw["chamber"] if kw["open"] else None
            if kw["open"]:
                self.inflating = None
        return True

    def step(self, dt: float) -> None:
        if self.inflating is not None:
            self.level[self.inflating] += 1500 * dt
        if self.venting is not None:
            self.level[self.venting] = max(0.0, self.level[self.venting] - 3000 * dt)

    def raw(self) -> list[int]:
        adc = [self.REST] * sm.MUX_CHANNELS
        for slot, level in self.level.items():
            ch = self.wiring.get(slot)
            if ch is None:
                continue
            adc[ch] += int(level)
            for near in (ch - 1, ch + 1):
                if 0 <= near < sm.MUX_CHANNELS and near not in self.wiring.values():
                    adc[near] += int(level * 0.4)
        return adc

    def status(self) -> tuple[list[int], list[int]]:
        st = [1 if self.inflating == i else 0 for i in range(9)]
        return st, st


def _run(session: SensorMapSession, node: _FakeNode, clock: list[float],
         *, limit_s: float = 120.0) -> None:
    session.start()
    dt = 0.05
    while session.running and clock[0] < limit_s:
        clock[0] += dt
        node.step(dt)
        if node.scan:
            session.on_raw(node.raw())
        session.on_status(*node.status())
        session.tick()


def _session(node: _FakeNode, slots, clock: list[float], **kw) -> SensorMapSession:
    return SensorMapSession(slots, node.send, clock=lambda: clock[0], **kw)


def test_finds_the_input_behind_each_chamber():
    node = _FakeNode({0: 3, 1: 0, 2: 7})
    clock = [0.0]
    session = _session(node, [0, 1, 2], clock)
    _run(session, node, clock)
    assert session.finished
    assert session.channels() == [3, 0, 7]
    assert all(m.actuated for m in session.results.values())
    assert session.progress == 100


def test_inflate_runs_until_an_input_reaches_the_target_rise():
    node = _FakeNode({0: 3})
    clock = [0.0]
    session = _session(node, [0], clock, stop_kpa=20, pulse_ms=4000)
    _run(session, node, clock)
    inflate = [kw for c, kw in node.sent if c == "inflate"]
    assert inflate == [{"chamber": 0, "delta": 100, "ms": 4000, "timed": 1}]
    cmds = [c for c, _ in node.sent]
    assert cmds.index("hold") > cmds.index("inflate")
    # Stopped on the 20 kPa rise (one frame of overshoot), far from the 4 s cap.
    assert 20.0 <= session.results[0].rise_kpa < 24.0


def test_time_cap_ends_the_inflate_when_the_target_is_out_of_reach():
    node = _FakeNode({0: 3})
    clock = [0.0]
    session = _session(node, [0], clock, stop_kpa=60, pulse_ms=300)
    _run(session, node, clock)
    assert not any(c == "hold" for c, _ in node.sent)   # the node's own cap closed it
    assert session.channels() == [3]                    # the rise it did see still counts


def test_chamber_is_vented_and_released_before_the_next_one():
    node = _FakeNode({0: 3, 1: 0})
    clock = [0.0]
    session = _session(node, [0, 1], clock)
    _run(session, node, clock)
    events = [(c, kw.get("chamber"), kw.get("open"))
              for c, kw in node.sent if c in ("inflate", "vent")]
    assert events == [("inflate", 0, None), ("vent", 0, 1), ("vent", 0, 0),
                      ("inflate", 1, None), ("vent", 1, 1), ("vent", 1, 0)]
    assert node.level[0] == 0.0 and node.level[1] == 0.0
    assert node.sent[-1] == ("sensor_scan", {"on": 0})


def test_chamber_without_a_sensor_is_reported_as_none():
    node = _FakeNode({0: 3})            # chamber 1 has no input at all
    clock = [0.0]
    session = _session(node, [0, 1], clock)
    _run(session, node, clock)
    assert session.channels() == [3, sm.NO_SENSOR]
    assert not session.results[1].found
    assert session.results[1].actuated      # the valve did open: no sensor, not no response


def test_node_that_does_not_actuate_is_told_apart_from_no_sensor():
    node = _FakeNode({0: 3}, responds=False)     # e.g. emergency stop latched
    clock = [0.0]
    session = _session(node, [0], clock)
    _run(session, node, clock)
    assert session.finished
    assert not session.results[0].found
    assert not session.results[0].actuated


def test_fails_when_the_node_never_streams_raw_readings():
    node = _FakeNode({0: 3}, streams=False)      # firmware without sensor_scan
    clock = [0.0]
    session = _session(node, [0], clock)
    _run(session, node, clock)
    assert session.phase is Phase.FAILED
    assert session.error == sm.NO_STREAM_ERROR
    assert not any(c == "inflate" for c, _ in node.sent)   # never pumped blind


def test_stop_mid_pulse_holds_the_chamber_and_leaves_it_venting():
    node = _FakeNode({0: 3})
    clock = [0.0]
    session = _session(node, [0], clock)
    session.start()
    while session.phase is not Phase.PULSE:
        clock[0] += 0.05
        session.on_raw(node.raw())
        session.tick()
    node.sent.clear()
    session.stop()
    assert node.sent == [("hold", {"chamber": 0}),
                         ("vent", {"chamber": 0, "open": 1}),
                         ("sensor_scan", {"on": 0})]
    assert not session.running


def test_scan_request_is_kept_alive_while_running():
    node = _FakeNode({0: 3, 1: 0, 2: 7})
    clock = [0.0]
    session = _session(node, [0, 1, 2], clock)
    _run(session, node, clock)
    assert sum(1 for c, kw in node.sent if c == "sensor_scan" and kw["on"]) >= 3


def test_input_claimed_twice_goes_to_the_stronger_rise():
    session = SensorMapSession([0, 1], lambda *a, **k: None)
    session._results = {0: sm.SensorMatch(0, 3, 400, True),
                        1: sm.SensorMatch(1, 3, 90, True)}
    assert session.channels() == [3, sm.NO_SENSOR]
    assert session.shared_channels() == {3: [0, 1]}


def test_channel_label_names_the_pcb_sensor():
    assert sm.channel_label(3) == "I3 (PSENSOR4)"
    assert sm.channel_label(13) == "I13 (external)"
    assert sm.channel_label(sm.NO_SENSOR) == "none"


# --- settings ---------------------------------------------------------------

def _data():
    node = {"mac": "AA:01", "node_type": "node_multiplexed", "max_slots": 9}
    return {"robots": {
        "turtles": [{"id": "turtle-1", "nodes": [dict(node)]}],
        "trees": [{"id": "tree-1", "nodes": [dict(node, max_slots=6)]}],
        "thymios": [{"thymio_id": "thymio-1", "nodes": [
            {"mac": "BB:01", "node_type": "node_direct", "max_slots": 3}]}],
    }}


def test_map_is_stored_on_every_entry_of_a_shared_board():
    data = _data()
    assert sm.set_sensor_channels(data, "AA:01", [3, 0, -1]) == 2
    assert data["robots"]["turtles"][0]["nodes"][0]["sensor_channels"] == [3, 0, -1]
    assert data["robots"]["trees"][0]["nodes"][0]["sensor_channels"] == [3, 0, -1]
    assert sm.get_sensor_channels(data, "AA:01") == [3, 0, -1]
    assert sm.set_sensor_channels(data, "AA:01", None) == 2
    assert "sensor_channels" not in data["robots"]["turtles"][0]["nodes"][0]
    assert sm.get_sensor_channels(data, "AA:01") == []


def test_iter_multiplexed_nodes_lists_each_board_once():
    nodes = sm.iter_multiplexed_nodes(_data())
    assert nodes == [{"mac": "AA:01", "max_slots": 9,
                      "robots": ["turtle-1", "tree-1"], "sensor_channels": [],
                      "organ_channels": []}]


def test_configure_payload_carries_the_saved_map():
    node = {"mac": "AA:01", "node_type": "node_multiplexed", "max_slots": 9}
    payload = multiplexed_configure(node)
    assert payload is not None
    assert payload["sensor_channels"] == []     # unmapped: board default
    node["sensor_channels"] = [3, 0, -1]
    payload = multiplexed_configure(node)
    assert payload is not None
    assert payload["sensor_channels"] == [3, 0, -1]
